"""Canva Connect API: OAuth (PKCE), upload de asset, autofill a partir do design-template e export PNG.

O template mestre nunca é alterado: usamos `create_from_design`, que cria um design NOVO por promoção.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from nikki.db import kv_get, kv_set, session_scope
from nikki.settings import Settings, get_settings

API = "https://api.canva.com/rest/v1"
AUTHORIZE = "https://www.canva.com/api/oauth/authorize"
SCOPES = [
    "asset:read", "asset:write", "design:content:read", "design:content:write", "design:meta:read",
    "brandtemplate:meta:read", "brandtemplate:content:read", "profile:read",
]
KV_TOKENS = "canva_tokens"
KV_PKCE = "canva_pkce"


class CanvaError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class CanvaNotAuthorized(CanvaError):
    pass


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def authorization_url(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    verifier = _b64url(secrets.token_bytes(64))[:96]
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))
    with session_scope() as db:
        kv_set(db, KV_PKCE, {"verifier": verifier, "state": state})
    q = {
        "code_challenge": challenge, "code_challenge_method": "s256", "scope": " ".join(SCOPES),
        "response_type": "code", "client_id": s.canva_client_id, "redirect_uri": s.canva_redirect_uri, "state": state,
    }
    return f"{AUTHORIZE}?{urlencode(q)}"


def _token_request(data: dict[str, str], s: Settings) -> dict[str, Any]:
    basic = base64.b64encode(f"{s.canva_client_id}:{s.canva_client_secret}".encode()).decode()
    r = httpx.post(f"{API}/oauth/token", data=data, headers={"Authorization": f"Basic {basic}"}, timeout=30)
    if r.status_code >= 400:
        raise CanvaError(f"OAuth Canva falhou HTTP {r.status_code}: {r.text[:300]}", retryable=r.status_code >= 500)
    tok = r.json()
    tok["obtained_at"] = int(time.time())
    return tok


def exchange_code(code: str, state: str, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    with session_scope() as db:
        pkce = kv_get(db, KV_PKCE) or {}
    if not pkce or pkce.get("state") != state:
        raise CanvaError("state OAuth inválido")
    tok = _token_request({"grant_type": "authorization_code", "code": code, "code_verifier": pkce["verifier"],
                          "redirect_uri": s.canva_redirect_uri}, s)
    with session_scope() as db:
        kv_set(db, KV_TOKENS, tok)


def is_authorized() -> bool:
    with session_scope() as db:
        return bool((kv_get(db, KV_TOKENS) or {}).get("refresh_token"))


class CanvaClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = settings or get_settings()
        self._http = httpx.Client(base_url=API, timeout=60, transport=transport)

    # --------------------------------------------------------------- auth
    def _access_token(self) -> str:
        with session_scope() as db:
            tok = kv_get(db, KV_TOKENS)
            if not tok or not tok.get("refresh_token"):
                raise CanvaNotAuthorized("Canva não autorizado: abra /oauth/canva/start")
            if time.time() < tok["obtained_at"] + int(tok.get("expires_in", 0)) - 120:
                return tok["access_token"]
            # refresh token é de uso único: grava o novo na mesma transação
            new = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, self.s)
            kv_set(db, KV_TOKENS, new)
            return new["access_token"]

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._access_token()}", **kw.pop("headers", {})}
        try:
            r = self._http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise CanvaError(f"rede Canva {path}: {e}", retryable=True) from e
        if r.status_code >= 400:
            raise CanvaError(f"Canva {method} {path} HTTP {r.status_code}: {r.text[:500]}",
                             retryable=r.status_code in (429,) or r.status_code >= 500)
        return r.json() if r.content else {}

    def _poll(self, path: str, timeout: float = 120) -> dict[str, Any]:
        deadline = time.time() + timeout
        delay = 1.0
        while True:
            job = self._req("GET", path)["job"]
            if job["status"] == "success":
                return job
            if job["status"] == "failed":
                raise CanvaError(f"job Canva falhou: {json.dumps(job.get('error'))[:300]}")
            if time.time() > deadline:
                raise CanvaError(f"timeout aguardando job Canva {path}", retryable=True)
            time.sleep(delay)
            delay = min(delay * 1.5, 5)

    # ------------------------------------------------------------- ações
    def me(self) -> dict[str, Any]:
        return self._req("GET", "/users/me")

    def get_design(self, design_id: str) -> dict[str, Any]:
        return self._req("GET", f"/designs/{design_id}")

    def upload_image(self, name: str, png_bytes: bytes) -> str:
        meta = {"name_base64": base64.b64encode(name[:50].encode()).decode()}
        job = self._req("POST", "/asset-uploads", content=png_bytes, headers={
            "Content-Type": "application/octet-stream", "Asset-Upload-Metadata": json.dumps(meta)})["job"]
        if job["status"] != "success":
            job = self._poll(f"/asset-uploads/{job['id']}")
        return job["asset"]["id"]

    def autofill(self, template_design_id: str, title: str, fields: dict[str, str], image_asset_id: str | None) -> str:
        data: dict[str, Any] = {k: {"type": "text", "text": v if v else " "} for k, v in fields.items()}
        if image_asset_id:
            data["PRODUCT_IMAGE"] = {"type": "image", "asset_id": image_asset_id}
        job = self._req("POST", "/autofills", json={"type": "create_from_design", "design_id": template_design_id,
                                                     "title": title[:255], "data": data})["job"]
        if job["status"] != "success":
            job = self._poll(f"/autofills/{job['id']}")
        return job["result"]["design"]["id"]

    def export_png(self, design_id: str, width: int | None = None) -> list[str]:
        fmt: dict[str, Any] = {"type": "png", "export_quality": "regular"}
        if width:
            fmt["width"] = width
        job = self._req("POST", "/exports", json={"design_id": design_id, "format": fmt})["job"]
        if job["status"] != "success":
            job = self._poll(f"/exports/{job['id']}", timeout=180)
        return job["urls"]
