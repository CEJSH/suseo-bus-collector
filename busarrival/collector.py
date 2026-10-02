"""수집 메인 루프.

대상: card.suseo_target_sttn(sttn_type='B') 정류장을 지나는 모든 노선 (targets.py).
매 사이클:
  1. 조회할 차례인 노선들의 '노선별 차량 위치'를 관할 기관 API로 동시 조회 (노선당 1회 호출)
     기관별 주기는 poll_interval_sec 이상이면서 daily_quota의 90% 안에 들도록 늘린다.
  2. RouteTracker가 직전 상태와 비교해 도착 이벤트 생성
  3. 이벤트 + 차량 상태 + 호출 로그를 한 트랜잭션으로 저장
심야 등 차량이 계속 0대인 노선은 idle 주기로만 조회해 쿼터를 아낀다.
기관별 하루 호출 수가 daily_quota에 닿으면 그날은 해당 기관 조회를 멈춘다.
매일 refresh_hour에 노선/정류장 목록을 재탐색한다.
"""
from __future__ import annotations

import asyncio
import logging
import math
import signal
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from .config import Config
from .db import Database
from .detector import RouteTracker
from .discovery import DiscoveredRoute, discover_routes
from .targets import target_candidates
from .models import KST, ArrivalEvent, Provider, RouteRef, VehiclePosition
from .providers import ApiError, BaseProvider, build_providers

log = logging.getLogger(__name__)

MAX_DATA_TIME_SKEW = timedelta(minutes=5)
QUOTA_SAFETY = 0.9  # 탐색·재시도 몫으로 일일 한도의 10%를 남긴다
LOOP_TICK_SEC = 2.0


def now_kst() -> datetime:
    return datetime.now(KST)


@dataclass
class RouteWorker:
    route: RouteRef
    tracker: RouteTracker
    empty_streak: int = 0
    next_poll_at: float = 0.0  # monotonic
    raw: list[tuple[datetime, VehiclePosition]] = field(default_factory=list)


def sanitize_data_time(positions: list[VehiclePosition], observed_at: datetime) -> None:
    """기관이 준 데이터 시각이 비정상(미래/너무 과거)이면 폐기하고 관측 시각을 쓰게 한다."""
    for p in positions:
        if p.data_time and not (observed_at - MAX_DATA_TIME_SKEW <= p.data_time <= observed_at + timedelta(seconds=5)):
            p.data_time = None


def next_refresh_after(t: datetime, hour: int) -> datetime:
    nxt = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    return nxt if nxt > t else nxt + timedelta(days=1)


def provider_intervals(cfg: Config, routes: list[DiscoveredRoute]) -> dict[Provider, int]:
    """기관별 조회 주기: 기본 주기로 24시간 돌려도 일일 한도의 90% 안에 들도록 필요하면 늘린다."""
    out = {}
    for prov, n in Counter(r.route.provider for r in routes).items():
        interval = cfg.poll_interval_sec
        quota = cfg.daily_quota.get(prov.value)
        if quota:
            interval = max(interval, math.ceil(86400 * n / (quota * QUOTA_SAFETY)))
        out[prov] = interval
    return out


def estimate_daily_calls(cfg: Config, routes: list[DiscoveredRoute]) -> dict[str, int]:
    intervals = provider_intervals(cfg, routes)
    out: dict[str, int] = {}
    for r in routes:
        out[r.route.provider.value] = out.get(r.route.provider.value, 0) + 86400 // intervals[r.route.provider]
    return out


async def discover_targets(cfg: Config, providers, db) -> tuple[list[DiscoveredRoute], list[str]]:
    candidates, errors, coords = await target_candidates(await db.target_stops(), providers)
    return await discover_routes(cfg, providers, candidates, coords), errors


