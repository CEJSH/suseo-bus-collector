# suseo-bus-collector

`public.ent_bus_stop_capital`의 모든 정류장을 지나는 노선이 목표다. 원본 테이블은 읽기 전용이며 신규 적재는 `tmp`에만 한다.

- 서울: T-DATA **배차 정류장별 이력**을 날짜별 적재한다. 월 1회 갱신이며 별도 T-DATA 키가 필요하다.
- 경기·인천: 기존 정류장/노선 매핑을 바탕으로 위치 API를 **시작 후 24시간** 수집하여 도착시각을 추정한다. 00~24시 달력 날짜와는 다르다.
- 실시간 추정과 서울 원본 이력은 다른 테이블에 저장한다. 기존 서울 추정 기록은 삭제하지 않는다.

## 새 수집 명령

```bash
# 기존 SEOUL_API_KEY 사용. SEOUL_HISTORY_API_KEY가 있으면 우선 사용.
# 제공 가능한 날짜를 지정
python -m busarrival import-seoul-history --date 20211219
# 위 날짜는 문서 예시일 뿐이며 실제 제공 여부는 확인 필요

# 경기·인천 매핑 및 노선 준비 (원본 정류장 조회 API로 대상 탐색하지 않음)
python -m busarrival map-stops --provider gyeonggi --provider incheon
python -m busarrival map-routes --provider gyeonggi --provider incheon
python -m busarrival discover --provider gyeonggi --provider incheon
python -m busarrival run --hours 24
```

`run`은 서울 위치 API를 호출하지 않는다. 기본 경기·인천 양쪽 키가 필요하고, 요청한 기관의 노선이 하나도 없으면 실패한다. 한 기관만 실행하려면 `--provider gyeonggi`처럼 명시한다. 종료 시 진행 중인 사이클과 DB 저장을 마치므로 24시간보다 약간 늦게 종료될 수 있다. 재실행은 새로운 24시간이다. systemd는 자동 재시작하지 않는다.

서울 신규 테이블: `tmp.seoul_stop_history`(원본·도착/출발 일시), `tmp.seoul_history_route_import`(날짜·노선별 페이지 체크포인트). 페이지 저장과 체크포인트는 원자적이며 재실행 시 이어받는다. 첫 페이지가 비어 있으면 완료로 표시하지 않아 미공개 자료를 나중에 다시 조회할 수 있다. 동일 원문은 중복 저장하지 않고, 원문이 바뀌면 별도 버전으로 보존한다. 분석 시 같은 운행의 수정 레코드를 구분해야 한다.

실제 API는 문서와 달리 `routeId`를 필수로 요구한다(2026-10-01 확인). 기본은 `tmp.route`에 저장된 서울 활성 노선별 조회이며, `--route-id`를 반복 지정해 대상을 제한할 수 있다. 캐시에 없는 과거 폐지 노선은 포함되지 않으므로 전수 이력을 보장하지 않는다. 알 수 없는 응답 구조는 중단한다. 서울 역사적 `sttnId`는 ARS가 아니다. 원본 이력을 먼저 보존하며, `ent_bus_stop_capital` 연결은 검증된 역사적 정류장 ID 매핑이 추가로 필요하다. 기존 노선/정류장 매핑의 미완료·기관 장애로 경기·인천 전수 커버리지도 아직 보장되지 않는다.

## 동작 방식

```
[매일 03시] 탐색 (discovery)
  tmp.bus_stop_route_map 캐시 → 수도권 전체 대상 노선
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
python -m busarrival map-stops --limit 100  # 검증 배치: public 읽기, tmp 매핑 적재
python -m busarrival map-stops              # 미처리/오류 행 재개
python -m busarrival map-routes             # 기관 내부 ID로 경유 노선 캐시 구축
python -m busarrival discover --dry-run       # 대상 노선 확인 + 일일 호출량 추정
python -m busarrival probe seoul 100100xxx    # 원시 응답 확인 (필드명 검증)
python -m busarrival discover
python -m busarrival run
```

운영은 `deploy/busarrival.service`(systemd)를 참고한다. SIGTERM을 받으면 현재 사이클을 마치고 종료한다.

## 트래픽(쿼터)
노선 1개를 30초 주기로 수집하면 하루 2,880회를 호출한다. 수서역 경유 노선이 서울 20개·경기 10개라면 서울 약 5.8만 회, 경기 약 2.9만 회/일이다.
개발계정 기본 한도(보통 1,000회/일)로는 부족하므로 **운영계정 트래픽 증설을 신청**하거나 `poll_interval_sec: 60`을 쓴다.
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
```

## 주의
- API 필드명은 공공데이터포털 명세 기준으로 작성했고, 대소문자·후보 키를 관대하게 읽도록 했다. 인증키를 받은 뒤 `probe`로 실제 응답을 한 번 확인할 것.
  - 서울 `sectOrd`가 '마지막 통과 정류장 순번'과 1만큼 어긋나면 `providers/seoul.py`에서 보정한다.
- 인천 API는 서울 소재 정류장 주변 검색을 지원하지 않는다. 그래서 인천 노선은 서울·경기 API의 경유노선 목록에 나온 이름으로 인천 API를 검색하고, 그중 실제로 수서역 반경을 지나는 노선만 채택한다.
- 수집량이 많아지면 `arrival_event`를 `service_date` 기준 월별 파티션으로 나누는 것을 고려한다.

## 테스트
```bash
# pip이 없는 환경(sudo 불가)이면 uv로 가상환경 구성
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --seed .venv && uv pip install --python .venv/bin/python -r requirements.txt pytest respx

.venv/bin/python -m pytest tests                                   # 감지 로직 + API 파싱(모킹)
TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest tests  # + PostgreSQL 통합 (bus_rt_test 스키마 사용)
```
