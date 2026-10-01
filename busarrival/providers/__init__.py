from __future__ import annotations

import httpx

from ..config import Config
from ..models import Provider
from .base import ApiError, BaseProvider
from .gyeonggi import GyeonggiProvider
from .incheon import IncheonProvider
from .seoul import SeoulProvider

_CLASSES: dict[Provider, type[BaseProvider]] = {
    Provider.SEOUL: SeoulProvider,
    Provider.GYEONGGI: GyeonggiProvider,
    Provider.INCHEON: IncheonProvider,
}


def build_providers(cfg: Config, client: httpx.AsyncClient) -> dict[Provider, BaseProvider]:
    """인증키가 설정된 기관만 생성."""
    out = {}
    for p, cls in _CLASSES.items():
        key = cfg.api_key(p)
        if key:
            out[p] = cls(client, key, concurrency=cfg.max_concurrency_per_provider)
    return out


__all__ = ["ApiError", "BaseProvider", "build_providers"]