class Collector:
    def __init__(self, cfg: Config, db: Database, providers: dict[Provider, BaseProvider]):
        self.cfg = cfg
        self.db = db
        self.providers = providers
        self.workers: dict[tuple[Provider, str], RouteWorker] = {}
        self.stop = asyncio.Event()
        self.refresh_task: asyncio.Task | None = None
        self.pending_cycle: tuple | None = None
        self.intervals: dict[Provider, int] = {}
        self.budget_day = None
        self.calls_at_day_start: dict[Provider, int] = {}
        self.budget_warned: set[Provider] = set()

    # ---------------------------------------------------------------- setup
    async def load_workers(self, routes: list[DiscoveredRoute]) -> None:
        saved_states = await self.db.load_vehicle_states()
        new: dict[tuple[Provider, str], RouteWorker] = {}
        for d in routes:
            if not d.stops or d.route.provider not in self.providers:
                continue
            key = (d.route.provider, d.route.route_id)
            if key in self.workers:  # 재탐색: 추적 상태 유지, 정류장만 교체
                w = self.workers[key]
                w.route = d.route
                w.tracker.route = d.route
                w.tracker.set_stops(d.stops)
            else:
                states = saved_states.get((d.route.provider.value, d.route.route_id), {})
                w = RouteWorker(d.route, RouteTracker(d.route, d.stops, self.cfg.detector, states))
            new[key] = w
        self.workers = new
        tracked = [DiscoveredRoute(w.route, [], []) for w in new.values()]
        self.intervals = provider_intervals(self.cfg, tracked)
        log.info("tracking %d routes: %s", len(new), ", ".join(sorted(w.route.route_name for w in new.values())))
        for prov, calls in estimate_daily_calls(self.cfg, tracked).items():
            log.info("%s: interval %ds, ~%d position calls/day at full rate (quota %s)", prov,
                     self.intervals[Provider(prov)], calls, self.cfg.daily_quota.get(prov))

    async def _discover_and_save(self) -> None:
        """탐색 결과를 기존 활성 노선에 병합한다. 부분 실패해도 찾은 노선은 저장한다."""
        try:
            routes, errors = await discover_targets(self.cfg, self.providers, self.db)
        except Exception:  # noqa: BLE001 - 탐색 실패 시 저장된 노선으로 계속 수집
            log.exception("route discovery failed — using saved routes")
            return
        if errors:
            log.warning("route discovery had %d lookup errors; saved routes are kept", len(errors))
        if routes:
            await self.db.save_routes(routes)

    async def ensure_routes(self) -> None:
        await self._discover_and_save()
        routes, _ = await self.db.load_routes()
        await self.load_workers([r for r in routes if r.route.provider in self.providers])

    async def _refresh(self) -> list[DiscoveredRoute]:
        await self._discover_and_save()
        await self.db.purge_poll_log(self.cfg.poll_log_retention_days)
        routes, _ = await self.db.load_routes()
        return [r for r in routes if r.route.provider in self.providers]

    def over_budget(self, provider: Provider) -> bool:
        today = now_kst().date()
        if today != self.budget_day:
            self.budget_day = today
            self.calls_at_day_start = {p: getattr(v, "calls", 0) for p, v in self.providers.items()}
            self.budget_warned.clear()
        quota = self.cfg.daily_quota.get(provider.value)
        used = getattr(self.providers[provider], "calls", 0) - self.calls_at_day_start.get(provider, 0)
        return bool(quota) and used >= quota

    # ---------------------------------------------------------------- polling
    async def poll(self, w: RouteWorker, cycle_start: float) -> tuple[list[ArrivalEvent], tuple]:
        prov = self.providers[w.route.provider]
        interval = self.intervals.get(w.route.provider, self.cfg.poll_interval_sec)
        started = time.monotonic()
        observed_at = now_kst()
        if self.over_budget(w.route.provider):
            if w.route.provider not in self.budget_warned:
                self.budget_warned.add(w.route.provider)
                log.error("%s: daily call budget reached — polling paused until midnight", w.route.provider.value)
            w.next_poll_at = cycle_start + self.cfg.idle_interval_sec
            return [], (w.route.provider.value, w.route.route_id, observed_at, False, None, None, 0,
                        "local daily budget reached")
        try:
            positions = await prov.vehicle_positions(w.route.route_id)
        except (ApiError, httpx.HTTPError) as e:
            log.warning("%s %s: %s", w.route.provider.value, w.route.route_name, e)
            w.next_poll_at = cycle_start + interval
            return [], (w.route.provider.value, w.route.route_id, observed_at, False, None, None,
                        int((time.monotonic() - started) * 1000), str(e)[:500])
        latency = int((time.monotonic() - started) * 1000)
        sanitize_data_time(positions, observed_at)
        events = w.tracker.update(positions, observed_at)

        w.empty_streak = 0 if positions else w.empty_streak + 1
        idle = w.empty_streak >= self.cfg.idle_after_empty_polls
        w.next_poll_at = cycle_start + (max(self.cfg.idle_interval_sec, interval) if idle else interval)
        if self.cfg.save_raw_positions:
            w.raw.extend((observed_at, p) for p in positions)
        return events, (w.route.provider.value, w.route.route_id, observed_at, True, len(positions),
                        len(events), latency, None)

    async def cycle(self) -> None:
        if self.pending_cycle is not None:
            await self.db.write_cycle(*self.pending_cycle)
            self.pending_cycle = None
        mono = time.monotonic()
        due = [w for w in self.workers.values() if w.next_poll_at <= mono + 0.5]
        if not due:
            return
        results = await asyncio.gather(*(self.poll(w, mono) for w in due))

        events = [e for evs, _ in results for e in evs]
        logs = [lg for _, lg in results]
        now = now_kst()
        states, expired, raw = [], [], []
        for w in due:
            expired += [(w.route, v) for v in w.tracker.expire(now)]
            states += [(w.route, s) for s in w.tracker.pop_dirty()]
            raw += [(w.route, t, p) for t, p in w.raw]
            w.raw.clear()
        self.pending_cycle = (events, states, expired, logs, raw)
        inserted = await self.db.write_cycle(*self.pending_cycle)
        self.pending_cycle = None
        ok = sum(1 for lg in logs if lg[3])
        log.info("cycle: polled %d/%d routes (%d ok), vehicles=%d, arrivals=%d (new %d)",
                 len(due), len(self.workers), ok, sum(lg[4] or 0 for lg in logs), len(events), inserted)

    # ---------------------------------------------------------------- main
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:  # Windows
                pass

        await self.ensure_routes()
        if not self.workers:
            raise RuntimeError("no realtime routes available: run map-stops, map-routes and discover first")
        missing = set(self.providers) - {w.route.provider for w in self.workers.values()}
        if missing:
            log.info("no target routes for providers: %s", sorted(p.value for p in missing))
        deadline = time.monotonic() + self.cfg.realtime_duration_hours * 3600
        next_refresh = next_refresh_after(now_kst(), self.cfg.refresh_hour)
        interval = LOOP_TICK_SEC  # 노선별 next_poll_at이 실제 주기를 정한다
        tick = time.monotonic()

        while not self.stop.is_set() and time.monotonic() < deadline:
            try:
                await self.cycle()
            except Exception:  # noqa: BLE001 - DB 일시 장애 등으로 루프가 죽지 않도록
                log.exception("cycle failed")

            if self.refresh_task is None and now_kst() >= next_refresh:
                log.info("daily route refresh started")
                self.refresh_task = asyncio.create_task(self._refresh())
                next_refresh = next_refresh_after(now_kst(), self.cfg.refresh_hour)
            if self.refresh_task is not None and self.refresh_task.done():
                try:
                    routes = self.refresh_task.result()
                    if routes:
                        await self.load_workers(routes)
                except Exception:  # noqa: BLE001
                    log.exception("route refresh failed — keeping current routes")
                self.refresh_task = None

            # 드리프트 없는 고정 주기
            tick += interval
            delay = tick - time.monotonic()
            if delay < 0:
                log.debug("cycle overran by %.1fs", -delay)
                tick = time.monotonic()
                delay = 0
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=min(delay, max(0, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                pass
        if self.refresh_task is not None:
            self.refresh_task.cancel()
            await asyncio.gather(self.refresh_task, return_exceptions=True)
        if self.pending_cycle is not None:
            await self.db.write_cycle(*self.pending_cycle)
            self.pending_cycle = None
        log.info("stopping")


async def run_collector(cfg: Config) -> None:
    if cfg.realtime_duration_hours <= 0:
        raise ValueError("realtime_duration_hours must be positive")
    db = await Database.connect(cfg.database_url, cfg.db_schema)
    try:
        await db.init_schema()
        async with httpx.AsyncClient(timeout=cfg.http_timeout_sec,
                                     headers={"User-Agent": "suseo-bus-collector/1.0"}) as client:
            providers = build_providers(cfg, client)
            for provider in providers.values():
                provider.request_interval_sec = 0.1
            if not providers:
                raise RuntimeError("no API keys configured")
            await Collector(cfg, db, providers).run()
    finally:
        await db.close()
