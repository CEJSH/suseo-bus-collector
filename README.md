# suseo-bus-collector

`card.suseo_target_sttn`에서 `sttn_type='B'`인 정류장(수서역 일대)을 지나는 **모든 노선**의 실제 도착시각을 실시간 버스 위치 API로 추정해 `tmp.arrival_event`에 적재한다. 원본 테이블은 읽기만 한다.

- 대상 노선 탐색: 정류장 ARS로 서울 `getRouteByStation`, 경기 `getBusStationListv2`→`getBusStationViaRouteListv2`를 조회한다. ARS가 없는 정류장은 서울 좌표 검색(60m)으로 가장 가까운 ARS를 쓴다.
- ARS는 지역 간 중복된다(예: 경기 `23403`은 남양주 정류장). 경기 정류장과 이름으로만 찾은 노선은 **대상 정류장 좌표 150m 안**일 때만 인정한다.
- 카드 노선–정류장 표(`card.routesttn`, 2025-03-19)와 대조해 빠진 노선이 없음을 확인했다. 카드에만 있는 16번은 현재 같은 번호 노선이 모두 수서역에서 2.9km 이상 떨어져 있다.
- 서울 시스템이 수서역5번출구(`23409`)에 연결해 둔 남양주·광주 노선 7개(100, 105, 2000, 2000-1, 202, 23, 9)는 GBIS 정류장 목록상 수서역에서 2.9km 이상 떨어져 있어 제외된다(2026-10-02 확인).
- 대상 정류장 좌표 15m 안에 붙은 다른 번호의 정류장도 조회한다. 송파02는 `23410`에서 8m 떨어진 `23547`에만, 송파03은 `23548`과 `23871`에 등록되어 있다. 이 노선들의 도착은 그 번호로 기록되므로 대상 정류장 분석 시 함께 포함한다.
- GBIS 노선 정류장 목록에서 서울 정류장 번호가 비어 있으면 대상 정류장 50m 안의 ARS로 채운다.
- 카드 데이터의 `91111`·`91112`·`91117`·`91119`는 GBIS상 `23406`·`23407`·`23401`·`23410`과 같은 위치(0m)의 같은 정류장이다. 분석 시 이 ARS로 바꿔 조회한다.
- 경기 API의 `mobileNo`는 앞에 공백이 붙어 오므로 저장 전에 공백을 제거한다.
- 서울 T-DATA 이력은 날짜가 달라도 시각이 같고 첫차·막차 위주라 계획 데이터로 판단해 제거했다.

## 실행

```bash
python -m busarrival discover --dry-run   # 대상 노선과 예상 호출량 확인 (저장 안 함)
python -m busarrival discover --replace   # 저장 + 이번에 안 나온 기존 노선 비활성화 (조회 오류가 있으면 거부)
python -m busarrival run --hours 65       # 시작 시 탐색(병합 저장) 후 수집. 매일 03시 재탐색
```

- 기관별 주기는 `poll_interval_sec`(기본 30초) 이상이면서 24시간 호출이 `daily_quota`의 90% 안에 들도록 자동으로 늘어난다.
- 기관별 하루 호출 수가 `daily_quota`에 닿으면 자정까지 해당 기관 조회를 멈춘다(`poll_log.error='local daily budget reached'`).
- 서울 정류소 API는 연속 호출 시 요청제한 오류를 주므로 탐색 조회는 1초 간격, 실패 시 재시도한다.
- **PC가 절전에 들어가면 수집도 멈춘다.** 수집 기간에는 절전을 끈다.

## 동작 방식

```
[시작 시 + 매일 03시] 탐색
  card.suseo_target_sttn(B) 정류장 → 경유 노선 후보 → 관할 기관 노선 ID
  → 관할 기관 API로 노선 전체 정류장 목록 (+ 좌표 누적거리)          → route, route_stop

[30초마다] 수집 (collector)
  노선마다 관할 기관의 '노선별 버스 위치' API 1회 호출
    경기  buslocationservice/v2/getBusLocationListv2 (stationSeq, stateCd)
    인천  busLocationService/getBusRouteLocation     (LATEST_STOPSEQ)
  → RouteTracker: 차량별 직전 순번과 비교, 순번이 올라간 만큼 정류장 도착 이벤트 생성 → arrival_event
```

### 왜 '도착예정' API가 아니라 '버스 위치' API인가
- 정류장별 도착예정 API로 모든 정류장을 조회하면 `노선 수 × 정류장 수` 호출(예: 30×80=2,400회/30초, 하루 약 690만 회)이 필요해 공공데이터포털 트래픽 한도를 크게 넘는다. 위치 API는 **노선당 1회**면 전 정류장이 커버된다.
- 도착예정 API는 예측값이라 '도착했다'는 사실을 간접 추정해야 하지만, 위치 API는 차량이 어느 정류장을 지났는지를 직접 알려준다.

