import json
from datetime import timedelta

import httpx
import pytest

from nikki.db import PublicationJob, session_scope, utcnow
from nikki.integrations import meta, oauth_store, tiktok, youtube
from nikki.integrations.metricool import PublishError
from nikki.pipeline import orchestrator as orch
from nikki.settings import get_settings, reset_settings_cache

PAGE, IG, ME = "111", "222", "999"


def meta_client(handler):
    oauth_store.save_tokens("meta", {"page_id": PAGE, "page_token": "PAGE_TOKEN", "ig_user_id": IG, "personal_user_id": ME})
    return meta.MetaClient(get_settings(), transport=httpx.MockTransport(handler))


def recorder():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        path = req.url.path
        if path.endswith("/photos"):
            return httpx.Response(200, json={"id": "ph1", "post_id": f"{PAGE}_post1"})
        if req.method == "GET" and "fields=images" in str(req.url):
            return httpx.Response(200, json={"images": [{"width": 1080, "source": "https://scontent.fbcdn/x.png"}]})
        if req.method == "GET" and "status_code" in str(req.url):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if path.endswith("/media"):
            return httpx.Response(200, json={"id": "cont1"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "igmedia1"})
        if path.endswith("/photo_stories"):
            return httpx.Response(200, json={"post_id": "st1"})
        return httpx.Response(200, json={"permalink": "https://instagram.com/p/x", "permalink_url": "https://facebook.com/x"})

    return calls, handler


# ------------------------------------------------- garantia: só Página da Nikki


def test_facebook_post_goes_to_page_with_page_token(tmp_path):
    calls, h = recorder()
    img = tmp_path / "f.png"
    img.write_bytes(b"x")
    meta_client(h).fb_photo_post(img, "oi")
    writes = [c for c in calls if c.method == "POST"]
    assert writes and all(c.url.path.split("/")[2] in (PAGE, IG) for c in writes)
    assert all(b"PAGE_TOKEN" in c.content for c in writes)
    assert not any("/me/" in c.url.path or c.url.path.endswith("/me") or "/feed" in c.url.path for c in writes)


def test_instagram_image_hosted_as_unpublished_page_photo(tmp_path):
    calls, h = recorder()
    img = tmp_path / "f.png"
    img.write_bytes(b"x")
    res = meta_client(h).ig_image(img, "legenda")
    host = next(c for c in calls if c.url.path.endswith("/photos"))
    assert host.url.path.split("/")[2] == PAGE and b'name="published"\r\n\r\nfalse' in host.content
    cont = next(c for c in calls if c.url.path.endswith("/media"))
    assert cont.url.path.split("/")[2] == IG and b"scontent.fbcdn" in cont.content
    assert res["id"] == "igmedia1"


@pytest.mark.parametrize("path", ["me/feed", "me/photos", f"{ME}/feed", f"{ME}/photos", "333/photos"])
def test_writes_outside_page_are_blocked(path):
    c = meta_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(PublishError, match="BLOQUEADO"):
        c._post(path, {"message": "x"})


def test_select_page_never_falls_back_to_first():
    pages = [{"id": "1", "name": "Perfil Pessoal Biz"}, {"id": "2", "name": "Nikki Ofertas"}]
    assert meta.select_page(pages, "")["id"] == "2"
    assert meta.select_page(pages, "1")["id"] == "1"
    with pytest.raises(PublishError):
        meta.select_page([{"id": "1", "name": "Outra"}, {"id": "3", "name": "Mais uma"}], "")
    with pytest.raises(PublishError):
        meta.select_page(pages, "404")


def test_saved_token_for_other_page_refused(monkeypatch):
    monkeypatch.setenv("NIKKI_META_PAGE_ID", "555")
    reset_settings_cache()
    oauth_store.save_tokens("meta", {"page_id": PAGE, "page_token": "T", "ig_user_id": IG})
    with pytest.raises(PublishError):
        meta.MetaClient(get_settings())


def test_meta_scopes_have_no_personal_publish():
    assert not any(s in meta.SCOPES for s in ("publish_actions", "user_posts", "publish_to_groups"))


def test_oauth_exchange_rejects_personal_id_and_non_page_token(monkeypatch):
    oauth_store.new_pkce("meta")
    state = oauth_store.load_tokens  # noqa: F841 - só para clareza
    from nikki.db import kv_get

    with session_scope() as db:
        st = kv_get(db, "meta_pkce")["state"]
    responses = {
        "oauth/access_token": [{"access_token": "short"}, {"access_token": "long"}],
        "me/accounts": [{"data": [{"id": "2", "name": "Nikki", "access_token": "PT", "instagram_business_account": {"id": IG}}]}],
        "me": [{"id": ME}, {"id": "OUTRO"}],  # token da página devolve id errado
    }

    def fake_get(url, params, timeout=60):
        key = url.split("/v25.0/", 1)[1]
        return httpx.Response(200, json=responses[key].pop(0), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(PublishError, match="não pertence à Página"):
        meta.exchange_code("c", st)


# ------------------------------------------------------------ youtube / tiktok


NIKKI_CH = {"items": [{"id": "UCnikki", "snippet": {"title": "Nikki Ofertas"}}]}
MAIN_CH = {"items": [{"id": "UCmain", "snippet": {"title": "Canal Principal"}}]}


@pytest.fixture
def channel(monkeypatch):
    state = {"resp": NIKKI_CH}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=state["resp"], request=httpx.Request("GET", url)))
    return state


