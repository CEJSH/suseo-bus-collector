from __future__ import annotations

import math

from .models import RouteStop

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def fill_cumulative_distance(stops: list[RouteStop]) -> None:
    """정류장 간 직선거리 누적값을 채운다. 좌표가 하나라도 없으면 순번 기반(정류장당 1)으로 대체."""
    if all(s.lat is not None and s.lon is not None for s in stops):
        total = 0.0
        for i, s in enumerate(stops):
            if i > 0:
                prev = stops[i - 1]
                total += haversine_m(prev.lat, prev.lon, s.lat, s.lon)  # type: ignore[arg-type]
            s.cum_dist_m = total
    else:
        for i, s in enumerate(stops):
            s.cum_dist_m = float(i)

