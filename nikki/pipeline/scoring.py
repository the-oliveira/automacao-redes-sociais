"""Promotion Score (0..100) com pesos configuráveis em config/rules.yaml."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from nikki.db import Promotion
from nikki.pipeline.intake import name_key


class PriceHistoryProvider(Protocol):
    """Gancho para histórico de preços (futuro). Retorna o menor preço em 90 dias ou None."""

    def lowest_price_90d(self, product_key: str) -> float | None: ...


class NoHistory:
    def lowest_price_90d(self, product_key: str) -> float | None:
        return None


@dataclass
class ScoreResult:
    score: float
    components: dict[str, float]
    weights: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "components": self.components, "weights": self.weights}


def _interp(x: float, points: list[tuple[float, float]]) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def compute_score(p: Promotion, rules: dict[str, Any], history: PriceHistoryProvider | None = None) -> ScoreResult:
    cfg = rules["score"]
    history = history or NoHistory()
    text = name_key(p.product_name)
    hype_hits = sum(1 for kw in cfg["hype_keywords"] if name_key(kw) in text)

    c: dict[str, float] = {}
    # Desconto: 10% quase nada, 50%+ excelente. Sem desconto conhecido = fraco.
    d = p.discount_percent or 0
    c["discount"] = _interp(d, [(0, 0.0), (10, 0.25), (20, 0.5), (35, 0.8), (50, 1.0)])

    # Preço: economia absoluta (escala log) + faixa de compra por impulso.
    savings = (p.original_price - p.current_price) if p.original_price else 0.0
    savings_score = min(1.0, math.log10(1 + savings) / math.log10(1 + 500))  # R$500 economizados = 1.0
    band = _interp(p.current_price, [(0, 0.7), (30, 1.0), (300, 0.9), (1500, 0.6), (5000, 0.35)])
    c["price"] = round(0.55 * savings_score + 0.45 * band, 4)

    cat = (p.category or "").lower()
    rel_map = cfg["category_relevance"]
    cat_rel = max((v for k, v in rel_map.items() if k != "default" and k in cat), default=rel_map["default"])
    c["relevance"] = min(1.0, cat_rel + 0.15 * min(hype_hits, 2))

    c["popularity"] = p.popularity if p.popularity is not None else 0.5
    c["coupon"] = 1.0 if p.coupon else 0.3

    trust = cfg["store_trust"]
    rel = trust.get(p.store, trust["default"])
    if not p.affiliate_url:
        rel *= 0.8
    if not p.original_price:
        rel *= 0.85  # preço "de" ausente: menos verificável
    c["reliability"] = round(rel, 4)

    c["image"] = 1.0 if p.image_url else 0.0

    name_len = len(p.product_name)
    readable = _interp(name_len, [(5, 0.4), (20, 1.0), (80, 1.0), (160, 0.6), (300, 0.4)])
    c["content_potential"] = round(min(1.0, 0.5 * readable + 0.25 * min(hype_hits, 2) + 0.25 * c["discount"]), 4)

    low = history.lowest_price_90d(p.product_key)
    if low is None:
        c["price_history"] = 0.5
    else:
        c["price_history"] = 1.0 if p.current_price <= low else _interp(p.current_price / low, [(1.0, 1.0), (1.1, 0.5), (1.3, 0.0)])

    weights = {k: float(v) for k, v in cfg["weights"].items()}
    total_w = sum(weights.values()) or 1.0
    score = sum(c[k] * w for k, w in weights.items()) / total_w * 100
    return ScoreResult(round(score, 1), {k: round(v, 3) for k, v in c.items()}, weights)


def tier_for(score: float, rules: dict[str, Any]) -> tuple[str, dict[str, list[str]]]:
    tiers = sorted(rules["distribution"]["tiers"], key=lambda t: t["min"])
    chosen = tiers[0]
    for t in tiers:
        if score >= t["min"]:
            chosen = t
    return chosen["name"], {net: list(fmts) for net, fmts in (chosen.get("formats") or {}).items()}
