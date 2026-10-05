"""FastAPI: entrada de promoções, redirecionadores /go, OAuth Canva e painel local."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import func, select

from nikki import analytics
from nikki.db import ChannelDestination, Promotion, PublicationJob, init_engine, session_scope
from nikki.events import log_event
from nikki.integrations import canva
from nikki.pipeline import orchestrator
from nikki.pipeline.intake import PromotionIn, ingest
from nikki.routing import RoutingError, destination_for
from nikki.settings import get_settings
from nikki.web import dashboard
from nikki.worker import worker


def sync_destinations() -> None:
    s = get_settings()
    with session_scope() as db:
        for net, roles in s.whatsapp_groups().items():
            active_role = destination_for(net, s).role
            for role, url in roles.items():
                row = db.scalar(select(ChannelDestination).where(ChannelDestination.network == net, ChannelDestination.role == role))
                if row is None:
                    row = ChannelDestination(network=net, role=role, url=url)
                    db.add(row)
                row.url, row.active = url, role == active_role


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine()
    sync_destinations()
    s = get_settings()
    with session_scope() as db:
        log_event(db, "service_started", publisher=s.publisher, renderer=s.renderer,
                  instagram_group=s.instagram_active_group, qa_mode=s.qa_mode)
    if s.env != "test":
        worker.start()
    yield
    worker.stop()


app = FastAPI(title="Nikki Automation", version="1.0", lifespan=lifespan)
get_settings().media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(get_settings().media_dir)), name="media")


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    key = get_settings().api_key
    if key and x_api_key != key:
        raise HTTPException(401, "X-API-Key inválida")


# ------------------------------------------------------------------ API


@app.post("/api/promotions", status_code=202, dependencies=[Depends(require_key)])
async def create_promotion(request: Request) -> JSONResponse:
    """Recebe uma promoção normalizada. Processamento e publicação acontecem no worker."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON inválido") from None
    items = body if isinstance(body, list) else [body]
    results = []
    for item in items:
        try:
            PromotionIn.model_validate(item)
        except ValidationError as e:
            results.append({"status": "INVALID", "errors": e.errors(include_url=False, include_context=False)})
            continue
        with session_scope() as db:
            r = ingest(db, item)
        results.append({"promotion_id": r.promotion_id, "status": r.status, "correlation_id": r.correlation_id,
                        "reason": r.reason, "duplicate_of": r.duplicate_of})
    code = 422 if all(r["status"] == "INVALID" for r in results) else 202
    return JSONResponse(results if isinstance(body, list) else results[0], status_code=code)


@app.get("/api/promotions/{pid}")
def get_promotion(pid: int) -> dict[str, Any]:
    with session_scope() as db:
        p = db.get(Promotion, pid)
        if not p:
            raise HTTPException(404)
        jobs = db.scalars(select(PublicationJob).where(PublicationJob.promotion_id == pid)).all()
        return {
            "id": p.id, "status": p.status, "reason": p.status_reason, "score": p.score, "tier": p.tier, "plan": p.plan,
            "correlation_id": p.correlation_id, "duplicates": p.duplicate_count,
            "jobs": [{"id": j.id, "network": j.network, "format": j.format, "status": j.status, "attempts": j.attempts,
                      "scheduled_for": str(j.scheduled_for), "external_id": j.external_id, "public_url": j.public_url,
                      "error": j.last_error} for j in jobs],
        }


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    with session_scope() as db:
        promos = dict(db.execute(select(Promotion.status, func.count()).group_by(Promotion.status)).all())
        jobs = dict(db.execute(select(PublicationJob.status, func.count()).group_by(PublicationJob.status)).all())
    s = get_settings()
    return {"ok": True, "worker_alive": worker.alive, "worker_last_error": worker.last_error,
            "publisher": s.publisher, "instagram_active_group": s.instagram_active_group,
            "promotions": promos, "jobs": jobs, "integrations": dashboard.integration_states()}


@app.post("/api/jobs/{jid}/retry")
def api_retry_job(jid: int):
    if not orchestrator.retry_job(jid):
        raise HTTPException(409, "job não está FAILED")
    return {"ok": True}


@app.post("/api/promotions/{pid}/retry")
def api_retry_promotion(pid: int):
    return {"requeued": orchestrator.retry_promotion(pid)}


