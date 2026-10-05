"""TikTok Content Posting API (Direct Post, FILE_UPLOAD) + Login Kit Desktop (PKCE com SHA256 em hex)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from nikki.integrations import oauth_store
from nikki.integrations.metricool import PublishError, classify_http
from nikki.settings import Settings, get_settings

API = "https://open.tiktokapis.com/v2"
SCOPES = ["user.info.basic", "video.publish", "video.upload", "video.list"]
PROVIDER = "tiktok"
CHUNK = 10 * 1024 * 1024


def authorization_url(s: Settings | None = None) -> str:
    s = s or get_settings()
    state, verifier, _ = oauth_store.new_pkce(PROVIDER)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()  # TikTok desktop exige hex
    q = {"client_key": s.tiktok_client_key, "response_type": "code", "scope": ",".join(SCOPES),
         "redirect_uri": s.tiktok_redirect_uri, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(q)}"


def _token(data: dict[str, str], s: Settings) -> dict[str, Any]:
    r = httpx.post(f"{API}/oauth/token/", data={"client_key": s.tiktok_client_key, "client_secret": s.tiktok_client_secret, **data},
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    body = r.json() if r.content else {}
    if r.status_code >= 400 or "access_token" not in body:
        raise PublishError(f"OAuth TikTok falhou: {body.get('error_description') or body}", retryable=r.status_code >= 500)
    return body


def exchange_code(code: str, state: str, s: Settings | None = None) -> None:
    s = s or get_settings()
    pk = oauth_store.pop_pkce(PROVIDER, state)
    oauth_store.save_tokens(PROVIDER, _token({"code": code, "grant_type": "authorization_code",
                                              "redirect_uri": s.tiktok_redirect_uri, "code_verifier": pk["verifier"]}, s))


class TikTokClient:
    def __init__(self, s: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = s or get_settings()
        self.http = httpx.Client(timeout=300, transport=transport)

    def _access(self) -> str:
        tok = oauth_store.load_tokens(PROVIDER)
        if not tok:
            raise PublishError("TikTok não autorizado: abra /oauth/tiktok/start", retryable=False)
        if oauth_store.expired(tok):
            tok = _token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, self.s)
            oauth_store.save_tokens(PROVIDER, tok)
        return tok["access_token"]

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.http.post(f"{API}{path}", json=body, headers={"Authorization": f"Bearer {self._access()}",
                                                                   "Content-Type": "application/json; charset=UTF-8"})
        except httpx.HTTPError as e:
            raise PublishError(f"rede TikTok: {e}", retryable=True) from e
        data = r.json() if r.content else {}
        err = data.get("error") or {}
        if r.status_code >= 400 or (err.get("code") not in (None, "ok")):
            code = err.get("code")
            retryable = classify_http(r.status_code) or code in ("rate_limit_exceeded", "internal_error")
            raise PublishError(f"TikTok {path} HTTP {r.status_code}: {code} {err.get('message')}", retryable=retryable,
                               response={"status": r.status_code, "error": err})
        return data.get("data") or {}

    def creator_info(self) -> dict[str, Any]:
        return self._post("/post/publish/creator_info/query/", {})

    def post_video(self, video: Path, title: str) -> dict[str, Any]:
        info = self.creator_info()
        options = info.get("privacy_level_options") or ["SELF_ONLY"]
        privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else options[0]
        size = video.stat().st_size
        chunk = size if size <= CHUNK else CHUNK
        count = max(1, size // chunk) if size > CHUNK else 1
        init = self._post("/post/publish/video/init/", {
            "post_info": {"title": title[:2200], "privacy_level": privacy, "disable_duet": False, "disable_stitch": False,
                          "disable_comment": False, "brand_organic_toggle": True},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
        })
        data = video.read_bytes()
        for i in range(count):
            start = i * chunk
            end = size if i == count - 1 else start + chunk  # último chunk absorve o resto
            r = self.http.put(init["upload_url"], content=data[start:end], headers={
                "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end - 1}/{size}"})
            if r.status_code >= 300:
                raise PublishError(f"upload TikTok chunk {i} HTTP {r.status_code}", retryable=classify_http(r.status_code))
        publish_id = init["publish_id"]
        status = self.wait(publish_id)
        post_ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
        user = info.get("creator_username")
        url = f"https://www.tiktok.com/@{user}/video/{post_ids[0]}" if post_ids and user else None
        return {"id": str(post_ids[0]) if post_ids else publish_id, "url": url, "privacy": privacy, "status": status.get("status")}

    def wait(self, publish_id: str, timeout: float = 300) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            st = self._post("/post/publish/status/fetch/", {"publish_id": publish_id})
            s = st.get("status")
            if s == "PUBLISH_COMPLETE":
                return st
            if s == "FAILED":
                raise PublishError(f"TikTok recusou o vídeo: {st.get('fail_reason')}", retryable=False)
            if time.time() > deadline:
                return st  # ainda processando: o sync posterior confirma
            time.sleep(5)

    def recent_videos(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            r = self.http.post(f"{API}/video/list/", params={"fields": "id,share_url,create_time,view_count,like_count,comment_count,share_count"},
                               json={"max_count": limit}, headers={"Authorization": f"Bearer {self._access()}"})
        except httpx.HTTPError as e:
            raise PublishError(f"rede TikTok: {e}", retryable=True) from e
        if r.status_code >= 400:
            raise PublishError(f"TikTok video/list HTTP {r.status_code}: {r.text[:200]}", retryable=classify_http(r.status_code))
        return (r.json().get("data") or {}).get("videos") or []
