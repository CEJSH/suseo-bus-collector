"""경기도 버스정보시스템 GBIS (apis.data.go.kr/6410000, v2 JSON 서비스).

GBIS는 경기 노선뿐 아니라 서울 경계 정류장과 그곳을 지나는 서울/인천 노선도 알고 있다.
노선 관할(districtCd: 1 서울, 2 경기, 3 인천)을 보고 실제 위치 조회를 어느 기관에 할지 정한다.
"""
from __future__ import annotations

import json

from ..geo import haversine_m
from ..models import Provider, RouteCandidate, RouteRef, RouteStop, VehiclePosition
from .base import ApiError, BaseProvider, as_list, pick, to_float, to_int

BASE = "https://apis.data.go.kr/6410000"

_DISTRICT = {"1": Provider.SEOUL, "2": Provider.GYEONGGI, "3": Provider.INCHEON}
# stateCd: 0 교차로 통과, 1 정류소 도착, 2 정류소 출발
_STATE_FRAC = {"1": 0.0, "2": 0.05}


MATCH_RADIUS_M = 150


def _within(row: dict, near: tuple[float, float]) -> bool:
    lat, lon = to_float(pick(row, "y")), to_float(pick(row, "x"))
    return lat is not None and lon is not None and haversine_m(near[0], near[1], lat, lon) <= MATCH_RADIUS_M


def provider_from_route_id(route_id: str) -> Provider:
    """districtCd를 못 얻었을 때의 보조 규칙 (GBIS 노선ID 체계: 1xx 서울, 16x 인천, 2xx 경기)."""
    if route_id.startswith("16"):
        return Provider.INCHEON
    if route_id.startswith("1"):
        return Provider.SEOUL
    return Provider.GYEONGGI


class GyeonggiProvider(BaseProvider):
    provider = Provider.GYEONGGI

    async def resolve_stop(self, ars_id, station_name, near=None):
        """ARS(mobileNo)로 경기 stationId를 찾는다. ARS는 지역 간 중복되므로 near=(lat, lon)가 있으면
        그 좌표에서 MATCH_RADIUS_M 안의 정류장만 인정한다."""
        if not ars_id:
            raise ApiError("gyeonggi: ARS/mobile number missing")
        numbers = {n.strip() for n in ars_id.split('|') if n.strip()}
        rows = []
        for number in sorted(numbers):
            rows.extend(as_list(await self._call("busstationservice/v2/getBusStationListv2", "busStationList", keyword=number)))
        matches = {str(pick(row, "stationId")): row for row in rows
                   if pick(row, "stationId") and str(pick(row, "mobileNo", default="")).strip() in numbers
                   and (near is None or _within(row, near))}
        if len(matches) == 1:
            sid, row = next(iter(matches.items()))
            return sid, str(pick(row, "stationName", default=station_name))
        raise ApiError(f"gyeonggi: no exact mobileNo match for {ars_id}")

    async def routes_at_stop(self, station_id):
        rows = as_list(await self._call("busstationservice/v2/getBusStationViaRouteListv2", "busRouteList", stationId=station_id))
        out = []
        for r in rows:
            rid, name = pick(r, "routeId"), pick(r, "routeName")
            if not rid or not name:
                continue
            prov = await self.route_district(str(rid))
            out.append(RouteCandidate(prov, str(name), str(rid) if prov is Provider.GYEONGGI else None,
                                      str(pick(r, "routeTypeCd", default="")), station_id))
        return out

    async def _call(self, path: str, body_key: str | tuple[str, ...], **params):
        params.update(serviceKey=self.api_key, format="json")
        text = await self._get_text(f"{BASE}/{path}", params)
        try:
            data = json.loads(text)["response"]
        except (json.JSONDecodeError, KeyError) as e:
            raise ApiError(f"gyeonggi {path}: unexpected response {text[:200]!r}") from e
        hdr = data.get("msgHeader") or {}
        code = str(hdr.get("resultCode", ""))
        if code == "4":  # 결과 없음
            return []
        if code != "0":
            raise ApiError(f"gyeonggi {path}: resultCode={code} {hdr.get('resultMessage')}")
        body = data.get("msgBody") or {}
        for k in (body_key,) if isinstance(body_key, str) else body_key:
            if k in body:
                return body[k]
        return None

    async def route_district(self, route_id: str) -> Provider:
        try:
            item = await self._call("busrouteservice/v2/getBusRouteInfoItemv2", "busRouteInfoItem", routeId=route_id)
            item = as_list(item)[0] if item else {}
            prov = _DISTRICT.get(str(pick(item, "districtCd", default="")))
            if prov:
                return prov
        except ApiError:
            pass
        return provider_from_route_id(route_id)

    async def find_routes_by_name(self, name):
        rows = as_list(await self._call("busrouteservice/v2/getBusRouteListv2", "busRouteList", keyword=name))
        out = []
        for r in rows:
            rid = str(pick(r, "routeId"))
            if _DISTRICT.get(str(pick(r, "districtCd", default=""))) not in (None, Provider.GYEONGGI):
                continue
            out.append(RouteRef(Provider.GYEONGGI, rid, str(pick(r, "routeName")),
                                str(pick(r, "routeTypeCd", default="")) or None))
        return out

    async def route_stops(self, route_id):
        rows = as_list(
            await self._call("busrouteservice/v2/getBusRouteStationListv2", "busRouteStationList", routeId=route_id)
        )
        stops = []
        for r in rows:
            seq = to_int(pick(r, "stationSeq"))
            if seq is None:
                continue
            stops.append(
                RouteStop(
                    seq=seq,
                    station_id=str(pick(r, "stationId")),
                    station_name=str(pick(r, "stationName", default="")),
                    ars_id=str(pick(r, "mobileNo", default="")).strip() or None,
                    lat=to_float(pick(r, "y")),
                    lon=to_float(pick(r, "x")),
                )
            )
        return sorted(stops, key=lambda s: s.seq)

    async def vehicle_positions(self, route_id):
        rows = as_list(
            await self._call("buslocationservice/v2/getBusLocationListv2", "busLocationList", routeId=route_id)
        )
        out = []
        for r in rows:
            seq = to_int(pick(r, "stationSeq"))
            plate = pick(r, "plateNo")
            veh = pick(r, "vehId", default=plate)
            if seq is None or not veh:
                continue
            state = str(pick(r, "stateCd", default=""))
            out.append(
                VehiclePosition(
                    vehicle_id=str(veh),
                    plate_no=plate,
                    seq=seq,
                    station_id=pick(r, "stationId"),
                    section_frac=_STATE_FRAC.get(state),
                    raw=r,
                )
            )
        return out
