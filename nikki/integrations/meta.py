"""Meta Graph API: Página do Facebook + Instagram Business ligado a ela.

GARANTIA: nada é publicado no perfil pessoal. Toda escrita usa o token da PÁGINA e um
caminho que começa pelo ID da Página da Nikki (ou do IG Business dela); qualquer outro
destino (ex.: /me/feed) é bloqueado antes da chamada. O token de usuário não é guardado.

- Facebook: post com foto (/photos), story (/photo_stories), reel (/video_reels).
- Instagram: container -> status -> media_publish. Imagens precisam de URL pública: usamos uma
  foto NÃO publicada na própria Página como hospedagem (URL do CDN do Facebook).
  Vídeos usam upload resumível (rupload.facebook.com) direto do arquivo local.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from nikki.integrations import oauth_store
from nikki.integrations.metricool import PublishError, classify_http
from nikki.settings import Settings, get_settings

SCOPES = [
    "pages_show_list", "pages_read_engagement", "pages_manage_posts", "pages_manage_metadata",
    "instagram_basic", "instagram_content_publish", "instagram_manage_insights", "read_insights", "business_management",
]
PROVIDER = "meta"


def graph(s: Settings) -> str:
    return f"https://graph.facebook.com/{s.meta_api_version}"


def authorization_url(s: Settings | None = None) -> str:
    s = s or get_settings()
    state, _, _ = oauth_store.new_pkce(PROVIDER)
    q = {"client_id": s.meta_app_id, "redirect_uri": s.meta_redirect_uri, "state": state, "scope": ",".join(SCOPES),
         "response_type": "code"}
    return f"https://www.facebook.com/{s.meta_api_version}/dialog/oauth?{urlencode(q)}"


def exchange_code(code: str, state: str, s: Settings | None = None) -> dict[str, Any]:
    """code -> token curto -> token longo -> token da Página (não expira) + id do IG Business."""
    s = s or get_settings()
    oauth_store.pop_pkce(PROVIDER, state)
    g = graph(s)
    short = _get(f"{g}/oauth/access_token", {"client_id": s.meta_app_id, "client_secret": s.meta_app_secret,
                                             "redirect_uri": s.meta_redirect_uri, "code": code})
    long = _get(f"{g}/oauth/access_token", {"grant_type": "fb_exchange_token", "client_id": s.meta_app_id,
                                            "client_secret": s.meta_app_secret, "fb_exchange_token": short["access_token"]})
    pages = _get(f"{g}/me/accounts", {"access_token": long["access_token"],
                                      "fields": "id,name,access_token,instagram_business_account{id,username}"})["data"]
    if not pages:
        raise PublishError("nenhuma Página do Facebook autorizada para o app", retryable=False)
    page = select_page(pages, s.meta_page_id)
    me_user = _get(f"{g}/me", {"access_token": long["access_token"], "fields": "id"})["id"]
    if page["id"] == me_user:
        raise PublishError("ID selecionado é do perfil pessoal, não da Página", retryable=False)
    # o token precisa ser DA PÁGINA: /me com ele devolve a própria Página
    if _get(f"{g}/me", {"access_token": page["access_token"], "fields": "id"})["id"] != page["id"]:
        raise PublishError("token obtido não pertence à Página", retryable=False)
    ig = page.get("instagram_business_account") or {}
    info = {"page_id": page["id"], "page_name": page["name"], "page_token": page["access_token"],
            "ig_user_id": ig.get("id"), "ig_username": ig.get("username"), "personal_user_id": me_user,
            "pages_available": [{"id": p["id"], "name": p["name"]} for p in pages]}
    oauth_store.save_tokens(PROVIDER, info)
    return {k: v for k, v in info.items() if "token" not in k}


def select_page(pages: list[dict[str, Any]], page_id: str) -> dict[str, Any]:
    """Escolhe a Página da Nikki: por ID explícito, ou a única cujo nome contém 'nikki'. Nunca 'a primeira'."""
    if page_id:
        hit = [p for p in pages if p["id"] == page_id]
    else:
        hit = [p for p in pages if "nikki" in p.get("name", "").lower()]
    if len(hit) != 1:
        names = ", ".join(f"{p['name']} ({p['id']})" for p in pages)
        raise PublishError(f"não consegui identificar a Página da Nikki com segurança; defina NIKKI_META_PAGE_ID. Páginas: {names}",
                           retryable=False)
    return hit[0]


def _raise(r: httpx.Response, what: str) -> None:
    if r.status_code < 400:
        return
    try:
        err = r.json().get("error", {})
    except ValueError:
        err = {}
    code = err.get("code")
    # 4/17/32/613 = rate limit; 1/2 = erro temporário; 190 = token inválido (não adianta retry)
    retryable = classify_http(r.status_code) or code in (1, 2, 4, 17, 32, 341, 613) or bool(err.get("is_transient"))
    raise PublishError(f"Meta {what} HTTP {r.status_code}: {err.get('message') or r.text[:300]} (code={code})",
                       retryable=retryable, response={"status": r.status_code, "error": err})


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        r = httpx.get(url, params=params, timeout=60)
    except httpx.HTTPError as e:
        raise PublishError(f"rede Meta: {e}", retryable=True) from e
    _raise(r, "GET " + url.rsplit("/", 1)[-1])
    return r.json()


class MetaClient:
    def __init__(self, s: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = s or get_settings()
        tok = oauth_store.load_tokens(PROVIDER)
        if not tok or not tok.get("page_token"):
            raise PublishError("Meta não autorizado: abra /oauth/meta/start", retryable=False)
        self.page_id, self.page_token, self.ig_id = tok["page_id"], tok["page_token"], tok.get("ig_user_id")
        self.personal_user_id = tok.get("personal_user_id")
        if self.s.meta_page_id and self.page_id != self.s.meta_page_id:
            raise PublishError("token salvo é de outra Página; refaça /oauth/meta/start", retryable=False)
        self.http = httpx.Client(timeout=120, transport=transport)
        self.g = graph(self.s)

    def _assert_page_target(self, path: str) -> None:
        owner = path.split("/", 1)[0]
        allowed = {self.page_id} | ({self.ig_id} if self.ig_id else set())
        if owner not in allowed or owner in ("me", self.personal_user_id):
            raise PublishError(f"BLOQUEADO: escrita fora da Página da Nikki ({path})", retryable=False)

    def _post(self, path: str, data: dict[str, Any] | None = None, files: Any = None, what: str = "") -> dict[str, Any]:
        self._assert_page_target(path)
        try:
            r = self.http.post(f"{self.g}/{path}", data={**(data or {}), "access_token": self.page_token}, files=files)
        except httpx.HTTPError as e:
            raise PublishError(f"rede Meta: {e}", retryable=True) from e
        _raise(r, what or path)
        return r.json()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.http.get(f"{self.g}/{path}", params={**params, "access_token": self.page_token})
        except httpx.HTTPError as e:
            raise PublishError(f"rede Meta: {e}", retryable=True) from e
        _raise(r, path)
        return r.json()

    # ------------------------------------------------------------ facebook
    def fb_photo_post(self, image: Path, message: str) -> dict[str, Any]:
        with open(image, "rb") as fh:
            res = self._post(f"{self.page_id}/photos", {"message": message, "published": "true"},
                             files={"source": (image.name, fh, "image/png")}, what="fb photo")
        post_id = res.get("post_id") or res["id"]
        return {"id": post_id, "url": self.permalink(post_id)}

    def host_image(self, image: Path) -> tuple[str, str]:
        """Sobe foto NÃO publicada na Página e devolve (photo_id, url pública do CDN)."""
        with open(image, "rb") as fh:
            res = self._post(f"{self.page_id}/photos", {"published": "false", "temporary": "true"},
                             files={"source": (image.name, fh, "image/png")}, what="host image")
        info = self._get(res["id"], {"fields": "images"})
        best = max(info["images"], key=lambda i: i.get("width", 0))
        return res["id"], best["source"]

    def fb_photo_story(self, image: Path) -> dict[str, Any]:
        photo_id, _ = self.host_image(image)
        res = self._post(f"{self.page_id}/photo_stories", {"photo_id": photo_id}, what="fb story")
        return {"id": res.get("post_id") or res.get("id"), "url": None}

    def fb_reel(self, video: Path, description: str) -> dict[str, Any]:
        start = self._post(f"{self.page_id}/video_reels", {"upload_phase": "start"}, what="fb reel start")
        vid = start["video_id"]
        data = video.read_bytes()
        r = self.http.post(f"https://rupload.facebook.com/video-upload/{self.s.meta_api_version}/{vid}", content=data,
                           headers={"Authorization": f"OAuth {self.page_token}", "offset": "0", "file_size": str(len(data))})
        _raise(r, "fb reel upload")
        self._post(f"{self.page_id}/video_reels", {"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                                                     "description": description}, what="fb reel finish")
        return {"id": vid, "url": f"https://www.facebook.com/reel/{vid}"}

    def permalink(self, object_id: str) -> str | None:
        try:
            return self._get(object_id, {"fields": "permalink_url"}).get("permalink_url")
        except PublishError:
            return None

    # ----------------------------------------------------------- instagram
    def _require_ig(self) -> str:
        if not self.ig_id:
            raise PublishError("Página do Facebook sem conta Instagram Business vinculada", retryable=False)
        return self.ig_id

    def _wait_container(self, cid: str, timeout: float = 300) -> None:
        deadline = time.time() + timeout
        while True:
            st = self._get(cid, {"fields": "status_code,status"})
            code = st.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"container IG {code}: {st.get('status')}", retryable=code == "EXPIRED")
            if time.time() > deadline:
                raise PublishError("timeout processando mídia no Instagram", retryable=True)
            time.sleep(4)

    def _publish_container(self, cid: str) -> dict[str, Any]:
        ig = self._require_ig()
        self._wait_container(cid)
        res = self._post(f"{ig}/media_publish", {"creation_id": cid}, what="ig publish")
        media_id = res["id"]
        try:
            link = self._get(media_id, {"fields": "permalink"}).get("permalink")
        except PublishError:
            link = None
        return {"id": media_id, "url": link}

    def ig_image(self, image: Path, caption: str | None, story: bool = False) -> dict[str, Any]:
        ig = self._require_ig()
        _, url = self.host_image(image)
        data = {"image_url": url}
        if story:
            data["media_type"] = "STORIES"
        elif caption:
            data["caption"] = caption
        cid = self._post(f"{ig}/media", data, what="ig container")["id"]
        return self._publish_container(cid)

    def ig_reel(self, video: Path, caption: str) -> dict[str, Any]:
        ig = self._require_ig()
        cid = self._post(f"{ig}/media", {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                                         "share_to_feed": "true"}, what="ig reel container")["id"]
        data = video.read_bytes()
        r = self.http.post(f"https://rupload.facebook.com/ig-api-upload/{self.s.meta_api_version}/{cid}", content=data,
                           headers={"Authorization": f"OAuth {self.page_token}", "offset": "0", "file_size": str(len(data))})
        _raise(r, "ig reel upload")
        return self._publish_container(cid)

    # ----------------------------------------------------------- insights
    def ig_recent_media(self, limit: int = 25) -> list[dict[str, Any]]:
        ig = self._require_ig()
        return self._get(f"{ig}/media", {"fields": "id,permalink,media_type,timestamp,like_count,comments_count",
                                         "limit": limit}).get("data", [])

    def fb_recent_posts(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._get(f"{self.page_id}/published_posts", {
            "fields": "id,permalink_url,created_time,shares,reactions.summary(true),comments.summary(true)", "limit": limit}).get("data", [])
