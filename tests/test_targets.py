import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import respx

from busarrival import collector as collector_mod
from busarrival.collector import Collector, provider_intervals
from busarrival.config import Config
from busarrival.discovery import DiscoveredRoute
from busarrival.models import KST, Provider, RouteCandidate, RouteRef, RouteStop
from busarrival.providers.base import ApiError
from busarrival.providers.seoul import SeoulProvider
from busarrival.targets import TargetStop, target_candidates


class FakeSeoul:
    provider = Provider.SEOUL

    def __init__(self):
        self.fail_once = {"23001"}

    async def nearest_ars(self, lat, lon, radius_m):
        return "23003" if lat > 37.49 else None

    async def routes_at_stop(self, ars):
        if ars in self.fail_once:
            self.fail_once.discard(ars)
            raise ApiError("seoul: 서비스 요청제한 횟수 초과")
        return {"23001": [RouteCandidate(Provider.SEOUL, "402", "100100001", "3", ars),
                          RouteCandidate(Provider.GYEONGGI, "1007", None, "8", ars)],
                "23003": [RouteCandidate(Provider.SEOUL, "333", "100100002", "3", ars)]}.get(ars, [])


class FakeGyeonggi:
    provider = Provider.GYEONGGI

    async def resolve_stop(self, ars, name):
        if ars != "91111":
            raise ApiError("gyeonggi: no exact mobileNo match")
        return "228000001", name

    async def routes_at_stop(self, station_id):
        return [RouteCandidate(Provider.GYEONGGI, "1007", "234000001", "11", station_id)]


class TargetCandidatesTest(unittest.IsolatedAsyncioTestCase):
    async def test_ars_coordinate_fallback_and_gyeonggi_stops(self):
        stops = [TargetStop("수서역", "23001", 37.48, 127.10),
                 TargetStop("수서역", "23001", 37.48, 127.10),       # 중복 ARS는 한 번만
                 TargetStop("수서역1번출구", None, 37.50, 127.10),   # 좌표로 23003
                 TargetStop("어딘가", None, 37.40, 127.10),          # 좌표로도 못 찾음
                 TargetStop("궁마을", "91111", 37.48, 127.09)]
        with patch("busarrival.targets.RETRY_DELAY_SEC", 0), patch("busarrival.targets.PACE_SEC", 0):
            cands, errors = await target_candidates(
                stops, {Provider.SEOUL: FakeSeoul(), Provider.GYEONGGI: FakeGyeonggi()})
        got = {(c.provider, c.route_id, c.route_name, c.source) for c in cands}
        self.assertEqual(got, {
            (Provider.SEOUL, "100100001", "402", "23001"),
            (Provider.GYEONGGI, None, "1007", "23001"),
            (Provider.SEOUL, "100100002", "333", "23003"),
            (Provider.GYEONGGI, "234000001", "1007", "91111"),  # source는 기관 ID가 아니라 ARS
        })
        self.assertTrue(any("어딘가" in e for e in errors))
        # 경기에서 못 찾는 서울 ARS는 정상 상황이므로 오류로 세지 않는다
        self.assertFalse(any("23001" in e and "gyeonggi" in e for e in errors))


class IntervalTest(unittest.TestCase):
    def routes(self, provider, n):
        return [DiscoveredRoute(RouteRef(provider, str(i), str(i)), [], []) for i in range(n)]

    def test_interval_stretches_to_fit_quota(self):
        cfg = Config(poll_interval_sec=30, daily_quota={"seoul": 100_000, "gyeonggi": 100_000})
        iv = provider_intervals(cfg, self.routes(Provider.SEOUL, 20) + self.routes(Provider.GYEONGGI, 40))
        self.assertEqual(iv[Provider.SEOUL], 30)
        self.assertEqual(iv[Provider.GYEONGGI], 39)  # 86400*40/(100000*0.9) = 38.4 → 39


class BudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_polling_stops_at_daily_budget(self):
        class Prov:
            provider = Provider.SEOUL
            calls = 0

            async def vehicle_positions(self, route_id):
                self.calls += 1
                return []

        prov = Prov()
        cfg = Config(daily_quota={"seoul": 2})
        db = AsyncMock()
        db.load_vehicle_states.return_value = {}
        col = Collector(cfg, db, {Provider.SEOUL: prov})
        stops = [RouteStop(1, "S1", "a", "23001", 37.48, 127.1), RouteStop(2, "S2", "b", "23002", 37.49, 127.1)]
        await col.load_workers([DiscoveredRoute(RouteRef(Provider.SEOUL, "R", "402"), stops, [])])
        w = next(iter(col.workers.values()))
        t = datetime(2026, 10, 2, 9, tzinfo=KST)
        with patch.object(collector_mod, "now_kst", return_value=t):
            for _ in range(4):
                _, log = await col.poll(w, 0.0)
        self.assertEqual(prov.calls, 2)
        self.assertIn("budget", log[-1])
        with patch.object(collector_mod, "now_kst", return_value=t.replace(day=3)):
            await col.poll(w, 0.0)  # 날짜가 바뀌면 다시 호출
        self.assertEqual(prov.calls, 3)


class SeoulNearestTest(unittest.TestCase):
    @respx.mock
    def test_nearest_ars_by_distance(self):
        respx.get("http://ws.bus.go.kr/api/rest/stationinfo/getStationByPos").respond(json={
            "msgHeader": {"headerCd": "0"}, "msgBody": {"itemList": [
                {"arsId": "23548", "dist": "19"}, {"arsId": "0", "dist": "1"}, {"arsId": "23402", "dist": "3"}]}})

        async def go():
            async with httpx.AsyncClient() as c:
                return await SeoulProvider(c, "K", retries=1).nearest_ars(37.48, 127.1, 40)
        self.assertEqual(asyncio.run(go()), "23402")


if __name__ == "__main__":
    unittest.main()
