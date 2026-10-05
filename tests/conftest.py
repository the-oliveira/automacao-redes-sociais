from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from nikki import db as dbmod
from nikki.media import render
from nikki.settings import reset_settings_cache


def fake_product() -> Image.Image:
    img = Image.new("RGBA", (600, 600), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((100, 150, 500, 450), 50, fill=(30, 30, 40, 255))
    return img


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NIKKI_ENV", "test")
    monkeypatch.setenv("NIKKI_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("NIKKI_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("NIKKI_PUBLISHER", "dry_run")
    monkeypatch.setenv("NIKKI_RENDERER", "local")
    monkeypatch.setenv("NIKKI_INSTAGRAM_ACTIVE_GROUP", "primary")
    monkeypatch.setenv("NIKKI_QA_MODE", "false")
    monkeypatch.setenv("NIKKI_PUBLIC_BASE_URL", "")
    monkeypatch.setenv("NIKKI_API_KEY", "")
    monkeypatch.setenv("NIKKI_TIKTOK_URL_IN_CAPTION", "false")
    monkeypatch.setenv("NIKKI_METRICOOL_USER_TOKEN", "")
    monkeypatch.setenv("NIKKI_CANVA_CLIENT_ID", "")
    # testes nunca dependem do .env real
    for k, v in {"NIKKI_NETWORKS": "instagram,facebook,tiktok,youtube", "NIKKI_YOUTUBE_CHANNEL_ID": "", "NIKKI_META_PAGE_ID": "",
                 "NIKKI_META_APP_ID": "", "NIKKI_GOOGLE_CLIENT_ID": "", "NIKKI_TIKTOK_CLIENT_KEY": "",
                 "NIKKI_YOUTUBE_PRIVACY": "public",
                 "NIKKI_WHATSAPP_CONFIG": "whatsapp_groups.example.yaml"}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    dbmod._engine = None
    dbmod._Session = None
    dbmod.init_engine()

    def _fetch(url: str, timeout: float = 20.0):
        if "invalid" in url or "broken" in url:
            raise render.InvalidImage("conteúdo não é imagem válida (text/html)")
        return fake_product()

    monkeypatch.setattr(render, "fetch_image", _fetch)
    # vídeo real via ffmpeg é lento: nos testes gera um arquivo vazio
    from nikki.media import video

    def _fake_video(src, out, *a, **k):
        out.write_bytes(b"\x00")
        return out

    monkeypatch.setattr(video, "single_offer_video", _fake_video)
    monkeypatch.setattr(video, "slideshow_video", lambda frames, out, *a, **k: _fake_video(None, out))
    yield
    reset_settings_cache()


def promo(**over) -> dict:
    base = {
        "external_id": "ext-1",
        "product_name": "Controle DualSense PS5 Branco",
        "original_price": 449.90,
        "current_price": 299.90,
        "coupon": None,
        "product_url": "https://www.amazon.com.br/dp/B0BQXZ11B8?ref=abc",
        "affiliate_url": "https://www.amazon.com.br/dp/B0BQXZ11B8?tag=nikki-20",
        "image_url": "https://m.media-amazon.com/images/I/x.jpg",
        "store": "amazon",
        "category": "games",
    }
    base.update(over)
    return base
