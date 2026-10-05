import copy

from nikki.db import Promotion, session_scope
from nikki.pipeline.copy import ADAPTERS, art_fields, brl, brl_value
from nikki.pipeline.intake import ingest
from nikki.pipeline.scoring import compute_score, tier_for
from nikki.settings import get_settings, reset_settings_cache
from tests.conftest import promo


def make(**over) -> Promotion:
    with session_scope() as db:
        r = ingest(db, promo(**over))
        return db.get(Promotion, r.promotion_id)


def test_score_range_and_breakdown():
    p = make()
    r = compute_score(p, get_settings().rules())
    assert 0 <= r.score <= 100
    assert set(r.components) >= {"discount", "price", "relevance", "popularity", "coupon", "reliability", "image", "content_potential"}


def test_discount_alone_does_not_win():
    rules = get_settings().rules()
    junk = make(external_id="j", product_name="Pano de prato liso", category="casa", store="lojaqualquer",
                original_price=100, current_price=40, affiliate_url=None, product_url="https://lojaqualquer.com/x")
    good = make(external_id="g", product_name="Booster Box Pokémon TCG Escarlate e Violeta", category="tcg",
                original_price=899, current_price=629, coupon="NIKKI10", product_url="https://www.amazon.com.br/dp/B0C1234567")
    assert junk.discount_percent > good.discount_percent
    assert compute_score(good, rules).score > compute_score(junk, rules).score


def test_weights_are_configurable():
    p = make(coupon="CUPOM")
    rules = copy.deepcopy(get_settings().rules())
    base = compute_score(p, rules).score
    rules["score"]["weights"] = {k: 0 for k in rules["score"]["weights"]}
    rules["score"]["weights"]["coupon"] = 1
    assert compute_score(p, rules).score == 100.0 != base


def test_tiers_and_channels():
    rules = get_settings().rules()
    assert tier_for(10, rules) == ("skip", {})
    name, plan = tier_for(65, rules)
    assert name == "low" and "tiktok" not in plan
    name, plan = tier_for(80, rules)
    assert name == "medium" and plan["instagram"] == ["feed", "story"]
    name, plan = tier_for(95, rules)
    assert name == "high" and {"instagram", "facebook", "tiktok", "youtube"} == set(plan)
    assert "x" not in plan and "twitter" not in plan


def test_brl_formatting():
    assert brl_value(9.9) == "9,90"
    assert brl_value(1299.9) == "1.299,90"
    assert brl(12999.9) == "R$ 12.999,90"


def test_copy_differs_per_network_and_has_cta():
    p = make(coupon="NIKKI10")
    texts = {net: ADAPTERS[net]().build(p, fmt).caption for net, fmt in
             [("instagram", "feed"), ("facebook", "post"), ("tiktok", "video"), ("youtube", "short")]}
    assert len(set(texts.values())) == 4
    assert "link na bio" in texts["instagram"]
    assert "Link da oferta" in texts["facebook"]  # FB pode link direto
    assert "perfil" in texts["tiktok"]
    assert all("NIKKI10" in t for t in texts.values())


def test_copy_without_coupon_and_without_old_price():
    p = make(coupon=None, original_price=None)
    t = ADAPTERS["instagram"]().build(p, "feed").caption
    assert "Cupom" not in t and "De R$" not in t and "Por R$ 299,90" in t
    f = art_fields(p)
    assert f["OLD_PRICE"] == "" and f["COUPON_TEXT"] == "" and f["DISCOUNT_TEXT"] == "OFERTA"


def test_art_fields_names_match_canva_template():
    f = art_fields(make(coupon="X1"))
    assert set(f) == {"PRODUCT_NAME", "OLD_PRICE", "CURRENT_PRICE_VALUE", "DISCOUNT_TEXT", "COUPON_TEXT", "CTA_TEXT"}
    assert f["CURRENT_PRICE_VALUE"] == "299,90" and not f["CURRENT_PRICE_VALUE"].startswith("R$")
    # arte é compartilhada entre redes: nunca leva URL de grupo
    assert "chat.whatsapp.com" not in " ".join(f.values())


def test_qa_prefix(monkeypatch):
    monkeypatch.setenv("NIKKI_QA_MODE", "true")
    reset_settings_cache()
    p = make()
    assert ADAPTERS["facebook"]().build(p, "post").caption.startswith("[Teste da operação da Nikki]")


def test_youtube_has_title_and_tags():
    d = ADAPTERS["youtube"]().build(make(), "short")
    assert d.title.endswith("#shorts") and len(d.title) <= 100 and d.extra["tags"]
