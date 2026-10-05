"""Copy da Nikki: um adaptador por rede, com variação determinística de headline/CTA.

O grupo de WhatsApp vem SEMPRE de nikki.routing — nunca de texto livre ou IA.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from nikki.db import Promotion
from nikki.routing import GroupDestination, assert_text_routes_correctly, destination_for
from nikki.settings import Settings, get_settings


def brl_value(v: float) -> str:
    """1299.9 -> '1.299,90' (sem o R$, para o campo CURRENT_PRICE_VALUE)."""
    s = f"{v:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def brl(v: float) -> str:
    return f"R$ {brl_value(v)}"


def _pick(options: list[str], *seed: Any) -> str:
    h = int(hashlib.sha256("|".join(map(str, seed)).encode()).hexdigest(), 16)
    return options[h % len(options)]


HEADLINES = [
    "Achadinho detectado no radar 👀",
    "Olha o que apareceu no meu radar hoje",
    "Preço caiu e eu vim correndo avisar",
    "Essa aqui vale o print 📸",
    "Alerta de oferta, player 🎮",
    "Não sei vocês, mas eu achei esse preço bem bom",
    "Bora economizar? Achei essa",
    "Spawnou uma oferta boa aqui",
]
HEADLINES_HOT = [
    "TÁ BARATO DEMAIS, corre 🔥",
    "Isso aqui é drop raro, hein",
    "Critical hit no preço 💥",
    "Oferta lendária desbloqueada",
]
CTA_GROUP = [
    "Os links eu solto primeiro no grupo do WhatsApp",
    "Quer receber antes de todo mundo? Entra no grupo",
    "No grupo do WhatsApp sai tudo em primeira mão",
    "Cola no grupo pra não perder as próximas",
]
TIKTOK_HOOKS = [
    "Esse preço não dura muito",
    "Para tudo e olha esse preço",
    "Achei antes de você 😏",
    "Se você queria um desses, a hora é agora",
    "Preço de loot, não de loja",
]
ART_CTA = [
    "Link no grupo da Nikki",
    "Entra no grupo e pega o link",
    "Corre no grupo da Nikki",
]
DISCLAIMER = "Preço e estoque podem mudar a qualquer momento."
QA_PREFIX = "[Teste da operação da Nikki] "

BASE_TAGS = ["#achadinhos", "#ofertas", "#promoção"]
CATEGORY_TAGS = {
    "tcg": ["#tcg", "#pokemontcg"],
    "games": ["#games", "#gamer"],
    "gamer": ["#gamer", "#setupgamer"],
    "geek": ["#geek", "#nerd"],
    "tecnologia": ["#tecnologia", "#tech"],
    "eletronicos": ["#tech"],
    "informatica": ["#setup", "#pcgamer"],
    "anime": ["#anime", "#otaku"],
}


@dataclass
class ContentDraft:
    network: str
    format: str
    caption: str
    group: GroupDestination
    title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OfferView:
    """Dados já formatados de uma oferta (usado por copy e por arte)."""

    name: str
    current: str
    current_value: str
    old: str | None
    discount: str | None
    coupon: str | None
    buy_url: str
    category: str
    hot: bool

    @classmethod
    def of(cls, p: Promotion, hot: bool = False) -> OfferView:
        return cls(
            name=p.product_name,
            current=brl(p.current_price),
            current_value=brl_value(p.current_price),
            old=brl(p.original_price) if p.original_price else None,
            discount=f"-{round(p.discount_percent):d}%" if p.discount_percent else None,
            coupon=p.coupon,
            buy_url=p.affiliate_url or p.product_url,
            category=p.category or "",
            hot=hot,
        )

    def price_line(self) -> str:
        if self.old:
            return f"De {self.old} por {self.current}" + (f" ({self.discount})" if self.discount else "")
        return f"Por {self.current}" + (f" ({self.discount})" if self.discount else "")


def short_name(name: str, limit: int = 60) -> str:
    return name if len(name) <= limit else name[: limit - 1].rsplit(" ", 1)[0] + "…"


def hashtags(category: str, extra: list[str] | None = None, limit: int = 8) -> str:
    tags = list(BASE_TAGS)
    for k, v in CATEGORY_TAGS.items():
        if k in category:
            tags += v
    tags += extra or []
    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return " ".join(seen[:limit])


def art_fields(p: Promotion) -> dict[str, str]:
    """Campos de autofill do Canva (mesmos nomes do template)."""
    o = OfferView.of(p)
    return {
        "PRODUCT_NAME": short_name(o.name, 70),
        "OLD_PRICE": o.old or "",
        "CURRENT_PRICE_VALUE": o.current_value,
        "DISCOUNT_TEXT": (o.discount + " OFF") if o.discount else "OFERTA",
        "COUPON_TEXT": f"CUPOM: {o.coupon}" if o.coupon else "",
        "CTA_TEXT": _pick(ART_CTA, p.id, "art"),
    }


# ------------------------------------------------------------- adaptadores


class BaseAdapter:
    network = ""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def group(self) -> GroupDestination:
        return destination_for(self.network, self.s)

    def prefix(self) -> str:
        return QA_PREFIX if self.s.qa_mode else ""

    def headline(self, p: Promotion, o: OfferView, fmt: str) -> str:
        return _pick(HEADLINES_HOT if o.hot else HEADLINES, p.id, self.network, fmt)

    def finalize(self, d: ContentDraft) -> ContentDraft:
        assert_text_routes_correctly(self.network, d.caption + (d.title or ""), self.s)
        return d

    def build(self, p: Promotion, fmt: str, hot: bool = False) -> ContentDraft:  # pragma: no cover
        raise NotImplementedError


class InstagramContentAdapter(BaseAdapter):
    network = "instagram"

    def build(self, p: Promotion, fmt: str, hot: bool = False) -> ContentDraft:
        o, g = OfferView.of(p, hot), self.group()
        lines = [self.prefix() + self.headline(p, o, fmt), "", f"🎮 {o.name}", o.price_line()]
        if o.coupon:
            lines.append(f"🎟️ Cupom: {o.coupon}")
        lines += [
            "",
            f"👉 {_pick(CTA_GROUP, p.id, self.network, fmt)} — link na bio!",
            f"Grupo: {g.cta_url}",
            "",
            DISCLAIMER,
            "",
            hashtags(o.category),
        ]
        return self.finalize(ContentDraft(self.network, fmt, "\n".join(lines), g))


class FacebookContentAdapter(BaseAdapter):
    network = "facebook"

    def build(self, p: Promotion, fmt: str, hot: bool = False) -> ContentDraft:
        o, g = OfferView.of(p, hot), self.group()
        lines = [self.prefix() + self.headline(p, o, fmt), "", f"🛒 {o.name}", o.price_line()]
        if o.coupon:
            lines.append(f"🎟️ Usa o cupom {o.coupon}")
        lines += [
            "",
            f"🔗 Link da oferta: {o.buy_url}",
            "",
            f"💬 {_pick(CTA_GROUP, p.id, self.network, fmt)}: {g.cta_url}",
            "",
            DISCLAIMER,
        ]
        return self.finalize(ContentDraft(self.network, fmt, "\n".join(lines), g))


class TikTokContentAdapter(BaseAdapter):
    network = "tiktok"

    def build(self, p: Promotion, fmt: str, hot: bool = False) -> ContentDraft:
        o, g = OfferView.of(p, hot), self.group()
        hook = _pick(TIKTOK_HOOKS, p.id, self.network, fmt)
        lines = [f"{self.prefix()}{hook} 👀 {short_name(o.name, 70)} por {o.current}" + (f" ({o.discount})" if o.discount else "")]
        if o.coupon:
            lines.append(f"Cupom: {o.coupon}")
        cta = "Grupo de ofertas no link do perfil 💜"
        if self.s.tiktok_url_in_caption:
            cta += f" {g.cta_url}"
        lines += [cta, hashtags(o.category, ["#fyp", "#achadinhosdodia"], limit=6)]
        title = short_name(f"{hook}: {o.name}", 90)
        return self.finalize(ContentDraft(self.network, fmt, "\n".join(lines), g, title=title))


class YouTubeContentAdapter(BaseAdapter):
    network = "youtube"

    def build(self, p: Promotion, fmt: str, hot: bool = False) -> ContentDraft:
        o, g = OfferView.of(p, hot), self.group()
        title = short_name(f"{self.prefix()}{short_name(o.name, 55)} por {o.current}" + (f" {o.discount}" if o.discount else ""), 92) + " #shorts"
        lines = [
            self.headline(p, o, fmt),
            "",
            f"{o.name}",
            o.price_line(),
        ]
        if o.coupon:
            lines.append(f"Cupom: {o.coupon}")
        lines += [
            "",
            f"Link da oferta: {o.buy_url}",
            f"Grupo de ofertas da Nikki no WhatsApp: {g.cta_url}",
            "",
            DISCLAIMER,
            "#shorts #achadinhos #ofertas",
        ]
        tags = ["achadinhos", "ofertas", "promoção", "nikki"] + [t.strip("#") for t in CATEGORY_TAGS.get(o.category, [])]
        return self.finalize(ContentDraft(self.network, fmt, "\n".join(lines), g, title=title, extra={"tags": tags}))


ADAPTERS: dict[str, type[BaseAdapter]] = {
    "instagram": InstagramContentAdapter,
    "facebook": FacebookContentAdapter,
    "tiktok": TikTokContentAdapter,
    "youtube": YouTubeContentAdapter,
}


def build_content(p: Promotion, plan: dict[str, list[str]], hot: bool = False, settings: Settings | None = None) -> list[ContentDraft]:
    out: list[ContentDraft] = []
    for net, fmts in plan.items():
        adapter = ADAPTERS[net](settings)
        for fmt in fmts:
            out.append(adapter.build(p, fmt, hot))
    return out


# ---------------------------------------------------------- conteúdo agregado

AGGREGATE_TITLES = {
    "top_morning": "{n} melhores promoções da manhã",
    "top_today": "{n} achadinhos de hoje",
    "price_drops": "{n} melhores quedas de preço",
    "still_available": "Ofertas que ainda estão disponíveis",
    "gamer_geek": "Top ofertas gamer/geek",
    "tcg": "Top ofertas TCG",
}


def build_aggregate_content(kind: str, promos: list[Promotion], network: str, fmt: str, agg_id: int,
                            settings: Settings | None = None) -> ContentDraft:
    s = settings or get_settings()
    g = destination_for(network, s)
    title = AGGREGATE_TITLES[kind].format(n=len(promos))
    prefix = QA_PREFIX if s.qa_mode else ""
    items = [f"{i}. {short_name(p.product_name, 50)} — {brl(p.current_price)}" + (f" (-{round(p.discount_percent)}%)" if p.discount_percent else "")
             for i, p in enumerate(promos, 1)]
    cats = " ".join(sorted({p.category or "" for p in promos}))
    if network == "youtube":
        caption = "\n".join([title, "", *items, "", f"Grupo de ofertas da Nikki no WhatsApp: {g.cta_url}", "", DISCLAIMER, "#shorts #achadinhos #ofertas"])
        d = ContentDraft(network, fmt, caption, g, title=(prefix + title)[:90] + " #shorts", extra={"tags": ["achadinhos", "ofertas", "promoção"]})
    elif network == "tiktok":
        cta = "Links no grupo — tá no perfil 💜" + (f" {g.cta_url}" if s.tiktok_url_in_caption else "")
        caption = "\n".join([prefix + title + " 👀", *items[:5], cta, hashtags(cats, ["#fyp"], limit=6)])
        d = ContentDraft(network, fmt, caption, g, title=title)
    elif network == "facebook":
        caption = "\n".join([prefix + title + " 🔥", "", *items, "", f"💬 Links e próximas ofertas no grupo: {g.cta_url}", "", DISCLAIMER])
        d = ContentDraft(network, fmt, caption, g)
    else:
        caption = "\n".join([prefix + title + " 🔥", "", *items, "", f"👉 {_pick(CTA_GROUP, agg_id, network)} — link na bio!", f"Grupo: {g.cta_url}", "", DISCLAIMER, "", hashtags(cats)])
        d = ContentDraft(network, fmt, caption, g)
    assert_text_routes_correctly(network, d.caption + (d.title or ""), s)
    return d
