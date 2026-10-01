"""서울특별시 버스정보시스템 (ws.bus.go.kr, 공공데이터포털 '서울특별시_노선정보/정류소정보/버스위치정보 조회 서비스')."""
from __future__ import annotations

import json
import re

from ..models import Provider, RouteCandidate, RouteRef, RouteStop, VehiclePosition
from .base import ApiError, BaseProvider, as_list, parse_kst, pick, to_float, to_int

BASE = "http://ws.bus.go.kr/api/rest"

# busRouteType: 1 공항, 2 마을, 3 간선, 4 지선, 5 순환, 6 광역, 7 인천, 8 경기, 9 폐지, 0 공용
_TYPE_PROVIDER = {"7": Provider.INCHEON, "8": Provider.GYEONGGI}
# 서울 API는 경기/인천 노선명 뒤에 지역명을 붙인다: "1007광주", "M5333(예약)안양" → "1007", "M5333(예약)"
_REGION_SUFFIX = re.compile(r"(?<=[0-9A-Za-z)])[가-힣]+$")


def strip_region_suffix(name: str) -> str:
    return _REGION_SUFFIX.sub("", name)


def section_frac(r: dict) -> float | None:
    """정차 중이면 0, 아니면 구간 내 진행 비율(sectDist/fullSectDist, km 단위). 값이 없으면 None."""
    if str(pick(r, "stopFlag", default="0")) == "1":
        return 0.0
    dist, full = to_float(pick(r, "sectDist")), to_float(pick(r, "fullSectDist"))
    if dist is None or not full:
        return None
    return min(max(dist / full, 0.0), 0.99)


class SeoulProvider(BaseProvider):
    provider = Provider.SEOUL

    async def routes_at_stop(self, station_id):
        out = []
        for r in await self._call("stationinfo/getRouteByStation", arsId=station_id):
            kind = str(pick(r, "busRouteType", default=""))
            prov = _TYPE_PROVIDER.get(kind, Provider.SEOUL)
            name = pick(r, "busRouteNm")
            rid = pick(r, "busRouteId")
            if not name or not rid:
                continue
            out.append(RouteCandidate(prov, name if prov is Provider.SEOUL else strip_region_suffix(name),
                                      str(rid) if prov is Provider.SEOUL else None, kind, station_id))
        return out

    async def _call(self, path: str, **params) -> list[dict]:
        params.update(serviceKey=self.api_key, resultType="json")
        text = await self._get_text(f"{BASE}/{path}", params)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ApiError(f"seoul {path}: non-JSON response {text[:200]!r}") from e
        hdr = data.get("msgHeader") or {}
        code = str(hdr.get("headerCd", ""))
        if code == "4":  # 결과 없음
            return []
        if code != "0":
            raise ApiError(f"seoul {path}: headerCd={code} {hdr.get('headerMsg')}")
        return as_list((data.get("msgBody") or {}).get("itemList"))

    async def nearest_ars(self, lat, lon, radius_m):
        """좌표 반경 내 가장 가까운 정류장의 ARS. 없으면 None."""
        rows = [r for r in await self._call("stationinfo/getStationByPos", tmX=lon, tmY=lat, radius=radius_m)
                if pick(r, "arsId") not in (None, "0")]
        if not rows:
            return None
        return str(pick(min(rows, key=lambda r: to_float(pick(r, "dist")) or 0.0), "arsId"))

    async def find_routes_by_name(self, name):
        rows = await self._call("busRouteInfo/getBusRouteList", strSrch=name)
        return [
            RouteRef(Provider.SEOUL, str(pick(r, "busRouteId")), str(pick(r, "busRouteNm")),
                     str(pick(r, "routeType", "busRouteType", default="")) or None)
            for r in rows
        ]

    async def route_stops(self, route_id):
        rows = await self._call("busRouteInfo/getStaionByRoute", busRouteId=route_id)  # API명 오타 그대로
        stops = []
        for r in rows:
            seq = to_int(pick(r, "seq"))
            if seq is None:
                continue
            stops.append(
                RouteStop(
                    seq=seq,
                    station_id=str(pick(r, "station", "stId")),
                    station_name=str(pick(r, "stationNm", default="")),
                    ars_id=pick(r, "arsId"),
                    lat=to_float(pick(r, "gpsY")),
                    lon=to_float(pick(r, "gpsX")),
                )
            )
        return sorted(stops, key=lambda s: s.seq)

    async def vehicle_positions(self, route_id):
        rows = await self._call("buspos/getBusPosByRtid", busRouteId=route_id)
        out = []
        for r in rows:
            if str(pick(r, "isrunyn", default="1")) == "0":
                continue
            seq = to_int(pick(r, "sectOrd"))  # 구간순번 = 마지막 도착/통과 정류장 순번
            veh = pick(r, "vehId")
            if seq is None or not veh:
                continue
            out.append(
                VehiclePosition(
                    vehicle_id=str(veh),
                    plate_no=pick(r, "plainNo"),
                    seq=seq,
                    station_id=pick(r, "lastStnId"),
                    section_frac=section_frac(r),
                    data_time=parse_kst(pick(r, "dataTm")),
                    raw=r,
                )
            )
        return out
