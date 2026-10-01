"""수집 대상: card.suseo_target_sttn(sttn_type='B') 정류장을 지나는 모든 노선 후보.

- ARS가 있으면 서울 API(arsId)와 경기 API(mobileNo→stationId)로 경유 노선을 조회한다.
- ARS가 없으면 서울 API 좌표 검색으로 가장 가까운 정류장의 ARS를 쓴다.
- 후보의 source는 항상 ARS다. discovery가 이름만 아는 교차기관 노선을 해석할 때
  노선 정류장 목록의 ARS와 대조하는 데 쓴다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .models import Provider, RouteCandidate
from .providers import ApiError

log = logging.getLogger(__name__)

TARGET_STOP_TYPE = "B"
NEAREST_RADIUS_M = 40
RETRIES = 4
RETRY_DELAY_SEC = 5.0  # 서울 정류소 API는 연속 호출 시 간헐적으로 요청제한 오류를 준다
PACE_SEC = 1.0  # 정류소 조회 사이 간격 (수집 중 위치 조회 속도에는 영향 없음)


@dataclass(frozen=True)
class TargetStop:
    name: str
    ars_id: str | None
    lat: float | None
    lon: float | None


async def load_target_stops(conn) -> list[TargetStop]:
    rows = await conn.fetch(
        """SELECT DISTINCT sttn_nm, NULLIF(btrim(sttn_ars_num), '') AS ars, sttn_lat, sttn_lon
           FROM card.suseo_target_sttn WHERE sttn_type = $1 ORDER BY 1, 2""", TARGET_STOP_TYPE)
    return [TargetStop(r["sttn_nm"], r["ars"], float(r["sttn_lat"]) if r["sttn_lat"] is not None else None,
                       float(r["sttn_lon"]) if r["sttn_lon"] is not None else None) for r in rows]


async def _retry(fn, *args):
    for attempt in range(RETRIES):
        try:
            result = await fn(*args)
            await asyncio.sleep(PACE_SEC)
            return result
        except ApiError as e:
            if attempt == RETRIES - 1 or "no exact mobileNo match" in str(e):
                raise
            await asyncio.sleep(RETRY_DELAY_SEC * (attempt + 1))


async def target_candidates(stops: list[TargetStop], providers) -> tuple[list[RouteCandidate], list[str]]:
    """(노선 후보, 오류 메시지). 일부 실패해도 나머지 후보는 돌려준다."""
    seoul, gyeonggi = providers.get(Provider.SEOUL), providers.get(Provider.GYEONGGI)
    errors: list[str] = []
    ars_ids: set[str] = set()
    for s in stops:
        if s.ars_id:
            ars_ids.add(s.ars_id)
            continue
        ars = None
        if seoul and s.lat is not None and s.lon is not None:
            try:
                ars = await _retry(seoul.nearest_ars, s.lat, s.lon, NEAREST_RADIUS_M)
            except ApiError as e:
                errors.append(f"{s.name}: nearest stop lookup failed: {e}")
                continue
        if ars:
            ars_ids.add(ars)
        else:
            errors.append(f"{s.name}({s.lat},{s.lon}): no ARS within {NEAREST_RADIUS_M}m")

    out: list[RouteCandidate] = []
    for ars in sorted(ars_ids):
        if seoul:
            try:
                out += await _retry(seoul.routes_at_stop, ars)
            except ApiError as e:
                errors.append(f"seoul {ars}: {e}")
        if gyeonggi:
            try:
                station_id, _ = await _retry(gyeonggi.resolve_stop, ars, "")
            except ApiError as e:
                if "no exact mobileNo match" not in str(e):  # 경기 BIS에 없는 서울 정류장은 정상
                    errors.append(f"gyeonggi {ars}: {e}")
                continue
            try:
                for c in await _retry(gyeonggi.routes_at_stop, station_id):
                    c.source = ars
                    out.append(c)
            except ApiError as e:
                errors.append(f"gyeonggi {ars}: {e}")
    for e in errors:
        log.warning("target lookup: %s", e)
    log.info("target stops: %d rows → %d ARS, %d route candidates, %d errors",
             len(stops), len(ars_ids), len(out), len(errors))
    return out, errors
