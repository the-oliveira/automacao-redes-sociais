import pytest
from pydantic import ValidationError

from nikki.db import Promotion, SystemEvent, session_scope
from nikki.pipeline.intake import PromotionIn, canonical_url, ingest, normalize, validate
from tests.conftest import promo


def run(**over):
    with session_scope() as db:
        return ingest(db, promo(**over))


def test_valid_promotion_is_queued():
    r = run()
    assert r.status == "QUEUED"


@pytest.mark.parametrize("field,value,msg", [
    ("product_url", "not-a-url", "product_url inválida"),
    ("product_url", "ftp://x.com/a", "product_url inválida"),
    ("affiliate_url", "javascript:alert(1)", "affiliate_url inválida"),
    ("image_url", "imagem.jpg", "image_url inválida"),
    ("image_url", None, "image_url ausente"),
    ("current_price", 0, "current_price deve ser > 0"),
    ("original_price", 100.0, "original_price menor que current_price"),
])
def test_invalid_inputs_rejected(field, value, msg):
    r = run(**{field: value})
    assert r.status == "REJECTED" and msg in r.reason


def test_schema_errors_raise():
    with pytest.raises(ValidationError):
        PromotionIn.model_validate({"product_name": "x"})


def test_normalization():
    n = normalize(PromotionIn.model_validate(promo(store="Amazon.com.br", coupon=" nikki10 ", product_name="  Controle   PS5 ")))
    assert n["store"] == "amazon"
    assert n["coupon"] == "NIKKI10"
    assert n["product_name"] == "Controle PS5"
    assert n["discount_percent"] == pytest.approx(33.3, abs=0.1)
    assert n["product_key"] == "amazon:url:amazon/dp/B0BQXZ11B8"


def test_without_old_price_does_not_invent_one():
    n = normalize(PromotionIn.model_validate(promo(original_price=None, discount_percent=20)))
    assert n["original_price"] is None and n["discount_percent"] == 20
    n = normalize(PromotionIn.model_validate(promo(original_price=None)))
    assert n["original_price"] is None and n["discount_percent"] is None
    assert run(original_price=None).status == "QUEUED"


def test_without_coupon_ok():
    assert run(coupon="").status == "QUEUED"


def test_canonical_url_strips_tracking():
    assert canonical_url("https://loja.com/p/123?utm_source=x&cor=azul&ref=y") == "loja.com/p/123?cor=azul"
    assert canonical_url("https://produto.mercadolivre.com.br/MLB-123456-x") == "mercadolivre/MLB123456"


def test_idempotent_same_external_id():
    a = run()
    b = run()
    assert a.status == "QUEUED" and b.status == "DUPLICATE" and b.duplicate_of == a.promotion_id
    with session_scope() as db:
        assert db.query(Promotion).count() == 1
        assert db.query(Promotion).one().duplicate_count == 1
        assert db.query(SystemEvent).filter_by(event="duplicate_rejected").count() == 1


def test_dedup_same_product_different_external_id():
    a = run(external_id="a")
    b = run(external_id="b", product_url="https://www.amazon.com.br/dp/B0BQXZ11B8?utm_source=zap")
    assert b.status == "DUPLICATE" and b.duplicate_of == a.promotion_id


def test_dedup_by_name_without_ids():
    run(external_id=None, product_url="https://loja.com/a")
    b = run(external_id=None, product_url="https://loja.com/b", product_name="controle dualsense ps5 branco")
    assert b.status == "DUPLICATE"


def test_price_drop_is_new_promotion():
    run(external_id="a", current_price=299.90)
    b = run(external_id="b", current_price=249.90)
    assert b.status == "QUEUED"


def test_new_coupon_is_new_promotion():
    run(external_id="a")
    assert run(external_id="b", coupon="NOVO15").status == "QUEUED"


def test_rejected_does_not_block_valid_resend():
    assert run(image_url=None).status == "REJECTED"
    assert run().status == "QUEUED"


def test_validate_reports_all_problems():
    p = PromotionIn.model_validate(promo(product_url="x", image_url="y"))
    assert len(validate(p)) == 2
