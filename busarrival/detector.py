"""노선별 차량 위치 스냅샷 → 정류장 도착 이벤트 변환 (순수 로직, I/O 없음).

원리
----
각 기관의 '노선별 버스 위치' API는 차량마다 "마지막으로 도착/통과한 정류장 순번(seq)"을 준다.
직전 관측(t0, seq0)과 이번 관측(t1, seq1)을 비교해서 seq1 > seq0 이면
seq0+1 … seq1 정류장에 (t0, t1] 사이에 도착한 것이다.

  * 각 정류장의 도착 시각 = 두 관측 시점의 차량 위치(누적거리)를 선형 보간
  * 이번 관측에서 차량이 seq1 정류장에 정차 중(section_frac=0)이면 seq1 도착 = 관측 시각
  * 실제 도착 시각이 반드시 들어있는 구간 (window_start, window_end]도 함께 저장
    → 분석 시 정밀도 필터링 가능 (30초 폴링이면 대부분 폭 30초 이하)

예외 처리
---------
  * seq가 소폭 감소(GPS 튐)             → 무시
  * seq가 크게 감소 / 오래 안 보임       → 새 운행(trip) 시작, 이벤트 없음
  * 두 관측 간격이 너무 김(API 장애 등)  → 상태만 갱신하고 이벤트 생략(정밀도 보장 불가)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import (
    ArrivalEvent,
    ArrivalMethod,
    RouteRef,
    RouteStop,
    VehiclePosition,
    VehicleState,
)

log = logging.getLogger(__name__)

DEFAULT_FRAC = 0.5  # 구간 내 위치를 모를 때 가정값


@dataclass(frozen=True)
class DetectorParams:
    backtrack_tolerance: int = 2  # 이 정도 이하의 seq 역행은 GPS 노이즈로 간주
    trip_timeout_sec: int = 1800  # 이 시간 이상 안 보이면 다음 등장 시 새 운행
    max_interp_gap_sec: int = 600  # 두 관측 간격이 이보다 길면 이벤트 생성 안 함


def make_trip_id(vehicle_id: str, started_at: datetime) -> str:
    return f"{vehicle_id}@{started_at.strftime('%Y%m%dT%H%M%S')}"


class RouteTracker:
    def __init__(
        self,
        route: RouteRef,
        stops: list[RouteStop],
        params: DetectorParams,
        states: dict[str, VehicleState] | None = None,
    ) -> None:
        self.route = route
        self.params = params
        self.states: dict[str, VehicleState] = dict(states or {})
        self.dirty: set[str] = set()  # DB에 반영해야 할 차량 상태
        self.set_stops(stops)

    def set_stops(self, stops: list[RouteStop]) -> None:
        self.stops = sorted(stops, key=lambda s: s.seq)
        self._idx = {s.seq: i for i, s in enumerate(self.stops)}

    # ------------------------------------------------------------------ public
    def update(self, positions: list[VehiclePosition], observed_at: datetime) -> list[ArrivalEvent]:
        events: list[ArrivalEvent] = []
        seen: set[str] = set()
        for p in positions:
            if p.vehicle_id in seen:  # 같은 응답에 중복 차량이 오는 경우 방어
                continue
            seen.add(p.vehicle_id)
            if p.seq not in self._idx:
                log.debug("%s: vehicle %s seq %s not in route stops", self.route.route_name, p.vehicle_id, p.seq)
                continue
            events.extend(self._update_vehicle(p, p.data_time or observed_at))
        return events

    def expire(self, now: datetime) -> list[str]:
        """오래 안 보인 차량 상태를 제거하고 제거된 vehicle_id 목록을 반환."""
        limit = timedelta(seconds=self.params.trip_timeout_sec * 2)
        gone = [v for v, s in self.states.items() if now - s.last_seen_at > limit]
        for v in gone:
            del self.states[v]
            self.dirty.discard(v)
        return gone

    def pop_dirty(self) -> list[VehicleState]:
        out = [self.states[v] for v in self.dirty if v in self.states]
        self.dirty.clear()
        return out

    # ----------------------------------------------------------------- private
    def _update_vehicle(self, p: VehiclePosition, t: datetime) -> list[ArrivalEvent]:
        st = self.states.get(p.vehicle_id)
        self.dirty.add(p.vehicle_id)

        if st is None or (t - st.last_seen_at).total_seconds() > self.params.trip_timeout_sec:
            self._start_trip(p, t)
            return []

        if t <= st.last_obs_time:  # 갱신 안 된(같은 시각) 데이터
            return []

        cur_i, prev_i = self._idx[p.seq], self._idx.get(st.last_seq)
        if prev_i is None:  # 노선 정류장 목록이 바뀐 경우
            self._start_trip(p, t)
            return []

        delta = cur_i - prev_i
        if delta < 0:
            if -delta <= self.params.backtrack_tolerance:
                st.last_seen_at = t
                return []
            self._start_trip(p, t)  # 종점 회차 후 기점에서 재출발
            return []

        if delta == 0:
            st.last_frac = p.section_frac if p.section_frac is not None else st.last_frac
            st.last_obs_time = t
            st.last_seen_at = t
            st.plate_no = p.plate_no or st.plate_no
            return []

        events: list[ArrivalEvent] = []
        gap = (t - st.last_obs_time).total_seconds()
        if gap <= self.params.max_interp_gap_sec:
            events = self._interpolate(st, prev_i, cur_i, p, t)
        else:
            log.info(
                "%s: vehicle %s gap %.0fs > max, skipping %d stops",
                self.route.route_name, p.vehicle_id, gap, delta,
            )

        st.last_seq = p.seq
        st.last_frac = p.section_frac
        st.last_obs_time = t
        st.last_seen_at = t
        st.plate_no = p.plate_no or st.plate_no
        return events

    def _start_trip(self, p: VehiclePosition, t: datetime) -> None:
        self.states[p.vehicle_id] = VehicleState(
            vehicle_id=p.vehicle_id,
            plate_no=p.plate_no,
            trip_id=make_trip_id(p.vehicle_id, t),
            trip_started_at=t,
            last_seq=p.seq,
            last_frac=p.section_frac,
            last_obs_time=t,
            last_seen_at=t,
        )

    def _position(self, idx: int, frac: float | None) -> float:
        """정류장 인덱스 + 구간 비율 → 누적거리."""
        base = self.stops[idx].cum_dist_m
        if idx + 1 >= len(self.stops):
            return base
        f = DEFAULT_FRAC if frac is None else min(max(frac, 0.0), 1.0)
        return base + f * (self.stops[idx + 1].cum_dist_m - base)

    def _interpolate(
        self, st: VehicleState, prev_i: int, cur_i: int, p: VehiclePosition, t: datetime
    ) -> list[ArrivalEvent]:
        t0 = st.last_obs_time
        span = (t - t0).total_seconds()
        d0 = self._position(prev_i, st.last_frac)
        d1 = self._position(cur_i, p.section_frac)
        events = []
        for i in range(prev_i + 1, cur_i + 1):
            stop = self.stops[i]
            if d1 > d0:
                ratio = min(max((stop.cum_dist_m - d0) / (d1 - d0), 0.0), 1.0)
            else:
                ratio = 1.0
            at_stop_now = i == cur_i and p.section_frac == 0.0
            events.append(
                ArrivalEvent(
                    provider=self.route.provider,
                    route_id=self.route.route_id,
                    route_name=self.route.route_name,
                    vehicle_id=p.vehicle_id,
                    plate_no=p.plate_no or st.plate_no,
                    trip_id=st.trip_id,
                    station_seq=stop.seq,
                    station_id=stop.station_id,
                    ars_id=stop.ars_id,
                    station_name=stop.station_name,
                    arrived_at=t0 + timedelta(seconds=ratio * span),
                    window_start=t0,
                    window_end=t,
                    method=ArrivalMethod.STOP_OBSERVED if at_stop_now else ArrivalMethod.INTERPOLATED,
                )
            )
        return events
