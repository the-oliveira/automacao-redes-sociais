"""Publishers: Metricool (real) e DryRun (outbox local, para desenvolvimento)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from nikki.db import ContentVariant, MediaAsset, PublicationJob
from nikki.integrations.metricool import MetricoolClient, PublishError, ScheduledResult
from nikki.settings import DATA_DIR, Settings, get_settings


class Publisher(Protocol):
    name: str

    def publish(self, db: Session, job: PublicationJob, variant: ContentVariant, media: list[MediaAsset]) -> ScheduledResult: ...

    def status(self, external_id: str) -> dict: ...


class DryRunPublisher:
    name = "dry_run"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def publish(self, db, job, variant, media):
        out = DATA_DIR / "outbox" / variant.network
        out.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job.id, "network": variant.network, "format": variant.format, "title": variant.title,
            "caption": variant.caption, "group": variant.group_url, "media": [m.local_path for m in media],
            "scheduled_for": job.scheduled_for.isoformat() if job.scheduled_for else None,
        }
        (out / f"job_{job.id:06d}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return ScheduledResult(f"dry-{job.id}", None, "DRY_RUN", None, payload)

    def status(self, external_id: str) -> dict:
        return {"status": "DRY_RUN"}


class MetricoolPublisher:
    name = "metricool"

    def __init__(self, settings: Settings | None = None, client: MetricoolClient | None = None):
        self.s = settings or get_settings()
        self.client = client or MetricoolClient(self.s)

    def ensure_uploaded(self, db: Session, asset: MediaAsset) -> str:
        if not asset.public_url:
            asset.public_url = self.client.upload_file(Path(asset.local_path))
            db.flush()
        return asset.public_url

    def publish(self, db, job, variant, media):
        if not media:
            raise PublishError(f"{variant.network}/{variant.format} exige mídia e nenhuma foi gerada", retryable=False)
        urls = [self.ensure_uploaded(db, m) for m in media]
        when = job.scheduled_for or datetime.now()
        body = self.client.build_post(network=variant.network, fmt=variant.format, text=variant.caption, media=urls,
                                      when=when, title=variant.title, tags=(variant.extra or {}).get("tags"))
        return self.client.schedule(body)

    def status(self, external_id: str) -> dict:
        data = self.client.get_post(external_id)
        prov = (data.get("providers") or [{}])[0]
        return {"status": prov.get("status"), "public_url": prov.get("publicUrl"), "detail": prov.get("detailedStatus"), "raw": data}


class DirectPublisher:
    """Publica direto nas APIs oficiais, no horário do job (o worker só executa jobs vencidos)."""

    name = "direct"

    def __init__(self, settings: Settings | None = None, clients: dict | None = None):
        self.s = settings or get_settings()
        self._clients = clients or {}

    def client(self, network: str):
        if network not in self._clients:
            if network in ("instagram", "facebook"):
                from nikki.integrations.meta import MetaClient

                self._clients["instagram"] = self._clients["facebook"] = MetaClient(self.s)
            elif network == "youtube":
                from nikki.integrations.youtube import YouTubeClient

                self._clients[network] = YouTubeClient(self.s)
            elif network == "tiktok":
                from nikki.integrations.tiktok import TikTokClient

                self._clients[network] = TikTokClient(self.s)
            else:
                raise PublishError(f"rede não suportada: {network}", retryable=False)
        return self._clients[network]

    def publish(self, db, job, variant, media):
        if not media:
            raise PublishError(f"{variant.network}/{variant.format} exige mídia e nenhuma foi gerada", retryable=False)
        path = Path(media[0].local_path)
        c = self.client(variant.network)
        net, fmt, text = variant.network, variant.format, variant.caption
        if net == "facebook":
            res = c.fb_reel(path, text) if fmt == "reel" else c.fb_photo_story(path) if fmt == "story" else c.fb_photo_post(path, text)
        elif net == "instagram":
            res = c.ig_reel(path, text) if fmt == "reel" else c.ig_image(path, text, story=fmt == "story")
        elif net == "youtube":
            res = c.upload_short(path, variant.title or text.splitlines()[0], text, (variant.extra or {}).get("tags"))
        elif net == "tiktok":
            res = c.post_video(path, text)
        else:
            raise PublishError(f"rede não suportada: {net}", retryable=False)
        status = "PUBLISHED"
        if net == "tiktok" and res.get("status") not in (None, "PUBLISH_COMPLETE"):
            status = "PENDING"  # ainda processando no TikTok; o sync confirma depois
        return ScheduledResult(str(res["id"]), None, status, res.get("url"), res)

    def status(self, external_id: str, network: str | None = None) -> dict:
        if network == "tiktok":
            try:
                st = self.client("tiktok").wait(external_id, timeout=0)
            except PublishError as e:
                if e.retryable:
                    raise
                return {"status": "ERROR", "detail": str(e)}
            return {"status": "PUBLISHED" if st.get("status") == "PUBLISH_COMPLETE" else "PENDING", "detail": st}
        return {"status": "PUBLISHED"}


def get_publisher(settings: Settings | None = None) -> Publisher:
    s = settings or get_settings()
    if s.publisher == "direct":
        return DirectPublisher(s)
    if s.publisher == "metricool":
        if not s.metricool_configured:
            raise RuntimeError("NIKKI_PUBLISHER=metricool mas faltam NIKKI_METRICOOL_USER_TOKEN/USER_ID no .env")
        return MetricoolPublisher(s)
    return DryRunPublisher(s)
