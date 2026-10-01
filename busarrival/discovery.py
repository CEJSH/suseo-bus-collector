"""대상 정류장 경유 노선 후보 → 관할 기관 노선 ID와 전체 정류장 목록 확정.

1. 후보(targets.target_candidates)는 서울 busRouteType / 경기 districtCd로 관할 기관이 정해져 있다.
2. 관할 기관 ID를 모르는 후보(예: 서울 API에서 본 경기 노선)는 관할 기관 API에서 이름으로 검색하고,
   그중 실제로 후보를 발견한 정류장(ARS)을 지나는 노선만 채택한다 (동명이노선 제거).
3. 관할 기관 API로 전체 정류장 목록 조회 + 누적거리 계산.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import Config
from .geo import fill_cumulative_distance
from .models import Provider, RouteCandidate, RouteRef, RouteStop
from .providers import ApiError, BaseProvider

log = logging.getLogger(__name__)

PROXIMITY_SLACK_M = 150  # 이름 검색으로 찾은 노선의 반경 판정 여유


@dataclass
class DiscoveredRoute:
    route: RouteRef
    stops: list[RouteStop]
    sources: list[str]


def norm_name(name: str) -> str:
    return re.sub(r"[\s\-_]", "", name or "").upper()


async def discover_routes(cfg: Config, providers: dict[Provider, BaseProvider],
                          candidates: list[RouteCandidate]) -> list[DiscoveredRoute]:
    candidates = list(candidates)

    for m in cfg.include_routes:
        candidates.append(RouteCandidate(m.provider, m.route_name, m.route_id, None, "config"))

    only = {norm_name(n) for n in cfg.only_route_names}
    excluded = {norm_name(n) for n in cfg.exclude_route_names}

    # (provider, route_id) → sources
    resolved: dict[tuple[Provider, str], tuple[RouteRef, list[str]]] = {}
    unresolved: dict[tuple[Provider, str], list[str]] = {}
    for c in candidates:
        n = norm_name(c.route_name)
        if n in excluded or (only and n not in only):
            continue
        if c.route_id:
            key = (c.provider, c.route_id)
            ref, srcs = resolved.setdefault(key, (RouteRef(c.provider, c.route_id, c.route_name, c.route_type), []))
            srcs.append(c.source)
        else:
            unresolved.setdefault((c.provider, c.route_name), []).append(c.source)

    stops_cache: dict[tuple[Provider, str], list[RouteStop]] = {}

    async def load_stops(ref: RouteRef) -> list[RouteStop]:
        key = (ref.provider, ref.route_id)
        if key not in stops_cache:
            stops = await providers[ref.provider].route_stops(ref.route_id)
            fill_cumulative_distance(stops)
            stops_cache[key] = stops
        return stops_cache[key]

    # 이름만 아는 후보 → 관할 기관 API에서 검색
    have_names = {(p, norm_name(ref.route_name)) for (p, _), (ref, _) in resolved.items()}
    for (prov, name), srcs in unresolved.items():
        if (prov, norm_name(name)) in have_names:
            for (p, _), (ref, s) in resolved.items():
                if p is prov and norm_name(ref.route_name) == norm_name(name):
                    s.extend(srcs)
            continue
        if prov not in providers:
            log.warning("route %s belongs to %s but no API key configured — skipped", name, prov.value)
            continue
        try:
            matches = [r for r in await providers[prov].find_routes_by_name(name)
                       if norm_name(r.route_name) == norm_name(name)]
        except ApiError as e:
            log.error("resolve %s/%s failed: %s", prov.value, name, e)
            continue
        hit = False
        for ref in matches:
            try:
                stops = await load_stops(ref)
            except ApiError as e:
                log.error("stops %s/%s failed: %s", prov.value, ref.route_id, e)
                continue
            if any(s.ars_id and s.ars_id.strip() in srcs for s in stops):
                resolved[(prov, ref.route_id)] = (ref, list(srcs))
                hit = True
        if not hit:
            log.warning("could not resolve %s route '%s' (found %d same-name routes, no matching source ARS)",
                        prov.value, name, len(matches))

    out: list[DiscoveredRoute] = []
    for (prov, rid), (ref, srcs) in sorted(resolved.items(), key=lambda kv: (kv[0][0].value, kv[1][0].route_name)):
        if prov not in providers:
            log.warning("route %s (%s) needs %s API key — skipped", ref.route_name, rid, prov.value)
            continue
        try:
            stops = await load_stops(ref)
        except ApiError as e:
            log.error("stops %s/%s failed: %s", prov.value, rid, e)
            continue
        if not stops:
            log.warning("route %s (%s) has no stops — skipped", ref.route_name, rid)
            continue
        out.append(DiscoveredRoute(ref, stops, sorted(set(srcs))))
    log.info("discovery done: %d routes, %d route-stops", len(out), sum(len(r.stops) for r in out))
    return out
