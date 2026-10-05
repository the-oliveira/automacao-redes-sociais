"""Armazenamento de tokens OAuth no banco local (tabela kv). Nunca em arquivo versionado ou log."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any

from nikki.db import kv_get, kv_set, session_scope


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def new_pkce(provider: str, extra: dict[str, Any] | None = None) -> tuple[str, str, str]:
    """Gera (state, verifier, challenge) e guarda para validar o callback."""
    verifier = b64url(secrets.token_bytes(64))[:96]
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(24))
    with session_scope() as db:
        kv_set(db, f"{provider}_pkce", {"state": state, "verifier": verifier, **(extra or {})})
    return state, verifier, challenge


def pop_pkce(provider: str, state: str) -> dict[str, Any]:
    with session_scope() as db:
        data = kv_get(db, f"{provider}_pkce") or {}
        if not data or data.get("state") != state:
            raise ValueError(f"state OAuth inválido para {provider}")
        kv_set(db, f"{provider}_pkce", {})
    return data


def save_tokens(provider: str, tokens: dict[str, Any]) -> None:
    tokens = {**tokens, "obtained_at": int(time.time())}
    with session_scope() as db:
        kv_set(db, f"{provider}_tokens", tokens)


def load_tokens(provider: str) -> dict[str, Any] | None:
    with session_scope() as db:
        return kv_get(db, f"{provider}_tokens")


def expired(tokens: dict[str, Any], skew: int = 120) -> bool:
    exp = tokens.get("expires_in")
    if not exp:
        return False
    return time.time() > tokens["obtained_at"] + int(exp) - skew