@app.post("/api/aggregates/{kind}")
def api_aggregate(kind: str, force: bool = False):
    agg_id = orchestrator.build_aggregate(kind, force=force)
    return {"aggregate_id": agg_id}


@app.post("/api/analytics/collect")
def api_analytics_collect(days: int = 14):
    return analytics.collect(days)


# ------------------------------------------------------------ redirect


@app.get("/go/{network}")
def go(network: str):
    try:
        dest = destination_for(network)
    except RoutingError:
        raise HTTPException(404, "rede desconhecida") from None
    with session_scope() as db:
        row = db.scalar(select(ChannelDestination).where(ChannelDestination.network == dest.network, ChannelDestination.role == dest.role))
        if row:
            row.clicks += 1
        log_event(db, "group_redirect", network=dest.network, role=dest.role)
    return RedirectResponse(dest.whatsapp_url, status_code=302)


# --------------------------------------------------------------- canva


@app.get("/oauth/canva/start")
def canva_start():
    if not get_settings().canva_configured:
        raise HTTPException(400, "Defina NIKKI_CANVA_CLIENT_ID e NIKKI_CANVA_CLIENT_SECRET no .env")
    return RedirectResponse(canva.authorization_url())


@app.get("/oauth/canva/callback")
def canva_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code:
        return HTMLResponse(f"<h1>Canva recusou: {error}</h1>", status_code=400)
    canva.exchange_code(code, state or "")
    with session_scope() as db:
        log_event(db, "canva_authorized")
    return HTMLResponse("<h1>Canva autorizado ✅</h1><p>Pode fechar esta aba.</p>")


# ------------------------------------------------- meta / google / tiktok

def _oauth_module(provider: str):
    from nikki.integrations import meta, tiktok, youtube

    mods = {"meta": (meta, "meta_app_id", "NIKKI_META_APP_ID/SECRET"),
            "google": (youtube, "google_client_id", "NIKKI_GOOGLE_CLIENT_ID/SECRET"),
            "tiktok": (tiktok, "tiktok_client_key", "NIKKI_TIKTOK_CLIENT_KEY/SECRET")}
    if provider not in mods:
        raise HTTPException(404)
    return mods[provider]


@app.get("/oauth/{provider}/start")
def oauth_start(provider: str):
    mod, key, env = _oauth_module(provider)
    if not getattr(get_settings(), key):
        raise HTTPException(400, f"Defina {env} no .env")
    return RedirectResponse(mod.authorization_url())


@app.get("/oauth/{provider}/callback")
@app.get("/oauth/{provider}/callback/")
def oauth_callback(provider: str, code: str | None = None, state: str | None = None, error: str | None = None,
                   error_description: str | None = None):
    mod, _, _ = _oauth_module(provider)
    if error or not code:
        return HTMLResponse(f"<h1>{provider} recusou: {error} {error_description or ''}</h1>", status_code=400)
    try:
        info = mod.exchange_code(code, state or "")
    except Exception as e:  # noqa: BLE001
        with session_scope() as db:
            log_event(db, f"{provider}_oauth_failed", level="error", error=str(e)[:400])
        return HTMLResponse(f"<h1>Falha ao autorizar {provider}</h1><pre>{e}</pre>", status_code=400)
    with session_scope() as db:
        log_event(db, f"{provider}_authorized", info=info or {})
    return HTMLResponse(f"<h1>{provider} autorizado ✅</h1><pre>{info or ''}</pre><p>Pode fechar esta aba.</p>")


# --------------------------------------------------------------- painel


@app.get("/", response_class=HTMLResponse)
def home():
    return dashboard.home_page()


@app.get("/promotion/{pid}", response_class=HTMLResponse)
def promotion_page(pid: int):
    return dashboard.promotion_page(pid)


@app.get("/events", response_class=HTMLResponse)
def events_page():
    return dashboard.events_page()


@app.post("/ui/retry/job/{jid}")
def ui_retry_job(jid: int):
    orchestrator.retry_job(jid)
    return RedirectResponse("/", status_code=303)


@app.post("/ui/retry/promotion/{pid}")
def ui_retry_promotion(pid: int):
    orchestrator.retry_promotion(pid)
    return RedirectResponse(f"/promotion/{pid}", status_code=303)

