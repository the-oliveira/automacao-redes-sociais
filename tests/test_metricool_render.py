import json
from datetime import datetime

import httpx
import pytest
from PIL import ImageDraw

from nikki.integrations.metricool import MetricoolClient, PublishError
from nikki.media import render
from nikki.settings import get_settings
from tests.conftest import fake_product


def client_with(handler):
    s = get_settings().model_copy(update={"metricool_user_token": "tok", "metricool_user_id": "42", "metricool_blog_id": "999"})
    return MetricoolClient(s, transport=httpx.MockTransport(handler))


def test_schedule_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json={"data": {"id": 987, "uuid": "u-1", "providers": [{"network": "instagram", "status": "PENDING"}]}})

    c = client_with(handler)
    body = c.build_post(network="instagram", fmt="story", text="oi", media=["https://m/x.png"], when=datetime(2026, 10, 5, 10, 0))
    res = c.schedule(body)
    req = seen["req"]
    assert req.headers["X-Mc-Auth"] == "tok"
    assert req.url.params["blogId"] == "999" and req.url.params["userId"] == "42"
    sent = json.loads(req.content)
    assert sent["publicationDate"] == {"dateTime": "2026-10-05T10:00:00", "timezone": "America/Sao_Paulo"}
    assert sent["providers"] == [{"network": "instagram"}]
    assert sent["instagramData"]["type"] == "STORY" and sent["autoPublish"] is True
    assert res.external_id == "987" and res.status == "PENDING"


@pytest.mark.parametrize("net,fmt,key,expected", [
    ("instagram", "feed", "instagramData", "POST"), ("instagram", "reel", "instagramData", "REEL"),
    ("facebook", "post", "facebookData", "POST"), ("facebook", "reel", "facebookData", "REEL"),
])
def test_network_types(net, fmt, key, expected):
    c = client_with(lambda r: httpx.Response(200, json={}))
    assert c.build_post(network=net, fmt=fmt, text="t", media=[], when=datetime.now())[key]["type"] == expected


def test_youtube_and_tiktok_payloads():
    c = client_with(lambda r: httpx.Response(200, json={}))
    yt = c.build_post(network="youtube", fmt="short", text="t", media=[], when=datetime.now(), title="Título #shorts", tags=["a"])
    assert yt["youtubeData"]["type"] == "short" and yt["youtubeData"]["title"] == "Título #shorts" and yt["youtubeData"]["madeForKids"] is False
    tk = c.build_post(network="tiktok", fmt="video", text="t", media=[], when=datetime.now(), title="x")
    assert tk["tiktokData"]["privacyOption"] == "PUBLIC_TO_EVERYONE"


@pytest.mark.parametrize("status,retryable", [(500, True), (503, True), (429, True), (400, False), (401, False), (403, False)])
def test_error_classification(status, retryable):
    c = client_with(lambda r: httpx.Response(status, text="erro"))
    with pytest.raises(PublishError) as ei:
        c.schedule({"x": 1})
    assert ei.value.retryable is retryable


def test_timeout_is_retryable():
    def handler(req):
        raise httpx.ReadTimeout("t")

    with pytest.raises(PublishError) as ei:
        client_with(handler).schedule({})
    assert ei.value.retryable


def test_upload_flow(tmp_path, monkeypatch):
    f = tmp_path / "a.png"
    f.write_bytes(b"PNGDATA")
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "PUT":
            return httpx.Response(200, json={"data": {"presignedUrl": "https://s3.example/k?sig=1", "key": "k", "bucket": "b",
                                                      "expiresAt": "x", "uploadType": "SIMPLE", "fileUrl": "https://cdn.example/k"}})
        return httpx.Response(200, json={"data": {"fileUrl": "https://cdn.example/k"}})

    monkeypatch.setattr(httpx, "put", lambda url, **kw: httpx.Response(200))
    assert client_with(handler).upload_file(f) == "https://cdn.example/k"
    assert calls == [("PUT", "/api/v2/media/s3/upload-transactions"), ("PATCH", "/api/v2/media/s3/upload-transactions")]


# ------------------------------------------------------------------ arte

PRICES = ["9,90", "49,90", "999,90", "1.299,90", "12.999,90"]


@pytest.mark.parametrize("kind", ["feed", "story"])
@pytest.mark.parametrize("value", PRICES)
def test_price_never_overflows_or_overlaps(kind, value):
    from PIL import Image

    W, _ = render.SIZES[kind]
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    fv, fr, rs_w, gap, total = render.price_layout(d, value, W - 144, 190)
    assert total <= W - 144
    assert gap >= 10 and rs_w == int(d.textlength("R$", font=fr))  # valor começa depois do R$ + gap


@pytest.mark.parametrize("kind", ["feed", "story"])
def test_render_long_content_inside_safe_area(kind):
    art = render.ArtData(fake_product(), "Nome de produto extremamente longo " * 4, "R$ 15.499,90", "12.999,90", "-36% OFF",
                         "CUPOM: NIKKI10", "Link no grupo da Nikki")
    img = render.render_art(art, kind)
    assert img.size == render.SIZES[kind]
    # faixa inferior reservada (UI do app) precisa ficar só com o fundo, sem texto/CTA
    bottom = 300 if kind == "story" else 60
    band = img.crop((0, img.height - bottom, img.width, img.height)).convert("L")
    assert max(band.getdata()) < 140, "conteúdo invadiu a safe area inferior"
