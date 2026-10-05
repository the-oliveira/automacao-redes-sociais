"""CLI: python -m nikki <comando>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def _base(args) -> str:
    from nikki.settings import get_settings

    s = get_settings()
    return args.url or f"http://{s.host}:{s.port}"


def cmd_serve(args) -> None:
    import uvicorn

    from nikki.settings import get_settings

    s = get_settings()
    uvicorn.run("nikki.web.app:app", host=s.host, port=s.port, log_level="warning")


def cmd_send(args) -> None:
    """Envia um JSON (fixture) para o endpoint, como faria um webhook."""
    from nikki.settings import get_settings

    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    headers = {"X-API-Key": get_settings().api_key} if get_settings().api_key else {}
    r = httpx.post(f"{_base(args)}/api/promotions", json=payload, headers=headers, timeout=30)
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False, indent=2))


def cmd_add(args) -> None:
    """Cadastra uma promoção pela linha de comando."""
    from nikki.settings import get_settings

    payload = {k: v for k, v in {
        "external_id": args.external_id, "product_name": args.name, "original_price": args.old, "current_price": args.price,
        "coupon": args.coupon, "product_url": args.url_product, "affiliate_url": args.affiliate, "image_url": args.image,
        "store": args.store, "category": args.category}.items() if v is not None}
    headers = {"X-API-Key": get_settings().api_key} if get_settings().api_key else {}
    r = httpx.post(f"{_base(args)}/api/promotions", json=payload, headers=headers, timeout=30)
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False, indent=2))


def cmd_status(args) -> None:
    try:
        r = httpx.get(f"{_base(args)}/api/status", timeout=10)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    except httpx.HTTPError as e:
        print(f"serviço fora do ar: {e}")
        sys.exit(1)


def cmd_tick(args) -> None:
    """Executa um ciclo do worker sem servidor (útil para depurar)."""
    from nikki.db import init_engine
    from nikki.worker import Worker

    init_engine()
    w = Worker()
    for _ in range(args.n):
        w.tick()
    print("ok")


def cmd_metricool_check(args) -> None:
    from nikki.integrations.metricool import MetricoolClient

    brands = MetricoolClient().brands()
    for b in brands:
        print(b.get("id"), b.get("label"), {k: v for k, v in b.items() if k in ("instagram", "facebook", "tiktok", "youtube", "facebookPageId")})


def cmd_canva_check(args) -> None:
    from nikki.db import init_engine
    from nikki.integrations.canva import CanvaClient
    from nikki.settings import get_settings

    init_engine()
    c = CanvaClient()
    print("user:", c.me())
    for t in (get_settings().canva_template_feed, get_settings().canva_template_story):
        print(t, c.get_design(t).get("design", {}).get("title"))


def cmd_aggregate(args) -> None:
    from nikki.db import init_engine
    from nikki.pipeline.orchestrator import build_aggregate

    init_engine()
    print("aggregate_id:", build_aggregate(args.kind, force=args.force))


def cmd_analytics(args) -> None:
    from nikki import analytics
    from nikki.db import init_engine

    init_engine()
    print(analytics.collect(args.days))
    print(json.dumps(analytics.summary(), indent=2))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="nikki")
    ap.add_argument("--url", help="URL do serviço (padrão http://host:port do .env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    p = sub.add_parser("send")
    p.add_argument("file")
    p.set_defaults(fn=cmd_send)
    p = sub.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--old", type=float)
    p.add_argument("--coupon")
    p.add_argument("--url-product", required=True)
    p.add_argument("--affiliate")
    p.add_argument("--image", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--category")
    p.add_argument("--external-id")
    p.set_defaults(fn=cmd_add)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("tick")
    p.add_argument("-n", type=int, default=1)
    p.set_defaults(fn=cmd_tick)
    sub.add_parser("metricool-check").set_defaults(fn=cmd_metricool_check)
    sub.add_parser("canva-check").set_defaults(fn=cmd_canva_check)
    p = sub.add_parser("aggregate")
    p.add_argument("kind", default="top_today", nargs="?")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_aggregate)
    p = sub.add_parser("analytics")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(fn=cmd_analytics)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
