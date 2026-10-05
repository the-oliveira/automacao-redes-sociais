"""Renderizador local (Pillow) — fallback do Canva com os mesmos campos de autofill.

Garantias de layout: "R$" e o valor nunca se sobrepõem (valor é medido e encolhido
até caber), nome quebra em até 3 linhas, produto sempre inteiro (contain) e tudo
dentro da safe area.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_DIR = Path("C:/Windows/Fonts")
FONTS = {
    "black": ["seguibl.ttf", "arialbd.ttf"],
    "bold": ["segoeuib.ttf", "arialbd.ttf"],
    "cond": ["bahnschrift.ttf", "arialbd.ttf"],
}

BG_TOP = (24, 10, 48)
BG_BOTTOM = (8, 4, 20)
NEON = (176, 92, 255)
PINK = (255, 64, 160)
CYAN = (60, 230, 255)
WHITE = (255, 255, 255)
MUTED = (190, 180, 215)
YELLOW = (255, 220, 60)

SIZES = {"feed": (1080, 1350), "story": (1080, 1920)}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


class InvalidImage(ValueError):
    pass


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for name in FONTS[kind]:
        p = FONT_DIR / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def fetch_image(url: str, timeout: float = 20.0) -> Image.Image:
    try:
        r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise InvalidImage(f"falha ao baixar imagem: {e}") from e
    try:
        img = Image.open(io.BytesIO(r.content))
        img.load()
    except Exception as e:  # noqa: BLE001 - qualquer falha de decodificação = imagem inválida
        raise InvalidImage(f"conteúdo não é imagem válida ({r.headers.get('content-type')})") from e
    if min(img.size) < 150:
        raise InvalidImage(f"imagem pequena demais: {img.size}")
    return img.convert("RGBA")


def trim_background(img: Image.Image) -> Image.Image:
    """Remove bordas brancas/transparentes para o produto ocupar o card."""
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, (255, 255, 255))
    from PIL import ImageChops

    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 12 else 0)
    alpha_box = img.getchannel("A").point(lambda v: 255 if v > 10 else 0).getbbox()
    box = diff.getbbox()
    if alpha_box and box:
        box = (max(box[0], alpha_box[0]), max(box[1], alpha_box[1]), min(box[2], alpha_box[2]), min(box[3], alpha_box[3]))
    if box and (box[2] - box[0]) > 50 and (box[3] - box[1]) > 50:
        return img.crop(box)
    return img


@dataclass
class ArtData:
    product_image: Image.Image
    product_name: str
    old_price: str  # "R$ 105,90" ou ""
    current_price_value: str  # "67,45"
    discount_text: str
    coupon_text: str
    cta_text: str
    rank: int | None = None  # para slides de conteúdo agregado


def _gradient(size: tuple[int, int]) -> Image.Image:
    w, h = size
    top, bot = Image.new("RGB", (1, 2)), None
    top.putpixel((0, 0), BG_TOP)
    top.putpixel((0, 1), BG_BOTTOM)
    bot = top.resize((w, h), Image.BILINEAR)
    img = bot.convert("RGBA")
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-w * 0.3, -h * 0.15, w * 0.8, h * 0.45), fill=(*NEON, 70))
    gd.ellipse((w * 0.45, h * 0.55, w * 1.4, h * 1.2), fill=(*PINK, 50))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img.alpha_composite(glow)
    # grade sutil estilo HUD
    grid = Image.new("RGBA", size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, w, 54):
        gd.line((x, 0, x, h), fill=(255, 255, 255, 10))
    for y in range(0, h, 54):
        gd.line((0, y, w, y), fill=(255, 255, 255, 10))
    img.alpha_composite(grid)
    return img


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if draw.textlength(trial, font=fnt) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def fit_text_lines(draw, text, kind, width, max_lines, start, minimum):
    size = start
    while size >= minimum:
        f = font(kind, size)
        lines = _wrap(draw, text, f, width)
        if len(lines) <= max_lines and all(draw.textlength(ln, font=f) <= width for ln in lines):
            return f, lines
        size -= 2
    f = font(kind, minimum)
    lines = _wrap(draw, text, f, width)[:max_lines]
    if lines:
        last = lines[-1]
        while draw.textlength(last + "…", font=f) > width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return f, lines


def price_layout(draw: ImageDraw.ImageDraw, value: str, max_width: int, start: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, int, int, int]:
    """Retorna (fonte_valor, fonte_rs, largura_rs, gap, largura_total) garantindo que cabe."""
    size = start
    while True:
        fv = font("black", size)
        fr = font("black", max(28, int(size * 0.42)))
        rs_w = int(draw.textlength("R$", font=fr))
        gap = max(10, int(size * 0.08))
        val_w = int(draw.textlength(value, font=fv))
        total = rs_w + gap + val_w
        if total <= max_width or size <= 60:
            return fv, fr, rs_w, gap, total
        size -= 4


def _rounded_card(size, radius, fill) -> Image.Image:
    card = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=fill)
    return card


def _pill(img: Image.Image, xy: tuple[int, int], text: str, fnt, fill, color, pad=(28, 14), anchor_right=False) -> tuple[int, int, int, int]:
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=fnt)
    asc, desc = fnt.getmetrics()
    w, h = int(tw + pad[0] * 2), int(asc + desc + pad[1] * 2)
    x, y = xy
    if anchor_right:
        x -= w
    d.rounded_rectangle((x, y, x + w, y + h), h // 2, fill=fill)
    d.text((x + pad[0], y + pad[1]), text, font=fnt, fill=color)
    return x, y, x + w, y + h


def render_art(data: ArtData, kind: str) -> Image.Image:
    W, H = SIZES[kind]
    img = _gradient((W, H))
    d = ImageDraw.Draw(img)
    story = kind == "story"
    margin = 72
    safe_top = 250 if story else 70  # story: evita barra de perfil/progresso
    safe_bottom = 320 if story else 70  # story: evita caixa de resposta/CTA do app
    inner_w = W - 2 * margin

    # topo: marca + selo de desconto
    brand_f = font("black", 46)
    d.text((margin, safe_top), "NIKKI", font=brand_f, fill=WHITE)
    bw = d.textlength("NIKKI", font=brand_f)
    d.text((margin + bw + 14, safe_top + 14), "radar de ofertas", font=font("bold", 26), fill=MUTED)
    if data.rank:
        _pill(img, (W - margin, safe_top - 4), f"#{data.rank}", font("black", 44), CYAN, BG_BOTTOM, anchor_right=True)
    elif data.discount_text:
        _pill(img, (W - margin, safe_top - 4), data.discount_text, font("black", 40), PINK, WHITE, anchor_right=True)

    # Mede o bloco de texto primeiro; o card do produto usa o espaço que sobrar.
    name_f, lines = fit_text_lines(d, data.product_name, "bold", inner_w, 3 if story else 2, 58 if story else 50, 34)
    lh = int(name_f.size * 1.18)
    of = font("bold", 40 if story else 36)
    fv, fr, rs_w, gap, _ = price_layout(d, data.current_price_value, inner_w, 190 if story else 160)
    asc_v = fv.getmetrics()[0]
    asc_r = fr.getmetrics()[0]
    cf = font("black", 40 if story else 36)
    coupon_h = (sum(cf.getmetrics()) + 28 + 24) if data.coupon_text else 0
    text_h = len(lines) * lh + (18 if story else 10) + (int(of.size * 1.25) if data.old_price else 0) + int(asc_v * 1.12) + coupon_h
    cta_h = 104 if story else 96
    cta_y = H - safe_bottom - cta_h
    card_top = safe_top + 100
    gap_card = 48 if story else 36
    card_h = min(int(H * (0.40 if story else 0.42)), cta_y - 24 - text_h - gap_card - card_top)
    card_h = max(card_h, 300)

    card = _rounded_card((inner_w, card_h), 44, (255, 255, 255, 255))
    prod = trim_background(data.product_image)
    pad = 40
    prod = ImageOps.contain(prod, (inner_w - 2 * pad, card_h - 2 * pad), Image.LANCZOS)
    card.alpha_composite(prod, ((inner_w - prod.width) // 2, (card_h - prod.height) // 2))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((margin, card_top + 18, margin + inner_w, card_top + card_h + 18), 44, fill=(*NEON, 120))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))
    img.alpha_composite(card, (margin, card_top))
    # centraliza o bloco de texto no espaço entre card e CTA
    free = cta_y - 24 - (card_top + card_h + gap_card) - text_h
    y = card_top + card_h + gap_card + max(0, free // 2)

    for ln in lines:
        d.text((margin, y), ln, font=name_f, fill=WHITE)
        y += lh
    y += 18 if story else 10

    if data.old_price:
        prefix = "de "
        d.text((margin, y), prefix + data.old_price, font=of, fill=MUTED)
        x0 = margin + d.textlength(prefix, font=of)
        x1 = margin + d.textlength(prefix + data.old_price, font=of)
        mid = y + of.getmetrics()[0] * 0.62
        d.line((x0 - 4, mid, x1 + 4, mid), fill=PINK, width=5)
        y += int(of.size * 1.25)

    # preço atual: "R$" + valor, sem sobreposição
    d.text((margin, y + (asc_v - asc_r) - int(fv.size * 0.06)), "R$", font=fr, fill=YELLOW)
    d.text((margin + rs_w + gap, y), data.current_price_value, font=fv, fill=YELLOW)
    y += int(asc_v * 1.12)

    if data.coupon_text:
        y += 8
        _, _, _, y2 = _pill(img, (margin, y), data.coupon_text, cf, CYAN, BG_BOTTOM)
        y = y2 + 16

    # CTA ancorado no fundo da safe area
    cta_f = font("black", 46 if story else 42)
    d.rounded_rectangle((margin, cta_y, W - margin, cta_y + cta_h), cta_h // 2, fill=NEON)
    cta_text = data.cta_text
    while d.textlength(cta_text, font=cta_f) > inner_w - 60 and cta_f.size > 28:
        cta_f = font("black", cta_f.size - 2)
    tw = d.textlength(cta_text, font=cta_f)
    asc, desc = cta_f.getmetrics()
    d.text(((W - tw) / 2, cta_y + (cta_h - asc - desc) / 2), cta_text, font=cta_f, fill=WHITE)
    return img.convert("RGB")


def render_title_card(title: str, subtitle: str, kind: str = "story") -> Image.Image:
    W, H = SIZES[kind]
    img = _gradient((W, H))
    d = ImageDraw.Draw(img)
    f, lines = fit_text_lines(d, title.upper(), "black", W - 160, 4, 120, 60)
    total = len(lines) * int(f.size * 1.1)
    y = (H - total) // 2 - 80
    for ln in lines:
        tw = d.textlength(ln, font=f)
        d.text(((W - tw) / 2, y), ln, font=f, fill=WHITE)
        y += int(f.size * 1.1)
    sf = font("bold", 48)
    tw = d.textlength(subtitle, font=sf)
    d.text(((W - tw) / 2, y + 40), subtitle, font=sf, fill=YELLOW)
    bf = font("black", 54)
    tw = d.textlength("NIKKI", font=bf)
    d.text(((W - tw) / 2, 260), "NIKKI", font=bf, fill=NEON)
    return img.convert("RGB")
