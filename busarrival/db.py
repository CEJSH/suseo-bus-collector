from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import asyncpg

from .discovery import DiscoveredRoute
from .models import ArrivalEvent, Provider, RouteRef, RouteStop, VehiclePosition, VehicleState

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


class Database:
    def __init__(self, pool: asyncpg.Pool, schema: str):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise ValueError(f"invalid schema name: {schema}")
        self.pool = pool
        self.s = schema

    @classmethod
    async def connect(cls, dsn: str, schema: str) -> "Database":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=30,
                                         max_inactive_connection_lifetime=60)
        return cls(pool, schema)

    async def close(self) -> None:
        await self.pool.close()

    async def init_schema(self) -> None:
        sql = SCHEMA_SQL.read_text(encoding="utf-8").format(schema=self.s)
        async with self.pool.acquire() as c:
            await c.execute(sql)

    async def target_stops(self):
        from .targets import load_target_stops
        async with self.pool.acquire() as c:
            return await load_target_stops(c)

    # ------------------------------------------------------------- routes
    async def save_routes(self, routes: list[DiscoveredRoute], replace: bool = False) -> None:
        """탐색 결과로 노선/정류장 갱신.

        기본은 병합: 부분 탐색/기관 장애 때문에 기존 수집 노선을 비활성화하지 않는다.
        replace=True면 이번에 안 나온 노선을 active=false로 둔다 (행은 보존).
        """
        async with self.pool.acquire() as c, c.transaction():
            if replace:
                await c.execute(f"UPDATE {self.s}.route SET active=false")
            for d in routes:
                r = d.route
                await c.execute(
                    f"""INSERT INTO {self.s}.route (provider, route_id, route_name, route_type, sources, active, discovered_at)
                        VALUES ($1,$2,$3,$4,$5,true,now())
                        ON CONFLICT (provider, route_id) DO UPDATE
                        SET route_name=EXCLUDED.route_name, route_type=EXCLUDED.route_type,
                            sources=EXCLUDED.sources, active=true, discovered_at=now()""",
                    r.provider.value, r.route_id, r.route_name, r.route_type, d.sources,
                )
                await c.execute(
                    f"DELETE FROM {self.s}.route_stop WHERE provider=$1 AND route_id=$2", r.provider.value, r.route_id
                )
                await c.copy_records_to_table(
                    "route_stop", schema_name=self.s,
                    columns=["provider", "route_id", "seq", "station_id", "ars_id", "station_name", "lat", "lon", "cum_dist_m"],
                    records=[(r.provider.value, r.route_id, s.seq, s.station_id, s.ars_id, s.station_name,
                              s.lat, s.lon, s.cum_dist_m) for s in d.stops],
                )

    async def load_routes(self) -> tuple[list[DiscoveredRoute], datetime | None]:
        async with self.pool.acquire() as c:
            routes = await c.fetch(f"SELECT * FROM {self.s}.route WHERE active ORDER BY provider, route_name")
            stops = await c.fetch(
                f"""SELECT s.* FROM {self.s}.route_stop s JOIN {self.s}.route r USING (provider, route_id)
                    WHERE r.active ORDER BY s.provider, s.route_id, s.seq"""
            )
        by_route: dict[tuple[str, str], list[RouteStop]] = {}
        for s in stops:
            by_route.setdefault((s["provider"], s["route_id"]), []).append(
                RouteStop(s["seq"], s["station_id"], s["station_name"], s["ars_id"], s["lat"], s["lon"], s["cum_dist_m"])
            )
        out = [
            DiscoveredRoute(
                RouteRef(Provider(r["provider"]), r["route_id"], r["route_name"], r["route_type"]),
                by_route.get((r["provider"], r["route_id"]), []),
                list(r["sources"]),
            )
            for r in routes
        ]
        last = max((r["discovered_at"] for r in routes), default=None)
        return out, last

    # ------------------------------------------------------------- state
    async def load_vehicle_states(self) -> dict[tuple[str, str], dict[str, VehicleState]]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(f"SELECT * FROM {self.s}.vehicle_state")
        out: dict[tuple[str, str], dict[str, VehicleState]] = {}
        for r in rows:
            out.setdefault((r["provider"], r["route_id"]), {})[r["vehicle_id"]] = VehicleState(
                r["vehicle_id"], r["plate_no"], r["trip_id"], r["trip_started_at"], r["last_seq"],
                r["last_frac"], r["last_obs_time"], r["last_seen_at"],
            )
        return out

    # ------------------------------------------------------------- per-cycle write
    async def write_cycle(
        self,
        events: list[ArrivalEvent],
        states: list[tuple[RouteRef, VehicleState]],
        expired: list[tuple[RouteRef, str]],
        poll_logs: list[tuple],
        raw: list[tuple[RouteRef, datetime, VehiclePosition]],
    ) -> int:
        """한 사이클 결과를 단일 트랜잭션으로 저장. 새로 적재된 이벤트 수 반환."""
        inserted = 0
        async with self.pool.acquire() as c, c.transaction():
            if events:
                rows = await c.fetch(
                    f"""INSERT INTO {self.s}.arrival_event
                        (provider, route_id, route_name, vehicle_id, plate_no, trip_id, station_seq, station_id,
                         ars_id, station_name, arrived_at, window_start, window_end, method, service_date)
                        SELECT * FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[],
                                             $7::int[], $8::text[], $9::text[], $10::text[], $11::timestamptz[],
                                             $12::timestamptz[], $13::timestamptz[], $14::text[], $15::date[])
                        ON CONFLICT (provider, route_id, trip_id, station_seq) DO NOTHING
                        RETURNING id""",
                    *map(list, zip(*[
                        (e.provider.value, e.route_id, e.route_name, e.vehicle_id, e.plate_no, e.trip_id,
                         e.station_seq, e.station_id, e.ars_id, e.station_name, e.arrived_at,
                         e.window_start, e.window_end, e.method.value, e.service_date)
                        for e in events
                    ])),
                )
                inserted = len(rows)
            if states:
                await c.executemany(
                    f"""INSERT INTO {self.s}.vehicle_state
                        (provider, route_id, vehicle_id, plate_no, trip_id, trip_started_at, last_seq, last_frac,
                         last_obs_time, last_seen_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        ON CONFLICT (provider, route_id, vehicle_id) DO UPDATE SET
                          plate_no=EXCLUDED.plate_no, trip_id=EXCLUDED.trip_id, trip_started_at=EXCLUDED.trip_started_at,
                          last_seq=EXCLUDED.last_seq, last_frac=EXCLUDED.last_frac,
                          last_obs_time=EXCLUDED.last_obs_time, last_seen_at=EXCLUDED.last_seen_at""",
                    [(r.provider.value, r.route_id, s.vehicle_id, s.plate_no, s.trip_id, s.trip_started_at,
                      s.last_seq, s.last_frac, s.last_obs_time, s.last_seen_at) for r, s in states],
                )
            if expired:
                await c.executemany(
                    f"DELETE FROM {self.s}.vehicle_state WHERE provider=$1 AND route_id=$2 AND vehicle_id=$3",
                    [(r.provider.value, r.route_id, v) for r, v in expired],
                )
            if poll_logs:
                await c.executemany(
                    f"""INSERT INTO {self.s}.poll_log
                        (provider, route_id, polled_at, ok, vehicle_count, event_count, latency_ms, error)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    poll_logs,
                )
            if raw:
                await c.executemany(
                    f"""INSERT INTO {self.s}.position_raw
                        (provider, route_id, observed_at, vehicle_id, seq, section_frac, payload)
                        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)""",
                    [(r.provider.value, r.route_id, t, p.vehicle_id, p.seq, p.section_frac,
                      json.dumps(p.raw, ensure_ascii=False)) for r, t, p in raw],
                )
        return inserted

    async def purge_poll_log(self, days: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(f"DELETE FROM {self.s}.poll_log WHERE polled_at < now() - make_interval(days => $1)", days)
