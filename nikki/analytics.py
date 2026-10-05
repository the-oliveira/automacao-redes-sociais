"""Coleta de métricas reais via Metricool (nada é estimado ou inventado)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from nikki.db import AnalyticsSnapshot, session_scope
from nikki.events import log_event
from nikki.integrations.metricool import MetricoolClient
from nikki.settings import Settings, get_settings

METRIC_KEYS = ("likes", "comments", "shares", "saved", "reach", "impressions", "impressionsTotal", "views",
               "videoViewsTotal", "interactions", "engagement", "clicks", "viewCount", "likeCount", "commentCount")


def _post_id(item: dict[str, Any]) -> str | None:
    for k in ("postId", "id", "videoId", "mediaId", "reelId", "storyId"):
        if item.get(k):
            return str(item[k])
    return None


def _direct_items(net: str, s: Settings) -> list[dict[str, Any]]:
    """Métricas direto das APIs oficiais (modo publisher=direct)."""
    if net in ("instagram", "facebook"):
        from nikki.integrations.meta import MetaClient

        c = MetaClient(s)
        if net == "instagram":
            return [{"postId": m["id"], "url": m.get("permalink"), "publishedAt": m.get("timestamp"),
                     "likes": m.get("like_count", 0), "comments": m.get("comments_count", 0)} for m in c.ig_recent_media()]
        return [{"postId": p["id"], "url": p.get("permalink_url"), "publishedAt": p.get("created_time"),
                 "likes": ((p.get("reactions") or {}).get("summary") or {}).get("total_count", 0),
                 "comments": ((p.get("comments") or {}).get("summary") or {}).get("total_count", 0),
                 "shares": (p.get("shares") or {}).get("count", 0)} for p in c.fb_recent_posts()]
    if net == "youtube":
        from nikki.integrations.youtube import YouTubeClient

        return [{"postId": v["id"], "url": f"https://www.youtube.com/shorts/{v['id']}", "publishedAt": v["snippet"].get("publishedAt"),
                 **{k: int(x) for k, x in v.get("statistics", {}).items() if k in ("viewCount", "likeCount", "commentCount")}}
                for v in YouTubeClient(s).recent_videos()]
    from nikki.integrations.tiktok import TikTokClient

    return [{"postId": v["id"], "url": v.get("share_url"), "publishedAt": v.get("create_time"), "views": v.get("view_count", 0),
             "likes": v.get("like_count", 0), "comments": v.get("comment_count", 0), "shares": v.get("share_count", 0)}
            for v in TikTokClient(s).recent_videos()]


def collect(days: int = 14, settings: Settings | None = None) -> dict[str, int]:
    s = settings or get_settings()
    if s.publisher == "direct":
        return _collect_direct(s)
    if not s.metricool_configured:
        return {}
    c = MetricoolClient(s)
    end = datetime.now()
    start = end - timedelta(days=days)
    counts: dict[str, int] = {}
    for net in ("instagram", "facebook", "tiktok", "youtube"):
        try:
            items = c.youtube_analytics(start, end) if net == "youtube" else c.analytics(net, start, end)
        except Exception as e:  # noqa: BLE001
            with session_scope() as db:
                log_event(db, "analytics_failed", level="warning", network=net, error=str(e)[:300])
            continue
        with session_scope() as db:
            for it in items:
                pid = _post_id(it)
                if not pid:
                    continue
                metrics = {k: it[k] for k in METRIC_KEYS if isinstance(it.get(k), (int, float))}
                row = db.scalar(select(AnalyticsSnapshot).where(AnalyticsSnapshot.network == net, AnalyticsSnapshot.post_id == pid))
                pub = it.get("publishedAt") or it.get("created") or it.get("publicationDate")
                if isinstance(pub, dict):
                    pub = pub.get("dateTime")
                if row:
                    row.metrics, row.url = metrics, it.get("url") or it.get("link") or row.url
                else:
                    db.add(AnalyticsSnapshot(network=net, post_id=pid, url=it.get("url") or it.get("link"),
                                             published_at=str(pub) if pub else None, metrics=metrics))
            counts[net] = len(items)
            log_event(db, "analytics_collected", network=net, posts=len(items))
    return counts


def _store(net: str, items: list[dict[str, Any]]) -> None:
    with session_scope() as db:
        for it in items:
            pid = _post_id(it)
            if not pid:
                continue
            metrics = {k: it[k] for k in METRIC_KEYS if isinstance(it.get(k), (int, float))}
            row = db.scalar(select(AnalyticsSnapshot).where(AnalyticsSnapshot.network == net, AnalyticsSnapshot.post_id == pid))
            if row:
                row.metrics, row.url = metrics, it.get("url") or row.url
            else:
                db.add(AnalyticsSnapshot(network=net, post_id=pid, url=it.get("url"), published_at=str(it.get("publishedAt") or ""),
                                         metrics=metrics))
        log_event(db, "analytics_collected", network=net, posts=len(items))


def _collect_direct(s: Settings) -> dict[str, int]:
    counts: dict[str, int] = {}
    for net in s.enabled_networks:
        try:
            items = _direct_items(net, s)
        except Exception as e:  # noqa: BLE001 - rede não autorizada não impede as outras
            with session_scope() as db:
                log_event(db, "analytics_failed", level="warning", network=net, error=str(e)[:300])
            continue
        _store(net, items)
        counts[net] = len(items)
    return counts


def summary() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    with session_scope() as db:
        for row in db.scalars(select(AnalyticsSnapshot)).all():
            agg = out.setdefault(row.network, {"posts": 0})
            agg["posts"] += 1
            for k, v in row.metrics.items():
                if k != "engagement":
                    agg[k] = agg.get(k, 0) + v
    return out
