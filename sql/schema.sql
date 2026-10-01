-- 수서역 경유 버스 실도착 수집 스키마
-- 스키마명은 설정(db_schema, 기본 bus_rt)으로 치환된다: {schema}

CREATE SCHEMA IF NOT EXISTS {schema};

-- 수집 대상 노선 (노선 관할 기관 기준 ID)
CREATE TABLE IF NOT EXISTS {schema}.route (
    provider      text        NOT NULL,          -- seoul | gyeonggi | incheon
    route_id      text        NOT NULL,
    route_name    text        NOT NULL,
    route_type    text,
    sources       text[]      NOT NULL DEFAULT '{{}}',  -- 발견 경로 (어느 API/정류장)
    active        boolean     NOT NULL DEFAULT true,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, route_id)
);

-- 노선별 전체 경유 정류장 (매일 재탐색 시 갱신)
CREATE TABLE IF NOT EXISTS {schema}.route_stop (
    provider     text             NOT NULL,
    route_id     text             NOT NULL,
    seq          integer          NOT NULL,
    station_id   text             NOT NULL,
    ars_id       text,                              -- 5자리 정류장 번호 (기관 간 공통 키)
    station_name text             NOT NULL,
    lat          double precision,
    lon          double precision,
    cum_dist_m   double precision NOT NULL,
    PRIMARY KEY (provider, route_id, seq),
    FOREIGN KEY (provider, route_id) REFERENCES {schema}.route ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS route_stop_ars_idx ON {schema}.route_stop (ars_id);

-- ★ 도착 이벤트 (핵심 산출물)
CREATE TABLE IF NOT EXISTS {schema}.arrival_event (
    id            bigserial   PRIMARY KEY,
    provider      text        NOT NULL,
    route_id      text        NOT NULL,
    route_name    text        NOT NULL,
    vehicle_id    text        NOT NULL,
    plate_no      text,
    trip_id       text        NOT NULL,             -- 차량ID@운행시작시각
    station_seq   integer     NOT NULL,
    station_id    text        NOT NULL,
    ars_id        text,
    station_name  text        NOT NULL,
    arrived_at    timestamptz NOT NULL,             -- 추정 도착 시각
    window_start  timestamptz NOT NULL,             -- 실제 도착 ∈ (window_start, window_end]
    window_end    timestamptz NOT NULL,
    method        text        NOT NULL CHECK (method IN ('stop_observed', 'interpolated')),
    service_date  date        NOT NULL,             -- 04시 기준 운행일
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, route_id, trip_id, station_seq)
);
CREATE INDEX IF NOT EXISTS arrival_station_time_idx ON {schema}.arrival_event (station_id, arrived_at);
CREATE INDEX IF NOT EXISTS arrival_ars_time_idx     ON {schema}.arrival_event (ars_id, arrived_at);
CREATE INDEX IF NOT EXISTS arrival_route_date_idx   ON {schema}.arrival_event (provider, route_id, service_date);

-- 차량 추적 상태 (재시작 시 이어서 추적)
CREATE TABLE IF NOT EXISTS {schema}.vehicle_state (
    provider        text        NOT NULL,
    route_id        text        NOT NULL,
    vehicle_id      text        NOT NULL,
    plate_no        text,
    trip_id         text        NOT NULL,
    trip_started_at timestamptz NOT NULL,
    last_seq        integer     NOT NULL,
    last_frac       double precision,
    last_obs_time   timestamptz NOT NULL,
    last_seen_at    timestamptz NOT NULL,
    PRIMARY KEY (provider, route_id, vehicle_id)
);

-- API 호출 로그 (장애/결측 구간 파악용)
CREATE TABLE IF NOT EXISTS {schema}.poll_log (
    id            bigserial   PRIMARY KEY,
    provider      text        NOT NULL,
    route_id      text        NOT NULL,
    polled_at     timestamptz NOT NULL,
    ok            boolean     NOT NULL,
    vehicle_count integer,
    event_count   integer,
    latency_ms    integer,
    error         text
);
CREATE INDEX IF NOT EXISTS poll_log_time_idx ON {schema}.poll_log (polled_at);

-- (선택) 원시 위치 스냅샷: save_raw_positions=true일 때만 적재. 감지 로직 재처리용
CREATE TABLE IF NOT EXISTS {schema}.position_raw (
    provider     text        NOT NULL,
    route_id     text        NOT NULL,
    observed_at  timestamptz NOT NULL,
    vehicle_id   text        NOT NULL,
    seq          integer     NOT NULL,
    section_frac double precision,
    payload      jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS position_raw_idx ON {schema}.position_raw (provider, route_id, observed_at);

-- 분석용 뷰: 정류장별 배차간격 / 정류장 간 소요시간
CREATE OR REPLACE VIEW {schema}.v_arrival_headway AS
SELECT e.*,
       e.arrived_at - lag(e.arrived_at) OVER w                    AS headway,
       e.window_end - e.window_start                              AS uncertainty
FROM {schema}.arrival_event e
WINDOW w AS (PARTITION BY e.provider, e.route_id, e.station_seq, e.service_date ORDER BY e.arrived_at);

CREATE OR REPLACE VIEW {schema}.v_link_travel_time AS
SELECT e.provider, e.route_id, e.route_name, e.trip_id, e.service_date,
       lag(e.station_seq)  OVER w AS from_seq, lag(e.station_name) OVER w AS from_station,
       e.station_seq AS to_seq, e.station_name AS to_station,
       lag(e.arrived_at)   OVER w AS from_time, e.arrived_at AS to_time,
       e.arrived_at - lag(e.arrived_at) OVER w AS travel_time
FROM {schema}.arrival_event e
WINDOW w AS (PARTITION BY e.provider, e.route_id, e.trip_id ORDER BY e.station_seq);
