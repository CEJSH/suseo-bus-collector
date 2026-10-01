from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import unquote

import yaml

from .detector import DetectorParams
from .models import Provider


@dataclass
class ManualRoute:
    provider: Provider
    route_id: str
    route_name: str


@dataclass
class Config:
    database_url: str = "postgresql://localhost/postgres"
    db_schema: str = "tmp"

    # data.go.kr / 서울 TOPIS 인증키 (Decoding 키 권장. Encoding 키를 넣어도 자동 디코딩)
    seoul_api_key: str | None = None
    gyeonggi_api_key: str | None = None
    incheon_api_key: str | None = None
    realtime_duration_hours: float = 24.0

    poll_interval_sec: int = 30
    idle_interval_sec: int = 300  # 차량 0대가 계속되는 노선(심야 등)은 이 주기로만 조회
    idle_after_empty_polls: int = 10
    refresh_hour: int = 3  # 매일 이 시각(KST)에 노선/정류장 목록 재탐색
    http_timeout_sec: float = 10.0
    max_concurrency_per_provider: int = 6
    daily_quota: dict[str, int] = field(
        default_factory=lambda: {"seoul": 100_000, "gyeonggi": 100_000, "incheon": 100_000}
    )

    detector: DetectorParams = field(default_factory=DetectorParams)

    include_routes: list[ManualRoute] = field(default_factory=list)
    exclude_route_names: list[str] = field(default_factory=list)
    only_route_names: list[str] = field(default_factory=list)  # 비어있지 않으면 이 노선들만

    save_raw_positions: bool = False
    poll_log_retention_days: int = 14
    log_level: str = "INFO"

    def api_key(self, provider: Provider) -> str | None:
        return {
            Provider.SEOUL: self.seoul_api_key,
            Provider.GYEONGGI: self.gyeonggi_api_key,
            Provider.INCHEON: self.incheon_api_key,
        }[provider]


_ENV = {
    "DATABASE_URL": "database_url",
    "SEOUL_API_KEY": "seoul_api_key",
    "GYEONGGI_API_KEY": "gyeonggi_api_key",
    "INCHEON_API_KEY": "incheon_api_key",
}


def _normalize_key(key: str | None) -> str | None:
    if not key:
        return None
    key = key.strip()
    # 포털의 'Encoding' 키(%2B 등 포함)를 넣었으면 디코딩 — httpx가 다시 인코딩한다.
    return unquote(key) if "%" in key else key


def load_dotenv(path: Path) -> None:
    """KEY=VALUE 형식의 .env를 읽어 os.environ에 넣는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_config(path: str | Path | None) -> Config:
    # 현재 디렉터리와 설정 파일 옆의 .env (systemd의 EnvironmentFile과 같은 파일)
    load_dotenv(Path.cwd() / ".env")
    if path:
        load_dotenv(Path(path).resolve().parent / ".env")

    raw: dict = {}
    if path and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")

    det = DetectorParams(**(raw.pop("detector", None) or {}))
    manual = [
        ManualRoute(Provider(r["provider"]), str(r["route_id"]), str(r.get("route_name", r["route_id"])))
        for r in raw.pop("include_routes", None) or []
    ]
    cfg = Config(**raw, detector=det, include_routes=manual)
    cfg.db_schema = "tmp"  # 사용자 지정: 모든 신규 적재는 tmp 스키마만 사용

    for env, attr in _ENV.items():
        if os.environ.get(env):
            setattr(cfg, attr, os.environ[env])
    for p in Provider:
        attr = f"{p.value}_api_key"
        setattr(cfg, attr, _normalize_key(getattr(cfg, attr)))
    return cfg
