"""도메인 모델. 외부 의존성 없음 (detector 단위 테스트가 표준 라이브러리만으로 돌도록)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# 운행일(service_date) 경계: 새벽 4시 이전 도착은 전날 운행분으로 본다.
SERVICE_DAY_START_HOUR = 4


class Provider(str, Enum):
    SEOUL = "seoul"
    GYEONGGI = "gyeonggi"
    INCHEON = "incheon"


@dataclass(frozen=True)
class RouteRef:
    """수집 대상 노선. route_id는 provider(노선 관할 기관) API의 고유 ID."""

    provider: Provider
    route_id: str
    route_name: str
    route_type: str | None = None


@dataclass
class RouteCandidate:
    """탐색 단계에서 발견한 노선 후보.

    route_id가 None이면 다른 기관 API에서 이름만 알게 된 노선이라
    provider API에서 이름으로 다시 찾아야 한다(resolution).
    """

    provider: Provider
    route_name: str
    route_id: str | None
    route_type: str | None
    source: str  # 어느 API/정류장에서 발견했는지 (로그/감사용)


@dataclass
class RouteStop:
    seq: int
    station_id: str
    station_name: str
    ars_id: str | None = None  # 서울 arsId == 경기 mobileNo (5자리 정류장 번호). 기관 간 공통 키.
    lat: float | None = None
    lon: float | None = None
    cum_dist_m: float = 0.0


@dataclass
class VehiclePosition:
    """노선 위치 API 한 행을 공통 형태로 정규화한 것.

    seq: 버스가 마지막으로 도착/통과한 정류장 순번.
    section_frac: seq 정류장 ~ 다음 정류장 구간에서의 위치(0=정류장에 있음).
                  기관이 정보를 안 주면 None → 구간 중간(0.5)으로 가정.
    """

    vehicle_id: str
    plate_no: str | None
    seq: int
    station_id: str | None = None
    section_frac: float | None = None
    data_time: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class VehicleState:
    vehicle_id: str
    plate_no: str | None
    trip_id: str
    trip_started_at: datetime
    last_seq: int
    last_frac: float | None
    last_obs_time: datetime  # last_seq에서 마지막으로 관측된 시각(다음 도착의 구간 하한)
    last_seen_at: datetime  # 어떤 형태로든 마지막으로 보인 시각


class ArrivalMethod(str, Enum):
    STOP_OBSERVED = "stop_observed"  # 정류장에 정차 중인 상태로 관측됨
    INTERPOLATED = "interpolated"  # 두 관측 사이 거리 기반 보간


@dataclass
class ArrivalEvent:
    provider: Provider
    route_id: str
    route_name: str
    vehicle_id: str
    plate_no: str | None
    trip_id: str
    station_seq: int
    station_id: str
    ars_id: str | None
    station_name: str
    arrived_at: datetime  # 추정 도착 시각
    window_start: datetime  # 실제 도착은 (window_start, window_end] 안에 있음
    window_end: datetime
    method: ArrivalMethod

    @property
    def service_date(self) -> date:
        local = self.arrived_at.astimezone(KST)
        return (local - timedelta(hours=SERVICE_DAY_START_HOUR)).date()
