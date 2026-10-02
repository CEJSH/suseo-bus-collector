"""CLI.

  python -m busarrival init-db               스키마 생성
  python -m busarrival discover [--dry-run] [--replace]
                                             대상 정류장(card.suseo_target_sttn, B) 경유 노선 탐색 → DB 저장
  python -m busarrival probe PROVIDER ROUTE  한 노선의 원시 위치 응답 확인 (필드 검증용)
  python -m busarrival run                   수집 루프 실행
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

import httpx

from .collector import discover_targets, estimate_daily_calls, run_collector
from .config import load_config
from .db import Database
from .models import Provider
from .providers import build_providers


async def cmd_init_db(cfg) -> None:
    db = await Database.connect(cfg.database_url, cfg.db_schema)
    try:
        await db.init_schema()
        print(f"schema '{cfg.db_schema}' ready")
    finally:
        await db.close()


async def cmd_discover(cfg, dry_run: bool, replace: bool) -> None:
    async with httpx.AsyncClient(timeout=cfg.http_timeout_sec) as client:
        providers = build_providers(cfg, client)
        if not providers:
            raise SystemExit("API 인증키가 없습니다. config.yaml의 seoul_api_key/gyeonggi_api_key/incheon_api_key "
                             "또는 환경변수 SEOUL_API_KEY 등을 설정하세요.")
        db = await Database.connect(cfg.database_url, cfg.db_schema)
        try:
            routes, errors = await discover_targets(cfg, providers, db)
        finally:
            await asyncio.wait_for(db.close(), 10)
    for e in errors:
        print(f"lookup error: {e}")
    for d in routes:
        near = [s.station_name for s in d.stops][:3]
        print(f"[{d.route.provider.value:8}] {d.route.route_name:>10} ({d.route.route_id}) "
              f"stops={len(d.stops):3}  via {', '.join(d.sources)[:80]}  e.g. {near}")
    for prov, calls in estimate_daily_calls(cfg, routes).items():
        print(f"{prov}: ~{calls:,} position calls/day (quota {cfg.daily_quota.get(prov)})")
    if replace and errors:
        raise SystemExit("lookup errors: not replacing saved routes (rerun, or drop --replace to merge)")
    if not dry_run and routes:
        db = await Database.connect(cfg.database_url, cfg.db_schema)
        try:
            await db.init_schema()
            await db.save_routes(routes, replace=replace)
            print(f"saved {len(routes)} routes" + (" (other routes deactivated)" if replace else ""))
        finally:
            await db.close()


async def cmd_probe(cfg, provider: str, route_id: str, stops: bool) -> None:
    async with httpx.AsyncClient(timeout=cfg.http_timeout_sec) as client:
        p = build_providers(cfg, client)[Provider(provider)]
        if stops:
            for s in await p.route_stops(route_id):
                print(s)
        for v in await p.vehicle_positions(route_id):
            print(f"veh={v.vehicle_id} plate={v.plate_no} seq={v.seq} frac={v.section_frac} t={v.data_time}")
            print("   raw:", json.dumps(v.raw, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(prog="busarrival")
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    d = sub.add_parser("discover")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--replace", action="store_true", help="deactivate saved routes not found now")
    d.add_argument("--provider", action="append", choices=[p.value for p in Provider])
    pr = sub.add_parser("probe")
    pr.add_argument("provider", choices=[p.value for p in Provider])
    pr.add_argument("route_id")
    pr.add_argument("--stops", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--provider", action="append", choices=[p.value for p in Provider])
    run.add_argument("--hours", type=float, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.cmd in ('discover', 'run') and args.provider:
        for p in Provider:
            if p.value not in args.provider:
                setattr(cfg, p.value + '_api_key', None)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "init-db":
        asyncio.run(cmd_init_db(cfg))
    elif args.cmd == "discover":
        asyncio.run(cmd_discover(cfg, args.dry_run, args.replace))
    elif args.cmd == "probe":
        asyncio.run(cmd_probe(cfg, args.provider, args.route_id, args.stops))
    elif args.cmd == "run":
        if args.provider:
            missing = [p for p in args.provider if not cfg.api_key(Provider(p))]
            if missing:
                raise SystemExit(f"Missing realtime API keys: {', '.join(missing)}")
        if args.hours is not None:
            cfg.realtime_duration_hours = args.hours
        asyncio.run(run_collector(cfg))


if __name__ == "__main__":
    main()
