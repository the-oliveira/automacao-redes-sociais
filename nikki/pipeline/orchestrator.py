"""Worker: processa a fila persistida no banco (sem Redis/Celery).

Etapas por promoção: score -> canais -> copy -> mídia -> jobs (1 por rede/formato).
Jobs são independentes: falha no TikTok não afeta Instagram; retry com backoff limitado.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nikki.db import (
    AggregatePost,
    ContentVariant,
    MediaAsset,
    Promotion,
    PublicationAttempt,
    PublicationJob,
    kv_get,
    kv_set,
    session_scope,
    utcnow,
)
from nikki.events import log_event, new_correlation_id
from nikki.integrations.metricool import PublishError
from nikki.media import render
from nikki.media.service import generate_aggregate_video, generate_promotion_media, kind_for
from nikki.pipeline.copy import build_aggregate_content, build_content
from nikki.pipeline.scoring import compute_score, tier_for
from nikki.publishing import Publisher, get_publisher
from nikki.settings import Settings, get_settings

# Status de job
J_PENDING, J_RETRYING, J_SCHEDULED, J_PUBLISHED, J_FAILED, J_DRY = "PENDING", "RETRYING", "SCHEDULED", "PUBLISHED", "FAILED", "DRY_RUN"
J_OK = {J_SCHEDULED, J_PUBLISHED, J_DRY}
J_OPEN = {J_PENDING, J_RETRYING}


def local_now(s: Settings) -> datetime:
    return datetime.now(ZoneInfo(s.timezone)).replace(tzinfo=None)


# ------------------------------------------------------------ planejamento


def next_slot(db: Session, network: str, s: Settings, taken: dict[str, datetime]) -> datetime:
    pub = s.rules()["publishing"]
    earliest = local_now(s) + timedelta(minutes=pub["schedule_offset_minutes"])
    gap = timedelta(minutes=pub["min_gap_minutes_per_network"])
    last = taken.get(network) or db.scalar(
        select(func.max(PublicationJob.scheduled_for)).where(
            PublicationJob.network == network, PublicationJob.status.in_(list(J_OK | J_OPEN)))
    )
    slot = max(earliest, last + gap) if last else earliest
    taken[network] = slot
    return slot.replace(microsecond=0)


def local_to_utc(local: datetime, s: Settings) -> datetime:
    return local.replace(tzinfo=ZoneInfo(s.timezone)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def first_attempt_at(slot: datetime, s: Settings) -> datetime:
    """Metricool/dry-run recebem o agendamento já; o publisher direto só roda no horário do slot."""
    return local_to_utc(slot, s) if s.publisher == "direct" else utcnow()


def _create_jobs(db: Session, *, cid: str, variants: list[ContentVariant], media_by_kind: dict[str, MediaAsset],
                 s: Settings, promotion_id: int | None = None, aggregate_id: int | None = None) -> list[PublicationJob]:
    taken: dict[str, datetime] = {}
    jobs = []
    max_attempts = s.rules()["publishing"]["max_attempts"]
    for v in variants:
        exists = db.scalar(select(PublicationJob).where(
            PublicationJob.promotion_id.is_(promotion_id) if promotion_id is None else PublicationJob.promotion_id == promotion_id,
            PublicationJob.aggregate_id.is_(aggregate_id) if aggregate_id is None else PublicationJob.aggregate_id == aggregate_id,
            PublicationJob.network == v.network, PublicationJob.format == v.format))
        if exists:
            continue  # idempotência: nunca dois jobs para o mesmo alvo
        kind = kind_for(v.network, v.format)
        media = media_by_kind.get(kind)
        slot = next_slot(db, v.network, s, taken)
        job = PublicationJob(correlation_id=cid, promotion_id=promotion_id, aggregate_id=aggregate_id, variant_id=v.id,
                             media_ids=[media.id] if media else [], network=v.network, format=v.format,
                             status=J_PENDING, max_attempts=max_attempts, next_attempt_at=first_attempt_at(slot, s),
                             scheduled_for=slot)
        db.add(job)
        jobs.append(job)
    db.flush()
    return jobs


def process_promotion(db: Session, p: Promotion, s: Settings | None = None) -> None:
    s = s or get_settings()
    rules = s.rules()
    result = compute_score(p, rules)
    p.score, p.score_breakdown = result.score, result.as_dict()
    p.tier, full_plan = tier_for(result.score, rules)
    plan = {net: fmts for net, fmts in full_plan.items() if net in s.enabled_networks}
    p.plan = plan
    log_event(db, "score_calculated", correlation_id=p.correlation_id, promotion_id=p.id, score=p.score, tier=p.tier)
    if not plan:
        p.status = "SKIPPED"
        p.status_reason = (f"score {p.score} abaixo do mínimo para conteúdo social" if not full_plan
                           else f"redes do tier {p.tier} desativadas em NIKKI_NETWORKS")
        log_event(db, "promotion_skipped", correlation_id=p.correlation_id, promotion_id=p.id, score=p.score)
        return
    log_event(db, "channels_selected", correlation_id=p.correlation_id, promotion_id=p.id, plan=plan)

    drafts = build_content(p, plan, hot=p.tier == "high", settings=s)
    variants = []
    for d in drafts:
        v = ContentVariant(promotion_id=p.id, network=d.network, format=d.format, title=d.title, caption=d.caption,
                           group_key=d.group.key, group_url=d.group.cta_url, extra=d.extra)
        db.add(v)
        variants.append(v)
    db.flush()
    p.status = "CONTENT_READY"
    log_event(db, "content_created", correlation_id=p.correlation_id, promotion_id=p.id, variants=len(variants))

    try:
        media = generate_promotion_media(db, p, plan, s)
    except render.InvalidImage as e:
        p.status, p.status_reason = "REJECTED", f"imagem inválida: {e}"
        log_event(db, "promotion_rejected", level="warning", correlation_id=p.correlation_id, promotion_id=p.id, reason=str(e))
        return
    p.status = "MEDIA_READY"
    jobs = _create_jobs(db, cid=p.correlation_id, variants=variants, media_by_kind=media, s=s, promotion_id=p.id)
    p.status = "SCHEDULING"
    log_event(db, "jobs_created", correlation_id=p.correlation_id, promotion_id=p.id,
              jobs=[f"{j.network}/{j.format}@{j.scheduled_for:%H:%M}" for j in jobs])


def process_queued_promotions(limit: int = 5, s: Settings | None = None) -> int:
    s = s or get_settings()
    with session_scope() as db:
        ids = db.scalars(select(Promotion.id).where(Promotion.status == "QUEUED").order_by(Promotion.id).limit(limit)).all()
    for pid in ids:
        with session_scope() as db:
            p = db.get(Promotion, pid)
            if p is None or p.status != "QUEUED":
                continue
            try:
                process_promotion(db, p, s)
            except Exception as e:  # noqa: BLE001 - uma promoção ruim não derruba o worker
                db.rollback()
                p = db.get(Promotion, pid)
                p.status, p.status_reason = "FAILED", f"erro no pipeline: {e}"[:1000]
                log_event(db, "pipeline_failed", level="error", correlation_id=p.correlation_id, promotion_id=pid, error=str(e)[:500])
    return len(ids)


# ------------------------------------------------------------ publicação


def backoff_seconds(attempt: int, s: Settings) -> int:
    pub = s.rules()["publishing"]
    return int(min(pub["backoff_base_seconds"] * (2 ** max(0, attempt - 1)), pub["backoff_max_seconds"]))


def run_job(db: Session, job: PublicationJob, publisher: Publisher, s: Settings) -> None:
    variant = db.get(ContentVariant, job.variant_id)
    media = [db.get(MediaAsset, mid) for mid in (job.media_ids or [])]
    job.attempts += 1
    attempt = PublicationAttempt(job_id=job.id, attempt_no=job.attempts)
    db.add(attempt)
    try:
        if s.publisher != "direct" and job.scheduled_for and job.scheduled_for < local_now(s) + timedelta(minutes=1):
            job.scheduled_for = (local_now(s) + timedelta(minutes=s.rules()["publishing"]["schedule_offset_minutes"])).replace(microsecond=0)
        res = publisher.publish(db, job, variant, [m for m in media if m])
    except Exception as e:  # noqa: BLE001
        retryable = getattr(e, "retryable", True) if isinstance(e, PublishError) else True
        attempt.success, attempt.retryable, attempt.error = False, retryable, str(e)[:2000]
        attempt.response = getattr(e, "response", None)
        attempt.finished_at = utcnow()
        job.last_error, job.error_class = str(e)[:2000], "retryable" if retryable else "non_retryable"
        if retryable and job.attempts < job.max_attempts:
            job.status = J_RETRYING
            job.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds(job.attempts, s))
            log_event(db, f"{job.network}_retry", level="warning", correlation_id=job.correlation_id,
                      promotion_id=job.promotion_id, job_id=job.id, attempt=job.attempts, error=str(e)[:300])
        else:
            job.status, job.next_attempt_at = J_FAILED, None
            log_event(db, f"{job.network}_failed", level="error", correlation_id=job.correlation_id,
                      promotion_id=job.promotion_id, job_id=job.id, attempt=job.attempts, retryable=retryable, error=str(e)[:300])
        return
    attempt.success, attempt.finished_at = True, utcnow()
    attempt.response = {"id": res.external_id, "status": res.status}
    job.external_id, job.external_uuid, job.external_status, job.public_url = res.external_id, res.uuid, res.status, res.public_url
    job.status = J_DRY if res.status == "DRY_RUN" else (J_PUBLISHED if res.status == "PUBLISHED" else J_SCHEDULED)
    job.last_error, job.error_class, job.next_attempt_at = None, None, None
    log_event(db, f"{job.network}_scheduled", correlation_id=job.correlation_id, promotion_id=job.promotion_id,
              job_id=job.id, format=job.format, external_id=res.external_id, at=str(job.scheduled_for), publisher=publisher.name)


def aggregate_status(st: set[str]) -> str:
    """Status da promoção a partir dos status dos seus jobs."""
    bad = st & {J_FAILED, J_RETRYING}
    if st == {J_FAILED}:
        return "FAILED"
    if bad and (st & J_OK or J_FAILED in st):
        return "PARTIAL_FAILURE"
    if bad:
        return "RETRYING"
    if J_PENDING in st:
        return "SCHEDULING"
    if st <= {J_PUBLISHED}:
        return "PUBLISHED"
    return "SCHEDULED"


def refresh_parent_status(db: Session, job: PublicationJob) -> None:
    if job.promotion_id:
        parent: Any = db.get(Promotion, job.promotion_id)
        jobs = db.scalars(select(PublicationJob).where(PublicationJob.promotion_id == job.promotion_id)).all()
    else:
        parent = db.get(AggregatePost, job.aggregate_id)
        jobs = db.scalars(select(PublicationJob).where(PublicationJob.aggregate_id == job.aggregate_id)).all()
    if parent is None or not jobs:
        return
    new = aggregate_status({j.status for j in jobs})
    if parent.status != new:
        parent.status = new


def process_due_jobs(limit: int = 10, s: Settings | None = None, publisher: Publisher | None = None) -> int:
    s = s or get_settings()
    publisher = publisher or get_publisher(s)
    with session_scope() as db:
        ids = db.scalars(select(PublicationJob.id).where(
            PublicationJob.status.in_(list(J_OPEN)), PublicationJob.next_attempt_at <= utcnow()
        ).order_by(PublicationJob.id).limit(limit)).all()
    for jid in ids:
        with session_scope() as db:
            job = db.get(PublicationJob, jid)
            if job is None or job.status not in J_OPEN:
                continue
            run_job(db, job, publisher, s)
            db.flush()
            refresh_parent_status(db, job)
    return len(ids)


def sync_external_status(s: Settings | None = None, publisher: Publisher | None = None) -> int:
    """Consulta o Metricool para jobs agendados: PUBLISHED + URL pública, ou ERROR -> retry."""
    s = s or get_settings()
    if s.publisher not in ("metricool", "direct"):
        return 0
    publisher = publisher or get_publisher(s)
    with session_scope() as db:
        ids = db.scalars(select(PublicationJob.id).where(
            PublicationJob.status == J_SCHEDULED, PublicationJob.external_id.is_not(None))).all()
    changed = 0
    for jid in ids:
        with session_scope() as db:
            job = db.get(PublicationJob, jid)
            try:
                st = publisher.status(job.external_id, job.network) if s.publisher == "direct" else publisher.status(job.external_id)
            except Exception as e:  # noqa: BLE001
                log_event(db, "status_sync_failed", level="warning", job_id=jid, error=str(e)[:300])
                continue
            ext = st.get("status")
            job.external_status = ext
            if st.get("public_url"):
                job.public_url = st["public_url"]
            if ext == "PUBLISHED":
                job.status = J_PUBLISHED
                log_event(db, f"{job.network}_success", correlation_id=job.correlation_id, promotion_id=job.promotion_id,
                          job_id=jid, url=job.public_url)
                changed += 1
            elif ext == "ERROR":
                job.last_error = f"Metricool reportou ERROR: {st.get('detail')}"
                job.error_class = "retryable"
                if job.attempts < job.max_attempts:
                    if s.publisher == "metricool":
                        try:
                            publisher.client.delete_post(job.external_id)  # type: ignore[attr-defined]
                        except Exception:  # noqa: BLE001 - post com erro pode já ter sumido
                            pass
                    job.status, job.external_id = J_RETRYING, None
                    job.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds(job.attempts, s))
                    log_event(db, f"{job.network}_retry", level="warning", correlation_id=job.correlation_id,
                              promotion_id=job.promotion_id, job_id=jid, reason=job.last_error)
                else:
                    job.status = J_FAILED
                    log_event(db, f"{job.network}_failed", level="error", correlation_id=job.correlation_id,
                              promotion_id=job.promotion_id, job_id=jid, reason=job.last_error)
                changed += 1
            db.flush()
            refresh_parent_status(db, job)
    return changed


def retry_job(job_id: int) -> bool:
    """Reprocessamento manual (painel/CLI) de um job FAILED."""
    with session_scope() as db:
        job = db.get(PublicationJob, job_id)
        if job is None or job.status != J_FAILED:
            return False
        job.status, job.next_attempt_at = J_RETRYING, utcnow()
        job.max_attempts = max(job.max_attempts, job.attempts + 1)
        log_event(db, "manual_retry", correlation_id=job.correlation_id, promotion_id=job.promotion_id, job_id=job_id)
        refresh_parent_status(db, job)
        return True


def retry_promotion(promotion_id: int) -> int:
    with session_scope() as db:
        p = db.get(Promotion, promotion_id)
        if p is None:
            return 0
        if p.status == "FAILED" and not p.jobs:
            p.status, p.status_reason = "QUEUED", None
            log_event(db, "manual_reprocess", correlation_id=p.correlation_id, promotion_id=p.id)
            return 1
        ids = [j.id for j in p.jobs if j.status == J_FAILED]
    return sum(retry_job(j) for j in ids)


# ------------------------------------------------------------ agregados

AGG_KIND_FILTERS = {
    "tcg": lambda p: "tcg" in (p.category or "") or "pokemon" in p.product_name.lower() or "booster" in p.product_name.lower(),
    "gamer_geek": lambda p: any(k in (p.category or "") for k in ("game", "gamer", "geek", "anime", "colecion")),
    "price_drops": lambda p: (p.discount_percent or 0) >= 25,
}


def build_aggregate(kind: str, s: Settings | None = None, force: bool = False) -> int | None:
    s = s or get_settings()
    cfg = s.rules()["aggregate"]
    since = utcnow() - timedelta(hours=cfg["lookback_hours"])
    with session_scope() as db:
        used: set[int] = set()
        for a in db.scalars(select(AggregatePost).where(AggregatePost.kind == kind, AggregatePost.created_at >= since)).all():
            used |= set(a.promotion_ids)
        cands = db.scalars(select(Promotion).where(
            Promotion.created_at >= since, Promotion.score >= cfg["min_score"],
            Promotion.status.notin_(["REJECTED", "QUEUED", "FAILED"])).order_by(Promotion.score.desc())).all()
        flt = AGG_KIND_FILTERS.get(kind, lambda p: True)
        picked = [p for p in cands if p.id not in used and flt(p)][: cfg["max_items"]]
        if len(picked) < (1 if force else cfg["min_items"]):
            log_event(db, "aggregate_skipped", kind=kind, candidates=len(picked))
            return None
        cid = new_correlation_id()
        from nikki.pipeline.copy import AGGREGATE_TITLES

        agg = AggregatePost(correlation_id=cid, kind=kind, title=AGGREGATE_TITLES[kind].format(n=len(picked)),
                            promotion_ids=[p.id for p in picked], status="CONTENT_READY")
        db.add(agg)
        db.flush()
        variants = []
        for net, fmts in cfg["networks"].items():
            if net not in s.enabled_networks:
                continue
            for fmt in fmts:
                d = build_aggregate_content(kind, picked, net, fmt, agg.id, s)
                v = ContentVariant(aggregate_id=agg.id, network=net, format=fmt, title=d.title, caption=d.caption,
                                   group_key=d.group.key, group_url=d.group.cta_url, extra=d.extra)
                db.add(v)
                variants.append(v)
        db.flush()
        log_event(db, "aggregate_content_created", correlation_id=cid, aggregate_id=agg.id, kind=kind, items=len(picked))
        video_asset = generate_aggregate_video(db, agg.id, agg.title, picked, cid, s)
        agg.status = "MEDIA_READY"
        _create_jobs(db, cid=cid, variants=variants, media_by_kind={"video": video_asset}, s=s, aggregate_id=agg.id)
        agg.status = "SCHEDULING"
        return agg.id


def run_aggregate_schedule(s: Settings | None = None) -> list[int]:
    s = s or get_settings()
    now = local_now(s)
    made = []
    for slot in s.rules()["aggregate"].get("schedule", []):
        hh, mm = map(int, slot["at"].split(":"))
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < due or now > due + timedelta(hours=2):
            continue
        key = f"agg:{slot['kind']}:{slot['at']}"
        with session_scope() as db:
            last = (kv_get(db, key) or {}).get("date")
            if last == now.date().isoformat():
                continue
            kv_set(db, key, {"date": now.date().isoformat()})
        agg_id = build_aggregate(slot["kind"], s)
        if agg_id:
            made.append(agg_id)
    return made
