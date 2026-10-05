"""Geração de criativos: Canva (templates oficiais) com fallback para o renderizador local."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
from PIL import Image
from sqlalchemy.orm import Session

from nikki.db import MediaAsset, Promotion
from nikki.events import log_event
from nikki.media import render, video
from nikki.pipeline.copy import art_fields, brl, short_name
from nikki.settings import Settings, get_settings

FEED_FORMATS = {("instagram", "feed"), ("facebook", "post")}
STORY_FORMATS = {("instagram", "story"), ("facebook", "story")}
VIDEO_FORMATS = {("instagram", "reel"), ("facebook", "reel"), ("tiktok", "video"), ("youtube", "short")}


def needed_kinds(plan: dict[str, list[str]]) -> set[str]:
    kinds: set[str] = set()
    for net, fmts in plan.items():
        for f in fmts:
            if (net, f) in FEED_FORMATS:
                kinds.add("feed")
            if (net, f) in STORY_FORMATS:
                kinds.add("story")
            if (net, f) in VIDEO_FORMATS:
                kinds |= {"story", "video"}
    return kinds


def kind_for(network: str, fmt: str) -> str:
    if (network, fmt) in VIDEO_FORMATS:
        return "video"
    if (network, fmt) in STORY_FORMATS:
        return "story"
    return "feed"


def _promo_dir(s: Settings, promo_id: int) -> Path:
    d = s.media_dir / f"promo_{promo_id:06d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _use_canva(s: Settings) -> bool:
    if s.renderer == "local":
        return False
    from nikki.integrations import canva

    ok = s.canva_configured and canva.is_authorized()
    if s.renderer == "canva" and not ok:
        raise RuntimeError("NIKKI_RENDERER=canva mas Canva não está configurado/autorizado")
    return ok


def render_canva(db: Session, p: Promotion, kind: str, product_png: bytes, out: Path, s: Settings) -> MediaAsset:
    from nikki.integrations.canva import CanvaClient

    c = CanvaClient(s)
    template = s.canva_template_feed if kind == "feed" else s.canva_template_story
    log_event(db, "canva_started", correlation_id=p.correlation_id, promotion_id=p.id, kind=kind)
    asset_id = c.upload_image(f"nikki-{p.id}", product_png)
    design_id = c.autofill(template, f"Nikki #{p.id} {kind} - {short_name(p.product_name, 40)}", art_fields(p), asset_id)
    urls = c.export_png(design_id, width=1080)
    r = httpx.get(urls[0], timeout=60)
    r.raise_for_status()
    out.write_bytes(r.content)
    w, h = Image.open(out).size
    log_event(db, "canva_success", correlation_id=p.correlation_id, promotion_id=p.id, kind=kind, design_id=design_id)
    return MediaAsset(promotion_id=p.id, kind=kind, source="canva", local_path=str(out), width=w, height=h,
                      external_ref=design_id)


def render_local(p: Promotion, kind: str, product: Image.Image, out: Path) -> MediaAsset:
    f = art_fields(p)
    art = render.ArtData(product, f["PRODUCT_NAME"], f["OLD_PRICE"], f["CURRENT_PRICE_VALUE"], f["DISCOUNT_TEXT"],
                         f["COUPON_TEXT"], f["CTA_TEXT"])
    img = render.render_art(art, kind)
    img.save(out, "PNG", optimize=True)
    return MediaAsset(promotion_id=p.id, kind=kind, source="local", local_path=str(out), width=img.width, height=img.height)


def generate_promotion_media(db: Session, p: Promotion, plan: dict[str, list[str]], settings: Settings | None = None) -> dict[str, MediaAsset]:
    """Gera as mídias necessárias. Lança render.InvalidImage se a imagem do produto não servir."""
    s = settings or get_settings()
    kinds = needed_kinds(plan)
    product = render.fetch_image(p.image_url or "")
    buf = io.BytesIO()
    product.save(buf, "PNG")
    out_dir = _promo_dir(s, p.id)
    assets: dict[str, MediaAsset] = {}
    use_canva = _use_canva(s)
    for kind in [k for k in ("feed", "story") if k in kinds]:
        out = out_dir / f"{kind}.png"
        asset = None
        if use_canva:
            try:
                asset = render_canva(db, p, kind, buf.getvalue(), out, s)
            except Exception as e:  # noqa: BLE001 - Canva fora do ar não pode travar a operação
                log_event(db, "canva_failed", level="warning", correlation_id=p.correlation_id, promotion_id=p.id,
                          kind=kind, error=str(e)[:400])
                if s.renderer == "canva":
                    raise
        if asset is None:
            asset = render_local(p, kind, product, out)
        db.add(asset)
        assets[kind] = asset
    if "video" in kinds:
        out = out_dir / "video.mp4"
        video.single_offer_video(Path(assets["story"].local_path), out)
        a = MediaAsset(promotion_id=p.id, kind="video", source="ffmpeg", local_path=str(out), width=1080, height=1920)
        db.add(a)
        assets["video"] = a
    db.flush()
    log_event(db, "media_created", correlation_id=p.correlation_id, promotion_id=p.id,
              assets={k: v.source for k, v in assets.items()})
    return assets


def generate_aggregate_video(db: Session, agg_id: int, title: str, promos: list[Promotion], correlation_id: str,
                             settings: Settings | None = None) -> MediaAsset:
    s = settings or get_settings()
    out_dir = s.media_dir / f"aggregate_{agg_id:06d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[Path, float]] = []
    intro = out_dir / "00_intro.png"
    render.render_title_card(title, "achados pela Nikki").save(intro)
    frames.append((intro, 2.5))
    for i, p in enumerate(promos, 1):
        try:
            product = render.fetch_image(p.image_url or "")
        except render.InvalidImage:
            continue
        f = art_fields(p)
        art = render.ArtData(product, f["PRODUCT_NAME"], f["OLD_PRICE"], f["CURRENT_PRICE_VALUE"], f["DISCOUNT_TEXT"],
                             f["COUPON_TEXT"], "Link no grupo da Nikki", rank=i)
        path = out_dir / f"{i:02d}.png"
        render.render_art(art, "story").save(path)
        frames.append((path, 3.5))
    outro = out_dir / "99_outro.png"
    render.render_title_card("Links no grupo da Nikki", "link no perfil • preços podem mudar").save(outro)
    frames.append((outro, 2.5))
    out = out_dir / "video.mp4"
    video.slideshow_video(frames, out)
    a = MediaAsset(aggregate_id=agg_id, kind="video", source="ffmpeg", local_path=str(out), width=1080, height=1920)
    db.add(a)
    db.flush()
    log_event(db, "media_created", correlation_id=correlation_id, aggregate_id=agg_id, items=len(frames) - 2,
              total=brl(sum(p.current_price for p in promos)))
    return a
