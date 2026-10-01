"""인천광역시 버스정보 (apis.data.go.kr/6280000, XML 전용).

대상 정류장의 인천 노선은 서울/경기 API의 경유노선 목록에서 이름을 얻어 find_routes_by_name으로 찾는다.
"""
from __future__ import annotations

from pyproj import Transformer

from ..models import Provider, RouteRef, RouteStop, VehiclePosition
from .base import ApiError, BaseProvider, pick, to_float, to_int, xml_items

BASE = "https://apis.data.go.kr/6280000"
_NO_DATA = {"3", "4"}

# 명세상 POSX/POSY는 Bessel TM(중부원점 127°) 좌표 → EPSG:2097로 보고 WGS84로 변환
_TM_TO_WGS84 = Transformer.from_crs("EPSG:2097", "EPSG:4326", always_xy=True)


def to_wgs84(x: float | None, y: float | None) -> tuple[float | None, float | None]:
    """(lat, lon) 반환. 이미 경위도(|x|<=180)로 오면 그대로 사용."""
    if x is None or y is None:
        return None, None
    if abs(x) <= 180 and abs(y) <= 90:
        return y, x
    lon, lat = _TM_TO_WGS84.transform(x, y)
    return lat, lon


class IncheonProvider(BaseProvider):
    provider = Provider.INCHEON

    async def _call(self, path: str, **params) -> list[dict]:
        params.update(serviceKey=self.api_key, numOfRows=1000, pageNo=1)
        text = await self._get_text(f"{BASE}/{path}", params)
        try:
            header, items = xml_items(text)
        except Exception as e:  # noqa: BLE001 - XML 파싱 실패는 모두 API 오류로
            raise ApiError(f"incheon {path}: bad XML {text[:200]!r}") from e
        code = str(header.get("resultCode", "0"))
        if code in _NO_DATA:
            return []
        if code != "0":
            raise ApiError(f"incheon {path}: resultCode={code} {header.get('resultMsg')}")
        return items

    async def find_routes_by_name(self, name):
        rows = await self._call("busRouteService/getBusRouteNo", routeNo=name)
        return [
            RouteRef(Provider.INCHEON, str(pick(r, "ROUTEID")), str(pick(r, "ROUTENO", default=name)),
                     pick(r, "ROUTETPCD"))
            for r in rows
            if pick(r, "ROUTEID")
        ]

    async def route_stops(self, route_id):
        rows = await self._call("busRouteService/getBusRouteSectionList", routeId=route_id)
        stops = []
        for r in rows:
            seq = to_int(pick(r, "BSTOPSEQ", "PATHSEQ"))
            if seq is None:
                continue
            lat, lon = to_wgs84(to_float(pick(r, "POSX")), to_float(pick(r, "POSY")))
            stops.append(
                RouteStop(
                    seq=seq,
                    station_id=str(pick(r, "BSTOPID")),
                    station_name=str(pick(r, "BSTOPNM", default="")),
                    ars_id=pick(r, "SHORT_BSTOPID"),
                    lat=lat,
                    lon=lon,
                )
            )
        return sorted(stops, key=lambda s: s.seq)

    async def vehicle_positions(self, route_id):
        rows = await self._call("busLocationService/getBusRouteLocation", routeId=route_id)
        out = []
        for r in rows:
            seq = to_int(pick(r, "LATEST_STOPSEQ", "PATHSEQ"))
            veh = pick(r, "BUSID", "BUS_NUM_PLATE")
            if seq is None or not veh:
                continue
            out.append(
                VehiclePosition(
                    vehicle_id=str(veh),
                    plate_no=pick(r, "BUS_NUM_PLATE"),
                    seq=seq,
                    station_id=pick(r, "LATEST_STOP_ID"),
                    section_frac=None,  # 인천은 정차 여부 미제공 → 구간 중간 가정
                    raw=r,
                )
            )
        return out