### 도착 시각 산정
- 두 관측 (t0, seq0) → (t1, seq1) 사이에 지난 정류장들의 도착 시각은 **정류장 좌표 누적거리로 선형 보간**한다.
- 이번 관측에서 차량이 그 정류장에 정차 중이면(서울 stopFlag=1, 경기 stateCd=1) 관측 시각을 그대로 쓰고 `method='stop_observed'`로 표시한다.
- 이벤트는 추정에 사용한 관측 구간 `(window_start, window_end]`를 저장한다. API 지연과 순번 오차 때문에 실제 도착이 그 안에 있다는 보장은 없다. 요청량과 제한에 따라 실제 폴링 주기는 30초보다 길 수 있다.
- GPS 튐(순번 소폭 역행)은 무시하고, 크게 역행하거나 30분 넘게 안 보이면 새 운행(trip)으로 본다. API 장애 등으로 관측 간격이 10분을 넘으면 정밀도를 보장할 수 없으므로 이벤트를 만들지 않는다.
- `UNIQUE(provider, route_id, trip_id, station_seq)`로 중복 적재를 막고, 차량 상태(`vehicle_state`)를 DB에 저장해 재시작해도 이어서 추적한다.

## 설치 / 실행

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
export DATABASE_URL=postgresql://user:pw@host:5432/db
export SEOUL_API_KEY=...      # data.go.kr 서울특별시 노선정보/정류소정보/버스위치정보 조회 서비스
export GYEONGGI_API_KEY=...   # 경기도 정류소/노선/버스위치 조회 서비스(v2)
export INCHEON_API_KEY=...    # 인천광역시 버스노선/버스위치 조회 서비스

python -m busarrival init-db
python -m busarrival discover --dry-run       # 대상 노선 확인 + 일일 호출량 추정
python -m busarrival probe seoul 100100xxx    # 원시 응답 확인 (필드명 검증)
python -m busarrival run
```

운영은 `deploy/busarrival.service`(systemd)를 참고한다. SIGTERM을 받으면 현재 사이클을 마치고 종료한다.

## 트래픽(쿼터)
노선 1개를 30초 주기로 수집하면 하루 2,880회를 호출한다. 2026-10-02 기준 대상은 서울 20개·경기 17개로, 서울 약 5.8만 회, 경기 약 4.9만 회/일이다(한도 각 10만 회).
심야처럼 차량이 없는 노선은 자동으로 5분 주기로 줄어든다. 시작 로그와 `discover --dry-run`이 추정 호출량을 보여준다.

## 주요 테이블 (`tmp` 스키마)
| 테이블 | 내용 |
|---|---|
| `arrival_event` | 노선·차량·운행·정류장별 추정 도착 시각 + 불확실 구간 (핵심) |
| `route`, `route_stop` | 수집 대상 노선과 전체 정류장 (ars_id = 기관 간 공통 정류장 번호) |
| `vehicle_state` | 차량 추적 상태 (재시작 복구용) |
| `poll_log` | API 호출 성공/실패·지연 (결측 구간 판별) |
| `position_raw` | (선택) 원시 위치 스냅샷 — 감지 로직 재처리용 |
| `v_arrival_headway`, `v_link_travel_time` | 배차간격, 정류장 간 소요시간 뷰 |

```sql
-- 수서역 정류장(ars_id)의 오늘 노선별 실제 도착 시각
SELECT route_name, arrived_at, window_end - window_start AS uncertainty, method
FROM tmp.arrival_event
WHERE ars_id = '23xxx' AND service_date = current_date
ORDER BY arrived_at;

-- 운행일별 대상 정류장 커버리지: 정류장(ARS)·노선별 도착 건수
SELECT e.service_date, t.sttn_ars_num, e.route_name, count(*) AS arrivals
FROM (SELECT DISTINCT sttn_ars_num FROM card.suseo_target_sttn WHERE sttn_type = 'B') t
JOIN tmp.arrival_event e ON e.ars_id = t.sttn_ars_num
JOIN tmp.route r ON r.provider = e.provider AND r.route_id = e.route_id AND r.active
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- 결측 구간: 노선별 호출 실패/예산 초과
SELECT provider, route_id, date_trunc('hour', polled_at) AS hour, count(*) FILTER (WHERE NOT ok) AS failed, count(*)
FROM tmp.poll_log WHERE polled_at > now() - interval '1 day'
GROUP BY 1, 2, 3 HAVING count(*) FILTER (WHERE NOT ok) > 0 ORDER BY 3, 1, 2;
```

경기 노선의 정류장 `ars_id`는 GBIS `mobileNo`이며, 서울 정류장의 경우 서울 ARS와 같다.

## 주의
- API 필드명은 공공데이터포털 명세 기준으로 작성했고, 대소문자·후보 키를 관대하게 읽도록 했다. 인증키를 받은 뒤 `probe`로 실제 응답을 한 번 확인할 것.
  - 서울 `sectOrd`가 '마지막 통과 정류장 순번'과 1만큼 어긋나면 `providers/seoul.py`에서 보정한다.
- 인천 노선은 서울·경기 API의 경유노선 목록에 나온 이름으로 인천 API를 검색하고, 그중 실제로 대상 정류장(ARS·좌표)을 지나는 노선만 채택한다. 2026-10-02 기준 대상 인천 노선은 없다.
- 수집량이 많아지면 `arrival_event`를 `service_date` 기준 월별 파티션으로 나누는 것을 고려한다.

## 테스트
```bash
# pip이 없는 환경(sudo 불가)이면 uv로 가상환경 구성
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --seed .venv && uv pip install --python .venv/bin/python -r requirements.txt pytest respx

.venv/bin/python -m pytest tests                                   # 감지 로직 + API 파싱(모킹)
TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest tests  # + PostgreSQL 통합 (bus_rt_test 스키마 사용)
```
