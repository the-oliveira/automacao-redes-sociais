from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from nikki.db import ContentVariant, MediaAsset, Promotion, PublicationAttempt, PublicationJob, session_scope, utcnow
from nikki.integrations.metricool import PublishError, ScheduledResult
from nikki.pipeline import orchestrator as orch
from nikki.settings import get_settings
from nikki.web.app import app
from tests.conftest import promo

HIGH = dict(product_name="Booster Box Pokémon TCG Escarlate e Violeta 36 pacotes", category="tcg", original_price=1299.90,
            current_price=649.90, coupon="NIKKI10", popularity=0.95)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class FakePublisher:
    name = "fake"

    def __init__(self, fail: dict[str, Exception] | None = None):
        self.fail = fail or {}
        self.calls: list[tuple[str, str]] = []

    def publish(self, db, job, variant, media):
        self.calls.append((job.network, job.format))
        if job.network in self.fail:
            raise self.fail[job.network]
        return ScheduledResult(f"mc-{job.id}", None, "PENDING", None, {})

    def status(self, external_id):
        return {"status": "PUBLISHED", "public_url": f"https://social.example/{external_id}"}


def due_all():
    with session_scope() as db:
        for j in db.query(PublicationJob).all():
            if j.next_attempt_at:
                j.next_attempt_at = utcnow() - timedelta(seconds=1)


def test_end_to_end_dry_run(client):
    r = client.post("/api/promotions", json=promo(**HIGH))
    assert r.status_code == 202 and r.json()["status"] == "QUEUED"
    pid = r.json()["promotion_id"]
    orch.process_queued_promotions()
    with session_scope() as db:
        p = db.get(Promotion, pid)
        assert p.tier == "high", p.score_breakdown
        assert p.status == "SCHEDULING"
        kinds = {m.kind for m in db.query(MediaAsset).filter_by(promotion_id=pid)}
        assert kinds == {"feed", "story", "video"}
        variants = db.query(ContentVariant).filter_by(promotion_id=pid).all()
        assert {(v.network, v.format) for v in variants} == {("instagram", "feed"), ("instagram", "story"), ("instagram", "reel"),
                                                             ("facebook", "post"), ("tiktok", "video"), ("youtube", "short")}
        for v in variants:
            assert v.group_key.startswith(v.network)
    orch.process_due_jobs()
    st = client.get(f"/api/promotions/{pid}").json()
    assert st["status"] == "SCHEDULED"
    assert {j["status"] for j in st["jobs"]} == {"DRY_RUN"}
    assert client.get("/").status_code == 200
    assert client.get(f"/promotion/{pid}").status_code == 200


def test_duplicate_webhook_never_publishes_twice(client):
    a = client.post("/api/promotions", json=promo(**HIGH)).json()
    b = client.post("/api/promotions", json=promo(**HIGH)).json()
    assert b["status"] == "DUPLICATE"
    orch.process_queued_promotions()
    orch.process_queued_promotions()
    pub = FakePublisher()
    orch.process_due_jobs(publisher=pub)
    orch.process_due_jobs(publisher=pub)
    assert len(pub.calls) == len(set(pub.calls)) == 6
    with session_scope() as db:
        assert db.query(PublicationJob).count() == 6
        assert db.query(Promotion).count() == 1 and a["promotion_id"] == b["duplicate_of"]


def test_low_score_is_skipped(client):
    r = client.post("/api/promotions", json=promo(product_name="Pano de prato", category="casa", store="lojax",
                                                 original_price=10, current_price=9.5, affiliate_url=None,
                                                 product_url="https://lojax.com/p")).json()
    orch.process_queued_promotions()
    with session_scope() as db:
        p = db.get(Promotion, r["promotion_id"])
        assert p.status == "SKIPPED" and db.query(PublicationJob).count() == 0


def test_invalid_image_rejected_at_media_stage(client):
    r = client.post("/api/promotions", json=promo(**HIGH, image_url="https://site.com/broken.jpg")).json()
    orch.process_queued_promotions()
    with session_scope() as db:
        p = db.get(Promotion, r["promotion_id"])
        assert p.status == "REJECTED" and "imagem inválida" in p.status_reason
        assert db.query(PublicationJob).count() == 0


def test_partial_failure_tiktok_does_not_cancel_others(client):
    client.post("/api/promotions", json=promo(**HIGH))
    orch.process_queued_promotions()
    pub = FakePublisher(fail={"tiktok": PublishError("HTTP 503", retryable=True)})
    orch.process_due_jobs(publisher=pub)
    with session_scope() as db:
        jobs = {j.network + "/" + j.format: j for j in db.query(PublicationJob).all()}
        assert jobs["tiktok/video"].status == "RETRYING"
        assert all(j.status == "SCHEDULED" for k, j in jobs.items() if not k.startswith("tiktok"))
        assert db.query(Promotion).one().status == "PARTIAL_FAILURE"
    # retries limitados: nunca loop infinito
    for _ in range(10):
        due_all()
        orch.process_due_jobs(publisher=pub)
    with session_scope() as db:
        tk = db.query(PublicationJob).filter_by(network="tiktok").one()
        assert tk.status == "FAILED" and tk.attempts == tk.max_attempts == 4
        assert db.query(PublicationAttempt).filter_by(job_id=tk.id).count() == 4
        assert db.query(Promotion).one().status == "PARTIAL_FAILURE"
    assert pub.calls.count(("instagram", "feed")) == 1
    # retry manual independente, agora com sucesso
    tk_id = tk.id
    assert orch.retry_job(tk_id)
    orch.process_due_jobs(publisher=FakePublisher())
    with session_scope() as db:
        assert db.get(PublicationJob, tk_id).status == "SCHEDULED"
        assert db.query(Promotion).one().status == "SCHEDULED"


