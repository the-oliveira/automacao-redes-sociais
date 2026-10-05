"""Configuração central: segredos vêm do .env, regras de negócio de config/*.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_prefix="NIKKI_", extra="ignore", env_file_encoding="utf-8"
    )

    # Execução
    env: Literal["dev", "prod", "test"] = "dev"
    host: str = "127.0.0.1"
    port: int = 8710
    database_url: str = f"sqlite:///{(DATA_DIR / 'nikki.db').as_posix()}"
    timezone: str = "America/Sao_Paulo"
    worker_interval_seconds: float = 5.0
    sync_interval_seconds: float = 300.0
    api_key: str = ""  # se definido, exigido no header X-API-Key do POST /api/promotions
    # URL pública deste servidor (túnel/domínio). Se vazio, posts usam o link direto do grupo.
    public_base_url: str = ""

    # Roteamento
    instagram_active_group: Literal["primary", "reserve"] = "primary"

    # Publisher
    publisher: Literal["direct", "metricool", "dry_run"] = "dry_run"
    # Redes ativas (as demais são removidas do plano de distribuição).
    networks: str = "instagram,facebook,tiktok,youtube"

    # Meta (Facebook Page + Instagram Business) — publica SOMENTE na Página
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_page_id: str = ""
    meta_api_version: str = "v25.0"
    meta_redirect_uri: str = "http://localhost:8710/oauth/meta/callback"

    # Google / YouTube
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8710/oauth/google/callback"
    youtube_privacy: Literal["public", "unlisted", "private"] = "public"
    # Canal da Nikki (subcanal/conta de marca). Uploads em qualquer outro canal são bloqueados.
    youtube_channel_id: str = ""

    # TikTok
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "http://127.0.0.1:8710/oauth/tiktok/callback/"
    metricool_user_token: str = ""
    metricool_user_id: str = ""
    metricool_blog_id: str = ""
    metricool_base_url: str = "https://app.metricool.com/api"
    # Marca os primeiros posts como teste de operação (desligar após QA).
    qa_mode: bool = False

    # Mídia
    renderer: Literal["auto", "canva", "local"] = "auto"
    canva_client_id: str = ""
    canva_client_secret: str = ""
    canva_redirect_uri: str = "http://127.0.0.1:8710/oauth/canva/callback"
    canva_template_feed: str = ""  # design-template do feed (ID do Canva)
    canva_template_story: str = ""  # design-template vertical

    # Conteúdo
    tiktok_url_in_caption: bool = False

    media_dir: Path = Field(default=DATA_DIR / "media")

    def rules(self) -> dict[str, Any]:
        return load_yaml("rules.yaml")

    # Arquivo com os links reais (fora do Git). Sem ele, usa o exemplo (testes/CI).
    whatsapp_config: str = "whatsapp_groups.yaml"

    def whatsapp_groups(self) -> dict[str, dict[str, str]]:
        name = self.whatsapp_config if (CONFIG_DIR / self.whatsapp_config).exists() else "whatsapp_groups.example.yaml"
        return load_yaml(name)["groups"]

    @property
    def enabled_networks(self) -> list[str]:
        return [n.strip().lower() for n in self.networks.split(",") if n.strip()]

    @property
    def metricool_configured(self) -> bool:
        return bool(self.metricool_user_token and self.metricool_user_id and self.metricool_blog_id)

    @property
    def canva_configured(self) -> bool:
        return bool(self.canva_client_id and self.canva_client_secret)


def load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
