"""YouTube Data API v3: OAuth (loopback, PKCE) + upload resumível de Shorts.

GARANTIA: só publica no canal da Nikki. O canal do token é verificado na autorização e antes
de cada upload (ID em NIKKI_YOUTUBE_CHANNEL_ID, ou nome contendo "nikki"); divergência = bloqueio.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from nikki.integrations import oauth_store
from nikki.integrations.metricool import PublishError, classify_http
from nikki.settings import Settings, get_settings

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/youtube.force-ssl"]  # force-ssl: editar título/descrição
PROVIDER = "google"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def authorization_url(s: Settings | None = None) -> str:
    s = s or get_settings()
    state, _, challenge = oauth_store.new_pkce(PROVIDER)
    q = {"client_id": s.google_client_id, "redirect_uri": s.google_redirect_uri, "response_type": "code",
         "scope": " ".join(SCOPES), "state": state, "access_type": "offline", "prompt": "consent",
         "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(q)}"


def _token(data: dict[str, str], s: Settings) -> dict[str, Any]:
    r = httpx.post(TOKEN_URL, data={"client_id": s.google_client_id, "client_secret": s.google_client_secret, **data}, timeout=30)
    if r.status_code >= 400:
        raise PublishError(f"OAuth Google falhou HTTP {r.status_code}: {r.text[:300]}", retryable=r.status_code >= 500)
    return r.json()


def is_nikki_channel(channel: dict[str, Any], expected_id: str) -> bool:
    if expected_id:
        return channel.get("id") == expected_id
    return "nikki" in (channel.get("snippet", {}).get("title") or "").lower()


def mine_channel(access_token: str) -> dict[str, Any]:
    r = httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                  headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code >= 400:
        raise PublishError(f"YouTube channels HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
    items = r.json().get("items") or []
    if len(items) != 1:
        raise PublishError(f"token do YouTube não aponta para exatamente 1 canal ({len(items)})", retryable=False)
    return items[0]


def exchange_code(code: str, state: str, s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    pk = oauth_store.pop_pkce(PROVIDER, state)
    tok = _token({"code": code, "grant_type": "authorization_code", "redirect_uri": s.google_redirect_uri,
                  "code_verifier": pk["verifier"]}, s)
    ch = mine_channel(tok["access_token"])
    title = ch.get("snippet", {}).get("title")
    if not is_nikki_channel(ch, s.youtube_channel_id):
        raise PublishError(f"canal autorizado '{title}' ({ch.get('id')}) NÃO é o canal da Nikki. Refaça e escolha o canal "
                           "da Nikki na tela do Google.", retryable=False)
    oauth_store.save_tokens(PROVIDER, {**tok, "channel_id": ch["id"], "channel_title": title})
    return {"channel_id": ch["id"], "channel_title": title}


class YouTubeClient:
    def __init__(self, s: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = s or get_settings()
        self.http = httpx.Client(timeout=600, transport=transport)

    def _access(self) -> str:
        tok = oauth_store.load_tokens(PROVIDER)
        if not tok or not tok.get("refresh_token"):
            raise PublishError("YouTube não autorizado: abra /oauth/google/start", retryable=False)
        if oauth_store.expired(tok):
            new = _token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, self.s)
            tok = {**tok, **new}
            oauth_store.save_tokens(PROVIDER, tok)
        return tok["access_token"]

    def _check(self, r: httpx.Response, what: str) -> None:
        if r.status_code < 400:
            return
        reason = ""
        try:
            reason = r.json()["error"]["errors"][0]["reason"]
        except (ValueError, KeyError, IndexError):
            pass
        retryable = classify_http(r.status_code) or reason in ("rateLimitExceeded", "backendError")
        if reason in ("quotaExceeded", "uploadLimitExceeded"):
            retryable = True  # quota renova em 24h; o backoff cobre
        raise PublishError(f"YouTube {what} HTTP {r.status_code}: {reason or r.text[:300]}", retryable=retryable,
                           response={"status": r.status_code, "reason": reason})

    def assert_nikki_channel(self) -> str:
        tok = oauth_store.load_tokens(PROVIDER) or {}
        ch = mine_channel(self._access())
        if not is_nikki_channel(ch, self.s.youtube_channel_id) or (tok.get("channel_id") and ch["id"] != tok["channel_id"]):
            raise PublishError(f"BLOQUEADO: token aponta para o canal '{ch.get('snippet', {}).get('title')}', não o da Nikki",
                               retryable=False)
        return ch["id"]

    def upload_short(self, video: Path, title: str, description: str, tags: list[str] | None = None) -> dict[str, Any]:
        self.assert_nikki_channel()
        meta = {
            "snippet": {"title": title[:100], "description": description[:4900], "tags": (tags or [])[:30], "categoryId": "22",
                        "defaultLanguage": "pt-BR"},
            "status": {"privacyStatus": self.s.youtube_privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
        }
        size = video.stat().st_size
        try:
            init = self.http.post("https://www.googleapis.com/upload/youtube/v3/videos",
                                  params={"uploadType": "resumable", "part": "snippet,status"},
                                  headers={"Authorization": f"Bearer {self._access()}", "Content-Type": "application/json; charset=UTF-8",
                                           "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                                  content=json.dumps(meta))
            self._check(init, "init")
            r = self.http.put(init.headers["Location"], content=video.read_bytes(),
                              headers={"Authorization": f"Bearer {self._access()}", "Content-Type": "video/mp4"})
        except httpx.HTTPError as e:
            raise PublishError(f"rede YouTube: {e}", retryable=True) from e
        self._check(r, "upload")
        body = r.json()
        st = body.get("status", {})
        return {"id": body["id"], "url": f"https://www.youtube.com/shorts/{body['id']}", "privacy": st.get("privacyStatus"),
                "upload_status": st.get("uploadStatus")}

    def video_status(self, video_id: str) -> dict[str, Any]:
        r = self.http.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "status,statistics", "id": video_id},
                          headers={"Authorization": f"Bearer {self._access()}"})
        self._check(r, "status")
        items = r.json().get("items") or []
        return items[0] if items else {}

    def recent_videos(self, limit: int = 25) -> list[dict[str, Any]]:
        h = {"Authorization": f"Bearer {self._access()}"}
        ch = self.http.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "contentDetails", "mine": "true"}, headers=h)
        self._check(ch, "channels")
        uploads = ch.json()["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl = self.http.get("https://www.googleapis.com/youtube/v3/playlistItems",
                           params={"part": "contentDetails", "playlistId": uploads, "maxResults": limit}, headers=h)
        self._check(pl, "playlist")
        ids = [i["contentDetails"]["videoId"] for i in pl.json().get("items", [])]
        if not ids:
            return []
        v = self.http.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "statistics,snippet", "id": ",".join(ids)}, headers=h)
        self._check(v, "videos")
        return v.json().get("items", [])
