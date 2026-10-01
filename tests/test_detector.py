"""도착 감지 로직 테스트 (표준 라이브러리만 사용: python3 -m unittest discover tests)."""
import unittest
from datetime import datetime, timedelta

from busarrival.detector import DetectorParams, RouteTracker
from busarrival.models import KST, ArrivalMethod, Provider, RouteRef, RouteStop, VehiclePosition

T0 = datetime(2026, 9, 30, 8, 0, 0, tzinfo=KST)
ROUTE = RouteRef(Provider.SEOUL, "R1", "402")


def stops(dists):
    return [RouteStop(seq=i + 1, station_id=f"S{i + 1}", station_name=f"정류장{i + 1}", cum_dist_m=d)
            for i, d in enumerate(dists)]


def pos(seq, frac=None, veh="V1"):
    return VehiclePosition(vehicle_id=veh, plate_no="서울70사1234", seq=seq, section_frac=frac)


def at(sec):
    return T0 + timedelta(seconds=sec)


class DetectorTest(unittest.TestCase):
    def setUp(self):
        self.tr = RouteTracker(ROUTE, stops([0, 400, 800, 1200, 1600, 2000]), DetectorParams())

    def test_first_sighting_emits_nothing(self):
        self.assertEqual(self.tr.update([pos(2)], at(0)), [])
        self.assertIn("V1", self.tr.states)

    def test_single_advance_observed_at_stop(self):
        self.tr.update([pos(2, 0.5)], at(0))
        ev = self.tr.update([pos(3, 0.0)], at(30))
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e.station_seq, e.method), (3, ArrivalMethod.STOP_OBSERVED))
        self.assertEqual(e.arrived_at, at(30))
        self.assertEqual((e.window_start, e.window_end), (at(0), at(30)))

    def test_multi_advance_interpolated_by_distance(self):
        # t=0: 1번 정류장 정차(0m) / t=40: 4번 정류장 정차(1200m) → 거리 비례 보간
        self.tr.update([pos(1, 0.0)], at(0))
        ev = self.tr.update([pos(4, 0.0)], at(40))
        self.assertEqual([e.station_seq for e in ev], [2, 3, 4])
        self.assertEqual([e.arrived_at for e in ev], [at(40 * 400 / 1200), at(40 * 800 / 1200), at(40)])
        self.assertEqual(ev[0].method, ArrivalMethod.INTERPOLATED)
        self.assertEqual(ev[-1].method, ArrivalMethod.STOP_OBSERVED)

    def test_unknown_frac_assumes_mid_section(self):
        # t=0: 1~2 구간 중간(200m), t=30: 2~3 구간 중간(600m) → 400m 지점은 15초
        self.tr.update([pos(1)], at(0))
        ev = self.tr.update([pos(2)], at(30))
        self.assertEqual(ev[0].arrived_at, at(15))

    def test_same_seq_tightens_window(self):
        self.tr.update([pos(2, 0.5)], at(0))
        self.tr.update([pos(2, 0.9)], at(30))
        ev = self.tr.update([pos(3, 0.0)], at(60))
        self.assertEqual(ev[0].window_start, at(30))

    def test_no_duplicate_on_repeated_snapshot(self):
        self.tr.update([pos(2, 0.0)], at(0))
        self.assertEqual(len(self.tr.update([pos(3, 0.0)], at(30))), 1)
        self.assertEqual(self.tr.update([pos(3, 0.0)], at(60)), [])
        self.assertEqual(self.tr.update([pos(3, 0.0)], at(60)), [])

    def test_small_backtrack_is_ignored(self):
        self.tr.update([pos(4, 0.0)], at(0))
        self.assertEqual(self.tr.update([pos(3)], at(30)), [])
        ev = self.tr.update([pos(5, 0.0)], at(60))
        self.assertEqual([e.station_seq for e in ev], [5])
        trip = self.tr.states["V1"].trip_id
        self.assertEqual(ev[0].trip_id, trip)

    def test_large_backtrack_starts_new_trip(self):
        self.tr.update([pos(6, 0.0)], at(0))
        old_trip = self.tr.states["V1"].trip_id
        self.assertEqual(self.tr.update([pos(1, 0.0)], at(600)), [])
        self.assertNotEqual(self.tr.states["V1"].trip_id, old_trip)
        ev = self.tr.update([pos(2, 0.0)], at(630))
        self.assertEqual(ev[0].trip_id, self.tr.states["V1"].trip_id)

    def test_long_gap_skips_events_but_updates_state(self):
        self.tr.update([pos(1, 0.0)], at(0))
        self.assertEqual(self.tr.update([pos(3, 0.0)], at(700)), [])
        self.assertEqual(self.tr.states["V1"].last_seq, 3)
        self.assertEqual(len(self.tr.update([pos(4, 0.0)], at(730))), 1)

    def test_vehicle_timeout_starts_new_trip(self):
        self.tr.update([pos(2, 0.0)], at(0))
        old = self.tr.states["V1"].trip_id
        self.assertEqual(self.tr.update([pos(3, 0.0)], at(3600)), [])
        self.assertNotEqual(self.tr.states["V1"].trip_id, old)

    def test_multiple_vehicles_independent(self):
        self.tr.update([pos(1, 0.0, "A"), pos(4, 0.0, "B")], at(0))
        ev = self.tr.update([pos(2, 0.0, "A"), pos(5, 0.0, "B")], at(30))
        self.assertEqual(sorted((e.vehicle_id, e.station_seq) for e in ev), [("A", 2), ("B", 5)])

    def test_unknown_seq_ignored(self):
        self.assertEqual(self.tr.update([pos(99)], at(0)), [])
        self.assertNotIn("V1", self.tr.states)

    def test_expire_and_dirty(self):
        self.tr.update([pos(1)], at(0))
        self.assertEqual([s.vehicle_id for s in self.tr.pop_dirty()], ["V1"])
        self.assertEqual(self.tr.pop_dirty(), [])
        self.assertEqual(self.tr.expire(at(4000)), ["V1"])

    def test_service_date_before_4am_is_previous_day(self):
        self.tr.update([pos(1, 0.0)], T0.replace(hour=0, minute=30))
        ev = self.tr.update([pos(2, 0.0)], T0.replace(hour=0, minute=31))
        self.assertEqual(str(ev[0].service_date), "2026-09-29")


if __name__ == "__main__":
    unittest.main()
