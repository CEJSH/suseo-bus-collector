"""PostgreSQL 통합 테스트. TEST_DATABASE_URL이 설정된 경우에만 실행.

  TEST_DATABASE_URL=postgresql://... python -m pytest tests/test_db_integration.py
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from busarrival import collector as collector_mod
from busarrival.collector import Collector
from busarrival.config import Config
from busarrival.db import Database
from busarrival.discovery import DiscoveredRoute
from busarrival.geo import fill_cumulative_distance
from busarrival.models import KST, Provider, RouteRef, RouteStop, VehiclePosition

DSN = os.environ.get("TEST_DATABASE_URL")
SCHEMA = "bus_rt_test"
ROUTE = RouteRef(Provider.SEOUL, "100100001", "402", "3")


def make_stops():
    stops = [RouteStop(i, f"S{i}", f"정류장{i}", f"2300{i}", 37.48 + i * 0.003, 127.10) for i in range(1, 7)]
    fill_cumulative_distance(stops)
    return stops


class FakeProvider:
    provider = Provider.SEOUL

    def __init__(self):
        self.script: list[list[VehiclePosition]] = []

    async def vehicle_positions(self, route_id):
        return self.script.pop(0) if self.script else []


def pos(seq, frac=0.0):
    return VehiclePosition("V1", "서울74사1234", seq, section_frac=frac, raw={"sectOrd": seq})


@unittest.skipUnless(DSN, "TEST_DATABASE_URL not set")
class DbIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await Database.connect(DSN, SCHEMA)
        async with self.db.pool.acquire() as c:
            await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await self.db.init_schema()
        await self.db.init_schema()  # 재실행해도 안전해야 함
        self.cfg = Config(database_url=DSN, db_schema=SCHEMA, save_raw_positions=True)
        await self.db.save_routes([DiscoveredRoute(ROUTE, make_stops(), ["seoul:수서역(23001)"])])

    async def asyncTearDown(self):
        await self.db.close()

    async def new_collector(self, fake):
        col = Collector(self.cfg, self.db, {Provider.SEOUL: fake})
        routes, _ = await self.db.load_routes()
        await col.load_workers(routes)
        return col

    async def run_cycles(self, col, times):
        for t in times:
            with mock.patch.object(collector_mod, "now_kst", return_value=t):
                for w in col.workers.values():
                    w.next_poll_at = 0
                await col.cycle()

    async def test_end_to_end_with_restart(self):
        t0 = datetime.now(KST).replace(microsecond=0) - timedelta(minutes=5)
        fake = FakeProvider()
        fake.script = [[pos(1)], [pos(3)], [pos(3)]]
        col = await self.new_collector(fake)
        await self.run_cycles(col, [t0, t0 + timedelta(seconds=30), t0 + timedelta(seconds=60)])

        # 재시작: 새 Collector가 DB의 vehicle_state를 이어받아야 함
        fake2 = FakeProvider()
        fake2.script = [[pos(4)]]
        col2 = await self.new_collector(fake2)
        await self.run_cycles(col2, [t0 + timedelta(seconds=90)])

        async with self.db.pool.acquire() as c:
            rows = await c.fetch(f"SELECT * FROM {SCHEMA}.arrival_event ORDER BY station_seq")
            logs = await c.fetchval(f"SELECT count(*) FROM {SCHEMA}.poll_log WHERE ok")
            raw = await c.fetchval(f"SELECT count(*) FROM {SCHEMA}.position_raw")
            hw = await c.fetchval(f"SELECT count(*) FROM {SCHEMA}.v_link_travel_time")
            state = await c.fetchrow(f"SELECT * FROM {SCHEMA}.vehicle_state")

        self.assertEqual([r["station_seq"] for r in rows], [2, 3, 4])
        self.assertEqual(len({r["trip_id"] for r in rows}), 1)
        self.assertEqual(rows[1]["method"], "stop_observed")
        self.assertEqual(rows[1]["arrived_at"], t0 + timedelta(seconds=30))
        self.assertTrue(rows[0]["window_start"] < rows[0]["arrived_at"] < rows[0]["window_end"])
        self.assertEqual(rows[2]["window_start"], t0 + timedelta(seconds=60))  # 재시작 후 이어짐
        self.assertEqual(rows[0]["ars_id"], "23002")
        self.assertEqual((logs, raw, hw), (4, 4, 3))
        self.assertEqual(state["last_seq"], 4)

    async def test_duplicate_events_ignored(self):
        t0 = datetime.now(KST).replace(microsecond=0)
        col = await self.new_collector(FakeProvider())
        w = next(iter(col.workers.values()))
        w.tracker.update([pos(1)], t0)
        events = w.tracker.update([pos(2)], t0 + timedelta(seconds=30))
        n1 = await self.db.write_cycle(events, [], [], [], [])
        n2 = await self.db.write_cycle(events, [], [], [], [])
        self.assertEqual((n1, n2), (1, 0))

    async def test_rediscovery_merges_unless_replace(self):
        other = RouteRef(Provider.GYEONGGI, "234000001", "1007")
        await self.db.save_routes([DiscoveredRoute(other, make_stops(), ["x"])])
        routes, _ = await self.db.load_routes()
        self.assertEqual(sorted(r.route.route_id for r in routes), ["100100001", "234000001"])
        await self.db.save_routes([DiscoveredRoute(other, make_stops(), ["x"])], replace=True)
        routes, last = await self.db.load_routes()
        self.assertEqual([r.route.route_id for r in routes], ["234000001"])
        self.assertIsNotNone(last)


if __name__ == "__main__":
    unittest.main()