def test_youtube_blocks_upload_on_main_channel(tmp_path, channel):
    oauth_store.save_tokens("google", {"access_token": "A", "refresh_token": "R", "expires_in": 3600, "channel_id": "UCnikki"})
    channel["resp"] = MAIN_CH
    v = tmp_path / "v.mp4"
    v.write_bytes(b"v")
    sent = []
    c = youtube.YouTubeClient(get_settings(), transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    with pytest.raises(PublishError, match="BLOQUEADO"):
        c.upload_short(v, "t", "d")
    assert sent == []  # nada foi enviado


def test_youtube_oauth_refuses_main_channel(monkeypatch, channel):
    channel["resp"] = MAIN_CH
    state, _, _ = oauth_store.new_pkce("google")
    monkeypatch.setattr(youtube, "_token", lambda data, s: {"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    with pytest.raises(PublishError, match="NÃO é o canal da Nikki"):
        youtube.exchange_code("c", state)
    assert oauth_store.load_tokens("google") is None


def test_youtube_channel_id_pin(monkeypatch, channel):
    monkeypatch.setenv("NIKKI_YOUTUBE_CHANNEL_ID", "UCoutro")
    reset_settings_cache()
    state, _, _ = oauth_store.new_pkce("google")
    monkeypatch.setattr(youtube, "_token", lambda data, s: {"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    with pytest.raises(PublishError):
        youtube.exchange_code("c", state)  # nome tem "nikki" mas o ID fixado manda


def test_youtube_upload_shape(tmp_path, channel):
    oauth_store.save_tokens("google", {"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    v = tmp_path / "v.mp4"
    v.write_bytes(b"video")
    seen = []

    def h(req):
        seen.append(req)
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/session1"})
        return httpx.Response(200, json={"id": "yt1", "status": {"privacyStatus": "public", "uploadStatus": "uploaded"}})

    res = youtube.YouTubeClient(get_settings(), transport=httpx.MockTransport(h)).upload_short(v, "T #shorts", "desc", ["a"])
    meta_body = json.loads(seen[0].content)
    assert meta_body["status"]["selfDeclaredMadeForKids"] is False and meta_body["snippet"]["title"] == "T #shorts"
    assert seen[1].url.host == "upload.example" and res["url"] == "https://www.youtube.com/shorts/yt1"


def test_youtube_quota_is_retryable(tmp_path, channel):
    oauth_store.save_tokens("google", {"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    v = tmp_path / "v.mp4"
    v.write_bytes(b"v")
    h = lambda req: httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})  # noqa: E731
    with pytest.raises(PublishError) as ei:
        youtube.YouTubeClient(get_settings(), transport=httpx.MockTransport(h)).upload_short(v, "t", "d")
    assert ei.value.retryable


def test_tiktok_flow_uses_allowed_privacy(tmp_path):
    oauth_store.save_tokens("tiktok", {"access_token": "A", "refresh_token": "R", "expires_in": 86400})
    v = tmp_path / "v.mp4"
    v.write_bytes(b"0123456789")
    seen = []

    def h(req):
        seen.append(req)
        p = req.url.path
        if p.endswith("creator_info/query/"):
            return httpx.Response(200, json={"data": {"privacy_level_options": ["SELF_ONLY"], "creator_username": "nikki"}, "error": {"code": "ok"}})
        if p.endswith("video/init/"):
            return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url": "https://up.tiktok/x"}, "error": {"code": "ok"}})
        if req.method == "PUT":
            assert req.headers["Content-Range"] == "bytes 0-9/10"
            return httpx.Response(201)
        return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [777]}, "error": {"code": "ok"}})

    res = tiktok.TikTokClient(get_settings(), transport=httpx.MockTransport(h)).post_video(v, "legenda")
    init = json.loads(next(r for r in seen if r.url.path.endswith("video/init/")).content)
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"  # app não auditado
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 10, "chunk_size": 10, "total_chunk_count": 1}
    assert res["url"] == "https://www.tiktok.com/@nikki/video/777"


def test_tiktok_pkce_challenge_is_hex(monkeypatch):
    monkeypatch.setenv("NIKKI_TIKTOK_CLIENT_KEY", "ck")
    reset_settings_cache()
    url = tiktok.authorization_url()
    from urllib.parse import parse_qs, urlsplit

    q = parse_qs(urlsplit(url).query)
    assert len(q["code_challenge"][0]) == 64 and all(c in "0123456789abcdef" for c in q["code_challenge"][0])
    assert "video.publish" in q["scope"][0]


# ---------------------------------------------------- publisher direto no pipeline


def test_direct_jobs_wait_for_slot(monkeypatch):
    from fastapi.testclient import TestClient

    from nikki.web.app import app
    from tests.test_pipeline import HIGH, promo

    monkeypatch.setenv("NIKKI_PUBLISHER", "direct")
    monkeypatch.setenv("NIKKI_NETWORKS", "instagram,facebook")
    reset_settings_cache()
    with TestClient(app) as c:
        c.post("/api/promotions", json=promo(**HIGH))
    orch.process_queued_promotions()
    with session_scope() as db:
        jobs = db.query(PublicationJob).all()
        assert {j.network for j in jobs} == {"instagram", "facebook"}  # redes desativadas ficam fora
        assert all(j.next_attempt_at > utcnow() + timedelta(minutes=1) for j in jobs)  # não publica antes do horário
    assert orch.process_due_jobs() == 0
