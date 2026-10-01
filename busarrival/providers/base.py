from __future__ import annotations

import asyncio
import logging
import random
import time
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..models import KST, Provider, RouteCandidate, RouteRef, RouteStop, VehiclePosition

log = logging.getLogger(__name__)


class ApiError(Exception):
    """기관 API가 오류 코드를 반환(인증키 오류, 트래픽 초과 등)."""



def as_list(v: Any) -> list[dict]:
    """data.go.kr JSON은 결과가 1건이면 리스트 대신 dict를 주는 경우가 있다."""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    return [v]


def pick(d: dict, *keys: str, default: Any = None) -> Any:
    """대소문자 무시 키 조회. 여러 후보 키 중 처음 값이 있는 것을 반환."""
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, ""):
            return v
    return default


def to_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f != 0.0 else None  # 좌표 0은 결측으로 취급


def to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_kst(v: Any, fmt: str = "%Y%m%d%H%M%S") -> datetime | None:
    if not v:
        return None
    s = re.sub(r"\D", "", str(v))[:14]
    try:
        return datetime.strptime(s, fmt).replace(tzinfo=KST)
    except ValueError:
        return None


def xml_items(text: str, item_tag: str = "itemList") -> tuple[dict, list[dict]]:
    """<msgHeader>와 <itemList> 반복 요소를 dict로 변환."""
    root = ET.fromstring(text)
    header = {}
    h = root.find(".//msgHeader")
    if h is not None:
        header = {c.tag: (c.text or "").strip() for c in h}
    items = [{c.tag: (c.text or "").strip() for c in el} for el in root.iter(item_tag)]
    return header, items


def _field(text: str, name: str) -> str | None:
    """XML(<name>v</name>)과 JSON("name": "v") 양쪽에서 값 추출."""
    m = re.search(rf"<{name}>(.*?)</{name}>", text) or re.search(rf'"{name}"\s*:\s*"(.*?)"', text)
    return m.group(1) if m else None


def check_gateway_error(text: str) -> None:
    """data.go.kr 게이트웨이 공통 오류(키 미등록 30, 트래픽 초과 22 등). XML 또는 JSON으로 온다."""
    if "OpenAPI_ServiceResponse" in text or "returnAuthMsg" in text:
        code = _field(text, "returnReasonCode") or "?"
        msg = _field(text, "returnAuthMsg") or _field(text, "errMsg") or text[:200]
        raise ApiError(f"gateway error {code}: {msg}")
    if text.strip().startswith(("Unauthorized", "SERVICE_KEY", "LIMITED_NUMBER")):
        raise ApiError(text.strip()[:200])
    msg = _field(text, "message")
    if '"error"' in text and msg:  # 서울 TOPIS: {"error":"Unauthorized","message":"...","status":401}
        raise ApiError(msg)


class BaseProvider(ABC):
    provider: Provider

    def __init__(self, client: httpx.AsyncClient, api_key: str, concurrency: int = 6, retries: int = 3):
        self.client = client
        self.api_key = api_key
        self.sem = asyncio.Semaphore(concurrency)
        self.retries = retries
        self.calls = 0  # 호출 수 (쿼터 모니터링)
        self.request_interval_sec = 0.0
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._blocked_until = 0.0

    async def _get_text(self, url: str, params: dict) -> str:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self.sem:
                    if time.monotonic() < self._blocked_until:
                        raise ApiError("HTTP 429: provider cooldown active")
                    if self.request_interval_sec:
                        async with self._request_lock:
                            delay = self.request_interval_sec - (time.monotonic() - self._last_request_at)
                            if delay > 0:
                                await asyncio.sleep(delay)
                            self._last_request_at = time.monotonic()
                    self.calls += 1
                    r = await self.client.get(url, params=params)
                if r.status_code == 429:
                    self._blocked_until = time.monotonic() + 300
                    raise ApiError("HTTP 429: provider paused for 300 seconds")
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                text = r.text
                check_gateway_error(text)
                if r.status_code >= 400:  # 인증 오류 등 4xx는 재시도해도 소용없음
                    raise ApiError(f"HTTP {r.status_code}: {text[:200]}")
                return text
            except ApiError as e:
                raise ApiError(f"{self.provider.value} {url.rsplit('/', 1)[-1]}: {self._redact(str(e))}") from None
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                await asyncio.sleep((2**attempt) + random.random())
        raise ApiError(f"{self.provider.value} {url.rsplit('/', 1)[-1]}: {self._redact(repr(last))}")

    async def resolve_stop(self, ars_id: str | None, station_name: str) -> tuple[str, str]:
        """원본 정류장의 ARS/이름에서 기관 내부 정류장 ID를 얻는다."""
        raise ApiError(f"{self.provider.value}: stop mapping is not implemented")

    async def routes_at_stop(self, station_id: str) -> list[RouteCandidate]:
        raise ApiError(f"{self.provider.value}: route lookup is not implemented")

    def _redact(self, s: str) -> str:
        """오류 메시지/로그에 인증키(원문·URL 인코딩)가 남지 않게 가린다."""
        for k in {self.api_key, quote(self.api_key, safe="")}:
            s = s.replace(k, "<KEY>")
        return s

    # --- 수집에 필요한 기능 --------------------------------------
    @abstractmethod
    async def find_routes_by_name(self, name: str) -> list[RouteRef]:
        """노선번호(이름)로 이 기관의 노선 검색."""

    @abstractmethod
    async def route_stops(self, route_id: str) -> list[RouteStop]:
        """노선의 전체 경유 정류장 (순번순)."""

    @abstractmethod
    async def vehicle_positions(self, route_id: str) -> list[VehiclePosition]:
        """노선의 현재 운행 차량 위치."""
