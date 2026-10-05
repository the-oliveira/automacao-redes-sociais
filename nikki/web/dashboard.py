"""Painel local mínimo (HTML server-side, sem build)."""

from __future__ import annotations

from html import escape
from pathlib import Path

from sqlalchemy import func, select

from nikki import analytics
from nikki.db import ChannelDestination, ContentVariant, MediaAsset, Promotion, PublicationAttempt, PublicationJob, SystemEvent, session_scope
from nikki.integrations import canva
from nikki.routing import NETWORKS
from nikki.settings import get_settings

CSS = """
:root{--bg:#0e0a1a;--panel:#18122b;--line:#2c2348;--text:#ece8f7;--muted:#a59cc2;--ok:#3ddc97;--warn:#ffcc4d;--bad:#ff5c8a;--acc:#b05cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;gap:24px;align-items:center;flex-wrap:wrap}
header b{font-size:18px;color:var(--acc)}a{color:#c9a6ff}main{padding:16px 24px;max-width:1400px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
.card .n{font-size:24px;font-weight:700}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:10px;overflow:hidden}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase}
.b{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:700;margin:1px;background:#2c2348;white-space:nowrap}
.ok{background:#173d2d;color:var(--ok)}.warn{background:#3d3317;color:var(--warn)}.bad{background:#3d1726;color:var(--bad)}
pre{white-space:pre-wrap;background:#0b0816;padding:10px;border-radius:8px;border:1px solid var(--line);margin:6px 0}
img.thumb{max-width:260px;border-radius:8px;border:1px solid var(--line)}video{max-width:260px;border-radius:8px}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:3px 9px;cursor:pointer;font-size:12px}
h2{font-size:16px;margin:22px 0 10px}
@media(max-width:700px){main,header{padding:12px 16px}table{font-size:12px}}
"""

OK_ST = {"SCHEDULED", "PUBLISHED", "DRY_RUN", "REAL_OK", "QUEUED", "CONTENT_READY", "MEDIA_READY"}
WARN_ST = {"RETRYING", "PENDING", "SCHEDULING", "SKIPPED", "CONFIGURED", "REAL_TEST_PENDING", "MOCK", "PARTIAL_FAILURE"}
BAD_ST = {"FAILED", "REJECTED", "ERROR"}


def badge(text: str | None) -> str:
    t = text or "-"
    cls = "ok" if t in OK_ST else "bad" if t in BAD_ST else "warn" if t in WARN_ST else ""
    return f'<span class="b {cls}">{escape(t)}</span>'


