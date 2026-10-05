"""Recepção -> validação -> normalização -> deduplicação -> persistência.

Roda dentro do request HTTP (rápido, sem rede). Publicação acontece no worker.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nikki.db import Promotion, utcnow
from nikki.events import log_event, new_correlation_id
from nikki.settings import get_settings

STATUS_QUEUED = "QUEUED"
STATUS_REJECTED = "REJECTED"

_TRACKING_PARAMS = re.compile(r"^(utm_|ref|tag|aff|affiliate|fbclid|gclid|psc|smid|th$|linkCode|camp|creative|ascsubtag|sp_|mkt_|ld)", re.I)
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
STORE_ALIASES = {
    "amazon.com.br": "amazon",
    "amazon": "amazon",
    "amzn": "amazon",
    "mercado livre": "mercadolivre",
    "mercado_livre": "mercadolivre",
    "mercadolivre": "mercadolivre",
    "ml": "mercadolivre",
    "magazine luiza": "magalu",
    "magalu": "magalu",
    "shopee": "shopee",
    "kabum": "kabum",
    "aliexpress": "aliexpress",
}


class PromotionIn(BaseModel):
    """Contrato de entrada normalizado (POST /api/promotions)."""

    external_id: str | None = Field(default=None, max_length=200)
    product_name: str = Field(min_length=1, max_length=500)
    original_price: float | None = None
    current_price: float
    discount_percent: float | None = None
    coupon: str | None = Field(default=None, max_length=80)
    product_url: str
    affiliate_url: str | None = None
    image_url: str | None = None
    store: str = Field(min_length=1, max_length=60)
    category: str | None = Field(default=None, max_length=120)
    sku: str | None = Field(default=None, max_length=120)
    popularity: float | None = Field(default=None, description="0..1 (vendas/avaliações normalizadas)")
    received_at: datetime | None = None

    @field_validator("coupon", "category", "sku", "external_id", "affiliate_url", "image_url", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class ValidationProblem(ValueError):
    pass


@dataclass
class IntakeResult:
    promotion_id: int
    status: str  # QUEUED | REJECTED | DUPLICATE
    correlation_id: str
    reason: str | None = None
    duplicate_of: int | None = None


# ---------------------------------------------------------------- validação


def _is_http_url(url: str | None) -> bool:
    if not url:
        return False
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc) and "." in parts.netloc and " " not in url.strip()


def validate(p: PromotionIn) -> list[str]:
    problems: list[str] = []
    if p.current_price is None or p.current_price <= 0:
        problems.append("current_price deve ser > 0")
    if p.current_price and p.current_price > 1_000_000:
        problems.append("current_price fora do intervalo")
    if p.original_price is not None:
        if p.original_price <= 0:
            problems.append("original_price deve ser > 0")
        elif p.current_price and p.original_price < p.current_price:
            problems.append("original_price menor que current_price")
    if p.discount_percent is not None and not (0 <= p.discount_percent < 100):
        problems.append("discount_percent fora de 0..99")
    if not _is_http_url(p.product_url):
        problems.append("product_url inválida")
    if p.affiliate_url is not None and not _is_http_url(p.affiliate_url):
        problems.append("affiliate_url inválida")
    if p.image_url is None:
        problems.append("image_url ausente")
    elif not _is_http_url(p.image_url):
        problems.append("image_url inválida")
    if p.popularity is not None and not (0 <= p.popularity <= 1):
        problems.append("popularity deve estar em 0..1")
    if len(normalize_name(p.product_name)) < 3:
        problems.append("product_name vazio")
    return problems


# ------------------------------------------------------------ normalização


def normalize_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return name


def name_key(name: str) -> str:
    n = unicodedata.normalize("NFKD", name.lower())
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()[:200]


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    # Amazon: /dp/ASIN é a identidade do produto
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", path, re.I)
    if "amazon" in host and m:
        return f"amazon/dp/{m.group(1).upper()}"
    m = re.search(r"(MLB-?\d+)", path, re.I)
    if "mercadolivre" in host and m:
        return f"mercadolivre/{m.group(1).upper().replace('-', '')}"
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)]
    return urlunsplit(("", host, path, urlencode(sorted(query)), "")).lstrip("/")


def normalize_store(store: str) -> str:
    key = store.strip().lower()
    return STORE_ALIASES.get(key, re.sub(r"[^a-z0-9]+", "", key) or "desconhecida")


def normalize(p: PromotionIn) -> dict[str, Any]:
    original = round(p.original_price, 2) if p.original_price else None
    current = round(p.current_price, 2)
    if original and original > current:
        discount = round((original - current) / original * 100, 1)
    elif p.discount_percent:
        # Desconto informado sem preço antigo: mantém o % mas NÃO inventa preço "de".
        discount = round(p.discount_percent, 1)
    else:
        discount = None
    if original and original <= current:
        original, discount = None, None
    store = normalize_store(p.store)
    sku = p.sku.strip() if p.sku else None
    product_key = f"{store}:sku:{sku.lower()}" if sku else f"{store}:url:{canonical_url(p.product_url)}"
    coupon = p.coupon.strip().upper() if p.coupon else None
    return {
        "external_id": p.external_id.strip() if p.external_id else None,
        "store": store,
        "sku": sku,
        "product_key": product_key[:300],
        "product_name": normalize_name(p.product_name),
        "name_key": name_key(p.product_name),
        "category": (p.category or "").strip().lower() or None,
        "original_price": original,
        "current_price": current,
        "discount_percent": discount,
        "coupon": coupon,
        "product_url": p.product_url.strip(),
        "affiliate_url": p.affiliate_url.strip() if p.affiliate_url else None,
        "image_url": p.image_url.strip() if p.image_url else None,
        "popularity": p.popularity,
        "received_at": (p.received_at.replace(tzinfo=None) if p.received_at else utcnow()),
    }


# ----------------------------------------------------------- deduplicação


def find_duplicate(s: Session, n: dict[str, Any]) -> tuple[Promotion | None, str | None]:
    rules = get_settings().rules()["dedup"]
    if n["external_id"]:
        hit = s.scalar(select(Promotion).where(Promotion.store == n["store"], Promotion.external_id == n["external_id"]))
        if hit:
            return hit, "external_id"
    since = utcnow() - timedelta(hours=rules["window_hours"])
    candidates = s.scalars(
        select(Promotion)
        .where(Promotion.store == n["store"], Promotion.created_at >= since, Promotion.status != STATUS_REJECTED)
        .where((Promotion.product_key == n["product_key"]) | (Promotion.name_key == n["name_key"]))
        .order_by(Promotion.created_at.desc())
    ).all()
    for c in candidates:
        drop_pct = (c.current_price - n["current_price"]) / c.current_price * 100 if c.current_price else 0
        if drop_pct >= rules["price_drop_pct_new_promo"]:
            continue  # caiu o preço de verdade: é promoção nova
        if (c.coupon or None) != (n["coupon"] or None) and n["coupon"]:
            continue  # cupom novo muda a oferta
        return c, "same_product_window"
    return None, None


# ------------------------------------------------------------ persistência


def ingest(s: Session, payload: dict[str, Any] | PromotionIn) -> IntakeResult:
    cid = new_correlation_id()
    raw = payload if isinstance(payload, dict) else payload.model_dump(mode="json")
    log_event(s, "promotion_received", correlation_id=cid, external_id=raw.get("external_id"), store=raw.get("store"))
    p = payload if isinstance(payload, PromotionIn) else PromotionIn.model_validate(payload)
    problems = validate(p)
    n = normalize(p)
    if problems:
        promo = Promotion(correlation_id=cid, raw=raw, status=STATUS_REJECTED, status_reason="; ".join(problems),
                          **{**n, "external_id": None})  # rejeitada não ocupa a chave de idempotência
        s.add(promo)
        s.flush()
        log_event(s, "promotion_rejected", level="warning", correlation_id=cid, promotion_id=promo.id, problems=problems)
        return IntakeResult(promo.id, STATUS_REJECTED, cid, reason=promo.status_reason)

    dup, why = find_duplicate(s, n)
    if dup:
        dup.duplicate_count = (dup.duplicate_count or 0) + 1
        log_event(s, "duplicate_rejected", correlation_id=cid, promotion_id=dup.id, rule=why,
                  external_id=n["external_id"], original_cid=dup.correlation_id)
        return IntakeResult(dup.id, "DUPLICATE", cid, reason=why, duplicate_of=dup.id)

    promo = Promotion(correlation_id=cid, raw=raw, status=STATUS_QUEUED, **n)
    s.add(promo)
    try:
        s.flush()
    except IntegrityError:
        # Corrida entre dois webhooks idênticos: a constraint (store, external_id) garante 1 só.
        s.rollback()
        hit = s.scalar(select(Promotion).where(Promotion.store == n["store"], Promotion.external_id == n["external_id"]))
        log_event(s, "duplicate_rejected", correlation_id=cid, promotion_id=hit.id if hit else None, rule="unique_constraint")
        return IntakeResult(hit.id if hit else 0, "DUPLICATE", cid, reason="unique_constraint", duplicate_of=hit.id if hit else None)
    log_event(s, "promotion_validated", correlation_id=cid, promotion_id=promo.id, product_key=promo.product_key)
    return IntakeResult(promo.id, STATUS_QUEUED, cid)
