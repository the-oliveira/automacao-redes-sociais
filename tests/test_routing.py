import pytest

from nikki.db import Promotion, session_scope
from nikki.pipeline.copy import ADAPTERS, build_aggregate_content
from nikki.pipeline.intake import ingest
from nikki.routing import RoutingError, all_group_urls, assert_text_routes_correctly, destination_for
from nikki.settings import get_settings, reset_settings_cache
from tests.conftest import promo

EXPECTED = {
    "instagram": "https://chat.whatsapp.com/EXAMPLEinstagramPRIMARY01",
    "facebook": "https://chat.whatsapp.com/EXAMPLEfacebookPRIMARY001",
    "tiktok": "https://chat.whatsapp.com/EXAMPLEtiktokPRIMARY00001",
    "youtube": "https://chat.whatsapp.com/EXAMPLEyoutubePRIMARY0001",
}
IG_RESERVE = "https://chat.whatsapp.com/EXAMPLEinstagramRESERVE01"


def code(u):
    return u.rsplit("/", 1)[-1]


@pytest.mark.parametrize("network,url", EXPECTED.items())
def test_each_network_routes_to_its_own_group(network, url):
    assert destination_for(network).whatsapp_url == url


def test_instagram_primary_is_default():
    d = destination_for("instagram")
    assert d.role == "primary" and d.whatsapp_url == EXPECTED["instagram"]


def test_instagram_reserve_switch_by_config_only(monkeypatch):
    monkeypatch.setenv("NIKKI_INSTAGRAM_ACTIVE_GROUP", "reserve")
    reset_settings_cache()
    d = destination_for("instagram")
    assert d.role == "reserve" and d.whatsapp_url == IG_RESERVE
    # as outras redes não mudam
    for net in ("facebook", "tiktok", "youtube"):
        assert destination_for(net).whatsapp_url == EXPECTED[net]


def test_unknown_network_rejected():
    with pytest.raises(RoutingError):
        destination_for("x")
    with pytest.raises(RoutingError):
        destination_for("twitter")


def test_all_five_groups_distinct():
    urls = all_group_urls()
    assert len(urls) == 5 and len(set(urls.values())) == 5


@pytest.mark.parametrize("reserve", [False, True])
@pytest.mark.parametrize("qa", [False, True])
def test_generated_content_never_leaks_foreign_group(monkeypatch, reserve, qa):
    monkeypatch.setenv("NIKKI_INSTAGRAM_ACTIVE_GROUP", "reserve" if reserve else "primary")
    monkeypatch.setenv("NIKKI_QA_MODE", str(qa).lower())
    monkeypatch.setenv("NIKKI_TIKTOK_URL_IN_CAPTION", "true")
    reset_settings_cache()
    with session_scope() as db:
        ingest(db, promo(coupon="NIKKI10"))
        p = db.query(Promotion).one()
        all_urls = list(all_group_urls().values())
        formats = {"instagram": ["feed", "story", "reel"], "facebook": ["post"], "tiktok": ["video"], "youtube": ["short"]}
        for net, fmts in formats.items():
            own = destination_for(net).whatsapp_url
            for fmt in fmts:
                d = ADAPTERS[net]().build(p, fmt)
                text = d.caption + (d.title or "")
                assert code(own) in text, f"{net}/{fmt} sem o próprio grupo"
                for u in all_urls:
                    if u != own:
                        assert code(u) not in text, f"{net}/{fmt} vazou {u}"
                assert d.group.whatsapp_url == own
            agg = build_aggregate_content("top_today", [p], net, formats[net][0], 1)
            for u in all_urls:
                if u != own:
                    assert code(u) not in agg.caption + (agg.title or "")


def test_guard_blocks_wrong_group_text():
    with pytest.raises(RoutingError):
        assert_text_routes_correctly("instagram", f"entra aqui {EXPECTED['facebook']}")
    with pytest.raises(RoutingError):
        assert_text_routes_correctly("instagram", f"reserva {IG_RESERVE}")  # reserva inativa
    assert_text_routes_correctly("youtube", f"ok {EXPECTED['youtube']}")


def test_go_links_used_when_public_url(monkeypatch):
    monkeypatch.setenv("NIKKI_PUBLIC_BASE_URL", "https://nikki.example.com/")
    reset_settings_cache()
    d = destination_for("tiktok")
    assert d.cta_url == "https://nikki.example.com/go/tiktok"
    assert d.whatsapp_url == EXPECTED["tiktok"]


def test_duplicate_group_in_config_is_rejected(monkeypatch):
    s = get_settings()
    bad = {"instagram": {"primary": EXPECTED["facebook"]}, "facebook": {"primary": EXPECTED["facebook"]},
           "tiktok": {"primary": EXPECTED["tiktok"]}, "youtube": {"primary": EXPECTED["youtube"]}}
    monkeypatch.setattr(type(s), "whatsapp_groups", lambda self: bad)
    with pytest.raises(RoutingError):
        destination_for("instagram")