def page(title: str, body: str) -> str:
    s = get_settings()
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style><meta http-equiv="refresh" content="30"></head><body>
<header><b>NIKKI • automação</b><a href="/">Painel</a><a href="/events">Eventos</a><a href="/api/status">API status</a>
<span class="muted">publisher: {badge(s.publisher)} grupo IG: {badge(s.instagram_active_group)} {"QA" if s.qa_mode else ""}</span></header>
<main>{body}</main></body></html>"""


def integration_states() -> dict[str, str]:
    s = get_settings()
    out: dict[str, str] = {}
    from nikki.integrations import oauth_store

    creds = {"instagram": ("meta", s.meta_app_id), "facebook": ("meta", s.meta_app_id),
             "youtube": ("google", s.google_client_id), "tiktok": ("tiktok", s.tiktok_client_key)}
    with session_scope() as db:
        for net in NETWORKS:
            if net not in s.enabled_networks:
                out[net] = "DISABLED"
                continue
            if s.publisher == "dry_run":
                out[net] = "MOCK"
                continue
            if s.publisher == "metricool" and not s.metricool_configured:
                out[net] = "FAILED"
                continue
            if s.publisher == "direct":
                provider, cred = creds[net]
                if not cred:
                    out[net] = "MOCK"
                    continue
                if not oauth_store.load_tokens(provider):
                    out[net] = "REAL_TEST_PENDING"
                    continue
            published = db.scalar(select(func.count()).where(PublicationJob.network == net, PublicationJob.status == "PUBLISHED",
                                                             PublicationJob.external_id.is_not(None)))
            scheduled = db.scalar(select(func.count()).where(PublicationJob.network == net, PublicationJob.status == "SCHEDULED"))
            last = db.scalar(select(PublicationJob).where(PublicationJob.network == net).order_by(PublicationJob.updated_at.desc()))
            if published:
                out[net] = "REAL_OK"
            elif last is not None and last.status == "FAILED":
                out[net] = "FAILED"
            elif scheduled:
                out[net] = "REAL_TEST_PENDING"
            else:
                out[net] = "CONFIGURED"
        canva_used = db.scalar(select(func.count()).where(MediaAsset.source == "canva"))
    if canva_used:
        out["canva"] = "REAL_OK"
    elif s.canva_configured:
        out["canva"] = "CONFIGURED" if canva.is_authorized() else "REAL_TEST_PENDING"
    else:
        out["canva"] = "MOCK"
    if s.publisher == "metricool":
        out["metricool"] = "REAL_OK" if any(out[n] == "REAL_OK" for n in NETWORKS) else "CONFIGURED"
    return out


def _jobs_cell(jobs: list[PublicationJob]) -> str:
    parts = []
    for j in jobs:
        link = f' <a href="{escape(j.public_url)}" target="_blank">↗</a>' if j.public_url else ""
        retry = (f'<form method="post" action="/ui/retry/job/{j.id}" style="display:inline"><button>retry</button></form>'
                 if j.status == "FAILED" else "")
        title = escape(j.last_error or "", quote=True)
        parts.append(f'<span title="{title}">{escape(j.network)}/{escape(j.format)} {badge(j.status)}</span>{link}{retry}')
    return "<br>".join(parts) or '<span class="muted">-</span>'


def home_page() -> str:
    with session_scope() as db:
        counts = dict(db.execute(select(Promotion.status, func.count()).group_by(Promotion.status)).all())
        jcounts = dict(db.execute(select(PublicationJob.status, func.count()).group_by(PublicationJob.status)).all())
        promos = db.scalars(select(Promotion).order_by(Promotion.id.desc()).limit(60)).all()
        jobs_by_promo: dict[int, list[PublicationJob]] = {}
        for j in db.scalars(select(PublicationJob).where(PublicationJob.promotion_id.in_([p.id for p in promos]))).all():
            jobs_by_promo.setdefault(j.promotion_id, []).append(j)
        agg_jobs = db.scalars(select(PublicationJob).where(PublicationJob.aggregate_id.is_not(None)).order_by(PublicationJob.id.desc()).limit(20)).all()
        retries = db.scalar(select(func.count()).select_from(PublicationAttempt).where(PublicationAttempt.success.is_(False)))
        dests = db.scalars(select(ChannelDestination).order_by(ChannelDestination.network, ChannelDestination.role)).all()
        errors = db.scalars(select(SystemEvent).where(SystemEvent.level == "error").order_by(SystemEvent.id.desc()).limit(8)).all()
        total = sum(counts.values())
        approved = total - counts.get("REJECTED", 0) - counts.get("SKIPPED", 0)
        cards = [("Recebidas", total), ("Aprovadas", approved), ("Rejeitadas", counts.get("REJECTED", 0)),
                 ("Score baixo", counts.get("SKIPPED", 0)), ("Posts agendados", jcounts.get("SCHEDULED", 0) + jcounts.get("DRY_RUN", 0)),
                 ("Publicados", jcounts.get("PUBLISHED", 0)), ("Jobs com falha", jcounts.get("FAILED", 0)), ("Tentativas falhas", retries)]
        html = ['<div class="grid">' + "".join(f'<div class="card"><div class="muted">{k}</div><div class="n">{v}</div></div>' for k, v in cards) + "</div>"]
        states = integration_states()
        html.append("<h2>Integrações</h2><div class='grid'>" + "".join(
            f"<div class='card'><div class='muted'>{k}</div>{badge(v)}</div>" for k, v in states.items()) + "</div>")
        html.append("<h2>Grupos WhatsApp</h2><table><tr><th>Rede</th><th>Papel</th><th>Ativo</th><th>Cliques /go</th><th>URL</th></tr>" + "".join(
            f"<tr><td>{d.network}</td><td>{d.role}</td><td>{badge('ATIVO') if d.active else ''}</td><td>{d.clicks}</td><td class='muted'>{escape(d.url)}</td></tr>" for d in dests) + "</table>")
        rows = []
        for p in promos:
            plan = " ".join(f"{n}:{'+'.join(f)}" for n, f in (p.plan or {}).items())
            rows.append(f"<tr><td><a href='/promotion/{p.id}'>#{p.id}</a></td><td>{escape(p.product_name[:70])}<div class='muted'>{escape(p.store)} • R$ {p.current_price:.2f}"
                        f"{(' • ' + escape(p.coupon)) if p.coupon else ''}</div></td><td>{p.score if p.score is not None else '-'}<div class='muted'>{p.tier or ''}</div></td>"
                        f"<td>{badge(p.status)}<div class='muted'>{escape((p.status_reason or '')[:90])}</div>{'<span class=muted>dup×' + str(p.duplicate_count) + '</span>' if p.duplicate_count else ''}</td>"
                        f"<td class='muted'>{escape(plan)}</td><td>{_jobs_cell(jobs_by_promo.get(p.id, []))}</td></tr>")
        html.append("<h2>Promoções</h2><table><tr><th>ID</th><th>Produto</th><th>Score</th><th>Status</th><th>Canais</th><th>Posts</th></tr>" + "".join(rows) + "</table>")
        if agg_jobs:
            html.append("<h2>Conteúdo agregado</h2><table><tr><th>Agregado</th><th>Posts</th></tr>" + "".join(
                f"<tr><td>#{j.aggregate_id}</td><td>{_jobs_cell([j])}</td></tr>" for j in agg_jobs) + "</table>")
        summ = analytics.summary()
        if summ:
            keys = sorted({k for v in summ.values() for k in v})
            html.append("<h2>Analytics (Metricool)</h2><table><tr><th>Rede</th>" + "".join(f"<th>{k}</th>" for k in keys) + "</tr>" + "".join(
                f"<tr><td>{n}</td>" + "".join(f"<td>{int(v.get(k, 0))}</td>" for k in keys) + "</tr>" for n, v in summ.items()) + "</table>")
        if errors:
            html.append("<h2>Últimos erros</h2><table>" + "".join(
                f"<tr><td class='muted'>{e.ts:%d/%m %H:%M}</td><td>{escape(e.event)}</td><td><pre>{escape(str(e.data)[:400])}</pre></td></tr>" for e in errors) + "</table>")
    return page("Nikki • Painel", "".join(html))


def _media_url(path: str) -> str:
    root = get_settings().media_dir
    try:
        rel = Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return "/media/" + rel.as_posix()


def promotion_page(pid: int) -> str:
    with session_scope() as db:
        p = db.get(Promotion, pid)
        if not p:
            return page("404", "<h2>Promoção não encontrada</h2>")
        variants = db.scalars(select(ContentVariant).where(ContentVariant.promotion_id == pid)).all()
        media = db.scalars(select(MediaAsset).where(MediaAsset.promotion_id == pid)).all()
        jobs = db.scalars(select(PublicationJob).where(PublicationJob.promotion_id == pid)).all()
        events = db.scalars(select(SystemEvent).where(SystemEvent.correlation_id == p.correlation_id).order_by(SystemEvent.id)).all()
        h = [f"<h2>#{p.id} {escape(p.product_name)}</h2><p>{badge(p.status)} score <b>{p.score}</b> ({p.tier}) • cid <code>{p.correlation_id}</code> "
             f"<form method='post' action='/ui/retry/promotion/{p.id}' style='display:inline'><button>reprocessar falhas</button></form></p>",
             f"<p class='muted'>{escape(p.status_reason or '')}</p>",
             f"<pre>{escape(str(p.score_breakdown))}</pre>", "<h2>Mídia</h2><div class='grid'>"]
        for m in media:
            url = _media_url(m.local_path)
            tag = f"<video src='{url}' controls></video>" if m.kind == "video" else f"<img class='thumb' src='{url}'>"
            h.append(f"<div class='card'>{tag}<div class='muted'>{m.kind} • {m.source}</div></div>")
        h.append("</div><h2>Posts</h2><table><tr><th>Rede</th><th>Status</th><th>Agendado</th><th>Grupo</th><th>Texto</th></tr>")
        vmap = {v.id: v for v in variants}
        for j in jobs:
            v = vmap.get(j.variant_id)
            h.append(f"<tr><td>{j.network}/{j.format}</td><td>{_jobs_cell([j])}<div class='muted'>tentativas {j.attempts}/{j.max_attempts}</div>"
                     f"<div class='muted'>{escape(j.last_error or '')[:300]}</div></td><td>{j.scheduled_for}</td>"
                     f"<td>{escape(v.group_key if v else '')}</td><td>{('<b>' + escape(v.title) + '</b>') if v and v.title else ''}<pre>{escape(v.caption if v else '')}</pre></td></tr>")
        h.append("</table><h2>Eventos</h2><table>" + "".join(
            f"<tr><td class='muted'>{e.ts:%d/%m %H:%M:%S}</td><td>{badge(e.level.upper()) if e.level != 'info' else ''} {escape(e.event)}</td><td class='muted'>{escape(str(e.data or '')[:300])}</td></tr>" for e in events) + "</table>")
    return page(f"Nikki • #{pid}", "".join(h))


def events_page() -> str:
    with session_scope() as db:
        evs = db.scalars(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(300)).all()
        rows = "".join(f"<tr><td class='muted'>{e.ts:%d/%m %H:%M:%S}</td><td>{escape(e.level)}</td><td>{escape(e.event)}</td><td>{e.promotion_id or ''}</td>"
                       f"<td class='muted'>{escape(e.correlation_id or '')}</td><td class='muted'>{escape(str(e.data or '')[:300])}</td></tr>" for e in evs)
    return page("Nikki • Eventos", f"<h2>Eventos</h2><table><tr><th>Quando (UTC)</th><th>Nível</th><th>Evento</th><th>Promo</th><th>CID</th><th>Dados</th></tr>{rows}</table>")