def test_non_retryable_fails_immediately(client):
    client.post("/api/promotions", json=promo(**HIGH))
    orch.process_queued_promotions()
    orch.process_due_jobs(publisher=FakePublisher(fail={"youtube": PublishError("HTTP 400 bad", retryable=False)}))
    with session_scope() as db:
        yt = db.query(PublicationJob).filter_by(network="youtube").one()
        assert yt.status == "FAILED" and yt.attempts == 1 and yt.error_class == "non_retryable"


def test_backoff_grows_and_is_capped():
    s = get_settings()
    vals = [orch.backoff_seconds(n, s) for n in range(1, 12)]
    assert vals[0] == 60 and vals[1] == 120 and vals == sorted(vals) and max(vals) == 3600


def test_status_sync_marks_published(client, monkeypatch):
    monkeypatch.setenv("NIKKI_PUBLISHER", "metricool")
    client.post("/api/promotions", json=promo(**HIGH))
    orch.process_queued_promotions()
    orch.process_due_jobs(publisher=FakePublisher())
    s = get_settings().model_copy(update={"publisher": "metricool"})
    assert orch.sync_external_status(s=s, publisher=FakePublisher()) == 6
    with session_scope() as db:
        assert all(j.status == "PUBLISHED" and j.public_url for j in db.query(PublicationJob).all())
        assert db.query(Promotion).one().status == "PUBLISHED"


def test_schedule_slots_spaced_per_network(client):
    client.post("/api/promotions", json=promo(**HIGH))
    client.post("/api/promotions", json=promo(**{**HIGH, "external_id": "ext-2", "product_name": "Elite Trainer Box Pokémon", "product_url": "https://www.amazon.com.br/dp/B0C7654321"}))
    orch.process_queued_promotions()
    with session_scope() as db:
        ig = sorted(j.scheduled_for for j in db.query(PublicationJob).filter_by(network="tiktok"))
        assert len(ig) == 2 and (ig[1] - ig[0]) >= timedelta(minutes=20)


def test_aggregate_builds_video_jobs(client):
    for i in range(3):
        client.post("/api/promotions", json=promo(**{**HIGH, "external_id": f"a{i}", "product_name": f"Produto Gamer Pokémon {i} Edição",
                                                     "product_url": f"https://www.amazon.com.br/dp/B0C000000{i}"}))
    orch.process_queued_promotions()
    agg_id = orch.build_aggregate("top_today")
    assert agg_id
    with session_scope() as db:
        jobs = db.query(PublicationJob).filter_by(aggregate_id=agg_id).all()
        assert {(j.network, j.format) for j in jobs} == {("instagram", "reel"), ("tiktok", "video"), ("youtube", "short"), ("facebook", "reel")}
    assert orch.build_aggregate("top_today") is None  # mesmas promoções não repetem


@pytest.mark.parametrize("statuses,expected", [
    ({"SCHEDULED", "PUBLISHED"}, "SCHEDULED"),
    ({"PUBLISHED"}, "PUBLISHED"),
    ({"FAILED"}, "FAILED"),
    ({"SCHEDULED", "FAILED"}, "PARTIAL_FAILURE"),
    ({"SCHEDULED", "RETRYING"}, "PARTIAL_FAILURE"),
    ({"RETRYING"}, "RETRYING"),
    ({"PENDING", "SCHEDULED"}, "SCHEDULING"),
])
def test_aggregate_status(statuses, expected):
    assert orch.aggregate_status(statuses) == expected


def test_api_rejects_garbage(client):
    assert client.post("/api/promotions", json={"foo": 1}).status_code == 422
    r = client.post("/api/promotions", json=promo(product_url="nope"))
    assert r.status_code == 202 and r.json()["status"] == "REJECTED"


def test_api_key_enforced(client, monkeypatch):
    from nikki.settings import reset_settings_cache

    monkeypatch.setenv("NIKKI_API_KEY", "segredo")
    reset_settings_cache()
    assert client.post("/api/promotions", json=promo()).status_code == 401
    assert client.post("/api/promotions", json=promo(), headers={"X-API-Key": "segredo"}).status_code == 202


def test_go_redirects(client, monkeypatch):
    from nikki.settings import reset_settings_cache

    r = client.get("/go/instagram", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("EXAMPLEinstagramPRIMARY01")
    monkeypatch.setenv("NIKKI_INSTAGRAM_ACTIVE_GROUP", "reserve")
    reset_settings_cache()
    assert client.get("/go/instagram", follow_redirects=False).headers["location"].endswith("EXAMPLEinstagramRESERVE01")
    assert client.get("/go/facebook", follow_redirects=False).headers["location"].endswith("EXAMPLEfacebookPRIMARY001")
    assert client.get("/go/tiktok", follow_redirects=False).headers["location"].endswith("EXAMPLEtiktokPRIMARY00001")
    assert client.get("/go/youtube", follow_redirects=False).headers["location"].endswith("EXAMPLEyoutubePRIMARY0001")
    assert client.get("/go/x", follow_redirects=False).status_code == 404
