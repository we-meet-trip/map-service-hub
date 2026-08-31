# map-service-hub

MAP 서비스의 외부 데이터 게이트웨이. FastAPI + PostGIS + APScheduler. 외부 API를 단일 진입점으로 캡슐화하며 L1 Redis 캐시·결정적 룰 엔진을 제공한다. (도로를 따라가는 경로는 hub가 라우팅 엔진에 직접 물어 `/v1/directions/batch` 로 제공한다. 엔진이 없으면 해당 구간을 비워 응답하고 BFF가 직선으로 접는다.)

## 역할

- 외부 API 통합 호출(실 구현): KMA(단기/중기) · Kakao Local · 두루누비 코스 · Naver Blog(리뷰)
- L1 Redis 캐시 + `hub_data` 사전적재 — 코스는 APScheduler 로 `hub_data.places` 에 미리 적재하고, 카카오/네이버 검색 결과는 Redis 에 짧게 캐시한다 (L2 PostgreSQL 캐시 어댑터는 자리표시자)
- APScheduler 기반 KMA 사전 폴링 — 등록된 좌표만 갱신하여 사용자 경로 외부 호출을 사실상 제거
- 결정적 룰 엔진(실 구현) — 모빌리티 반경(도보 3km · 자전거 10km · 킥보드 10km · 자동차/대중교통 무제한) · 강수 PoP 50%+ 실내 우선 · 금지구역 교차
- PostGIS 공간 연산 (반경 내 장소 · 폴리라인 금지구역 교차 등)
- 자체 schema `hub_data` 단독 쓰기

## 폴더 구조

```
map-service-hub/
├── Dockerfile                    Python 3.12-slim + libgeos/libproj
├── requirements.txt              핵심 의존성 (fastapi · sqlalchemy[asyncio] · geoalchemy2 · alembic · apscheduler 등)
└── app/
    ├── __init__.py
    ├── main.py                   FastAPI 진입점 + /health
    ├── routers/hub_routers.py    /v1/places · /v1/places/photos · /v1/weather
    │                             · /v1/weather/now · /v1/reviews
    │                             · /v1/directions/batch · /v1/transit/subway
    │                             · /v1/transit/routes
    │                             · /v1/mobility/bike-stations
    │                             · /v1/mobility/pm-vehicles
    ├── routers/rules_router.py   /v1/rules/* (모빌리티 반경 · 실내 가점 · 금지구역)
    ├── routers/internal_router.py       /internal/kma/run-now
    ├── routers/internal_admin_router.py /internal/grids · /internal/forbidden-zones
    ├── routers/guards.py         public_guard (AUTH_ENFORCED 선택적 인증)
    ├── rules/rule_engine.py      결정적 룰 순수 함수 (반경 · 실내 가점)
    ├── clients/hub_clients.py    외부 API 클라이언트 (KMA·Kakao·두루누비·Naver)
    ├── cache/hub_cache.py        Redis L1 캐시 (L2 어댑터는 자리표시자)
    ├── scheduler/hub_scheduler.py APScheduler KMA polling job
    └── db/hub_db.py              SQLAlchemy async engine + PostGIS 모델
```

## 실행 (단독 빌드 — 통합 실행은 map-service-infra 사용 권장)

```bash
docker build -t map-service-hub:dev .
docker run --rm -p 8001:8000 --env-file ../map-service-infra/.env map-service-hub:dev
curl http://127.0.0.1:8001/health
```

## 의존성

- Python 3.12
- PostgreSQL + PostGIS extension
- Redis
- 외부 API 키 (Kakao Local · KMA · 두루누비 · AirKorea · Naver) — 미설정 시 해당 출처는 스텁으로 동작
- 도로 추종 경로를 쓰려면 라우팅 엔진 두 개(도보·자전거)와 그 주소(`OSRM_FOOT_BASE_URL` · `OSRM_BICYCLE_BASE_URL`)

## 장소 조회 (`GET /v1/places`)

행정구역 기준으로 점 장소(카카오 로컬)와 걷기/자전거 코스(두루누비)를 한 번에 병합 조회한다.

- 쿼리: `province`(필수) · `city` · `keyword` · `category_group_code` · `mobility`(walk/bicycle → 코스 걷기/자전거 필터) · `size`
- 카카오: 행정구역을 좌표로 변환한 뒤 키워드/카테고리 검색. 결과는 Redis L1에 1시간 캐시. 좌표 x(경도)/y(위도)는 hub에서 lat/lng로 교차한다.
- 두루누비: 코스를 주기 동기화(APScheduler)로 `hub_data.places`에 사전 적재하고, 각 코스의 GPX에서 대표 좌표를 계산해 둔다. 조회 시 행정구역 중심 좌표 주변을 PostGIS 반경으로 거른다.
- 응답: `{places, count, sources}`. 출처는 `source`("kakao"|"durunubi")로 구분한다.

### 환경 변수 (장소 출처)

- `KAKAO_REST_API_KEY` — 카카오 REST 키. 비어 있으면 스텁 응답으로 동작(키 발급 전 인터페이스 검증용).
- `TOUR_API_SERVICE_KEY` — 두루누비(data.go.kr) 인증키(디코딩 키 권장 — 이중 인코딩 회피). 비어 있으면 스텁 코스를 적재한다.
- `REDIS_URL` · `REDIS_DB_CACHE` — 장소 L1 캐시 접속(기본 DB 4).
- `PLACES_STUB_MODE=true` — 키가 채워져 있어도 강제로 스텁만 사용.

## 리뷰 조회 (`GET /v1/reviews`)

검색어에 대한 네이버 블로그 리뷰를 조회한다(장소 보강용).

- 쿼리: `query`(필수, 1~60자) · `display`(1~10, 기본 5) · `sort`(`sim` 정확도 | `date` 최신순)
- 응답은 Redis L1에 6시간 캐시. `title`/`description` 은 `<b></b>` 마크업과 HTML 엔티티를 제거한 순수 텍스트다.
- 네이버 호출 실패는 빈 리뷰 목록으로 흡수한다(5xx 를 내지 않는 hub degrade 원칙).
- 응답: `{query, reviews, count}`.
- 스텁 모드: `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET` 중 하나라도 비어 있으면 고정 스텁 리뷰로 동작한다. 시크릿은 요청 헤더로만 전달되며 URL에 실리지 않는다.
- 무료 쿼터(문서화): 네이버 검색 API 25,000회/일.

### 환경 변수 (리뷰 출처)

- `NAVER_CLIENT_ID` · `NAVER_CLIENT_SECRET` — 네이버 개발자센터 애플리케이션 자격증명. 하나라도 비면 스텁.
- `NAVER_BLOG_TIMEOUT_SEC`(기본 3.0) · `NAVER_BLOG_CACHE_TTL_SEC`(기본 21600 = 6h).

## 장소 사진 조회 (`GET /v1/places/photos`)

장소명과 좌표로 그 장소의 사진을 조회한다(장소 상세 화면용).

- 쿼리: `query`(필수, 1~60자) · `lat`(33.0~43.0, 필수) · `lng`(124.0~132.0, 필수). 좌표를 필수로 받는 이유는 같은 상호가 여러 지역에 있어 이름만으로는 다른 동네 지점이 잡히기 때문이다.
- 조회는 세 단계다. 좌표 주변으로 장소 식별자를 찾고, 그 장소의 사진 목록을 받고, 사진마다 이미지 주소를 발급받는다. 앞의 두 단계는 응답 필드를 최소로 지정해 과금이 없고, 마지막 발급만 건당 과금된다.
- 캐시에 담는 것은 **장소 식별자뿐**(30일, 무매칭은 1일)이다. 사진 이름과 이미지 주소는 만료되는 값이라 담지 않고 요청마다 새로 받는다. 사진 바이트를 hub 가 중계하지도 않는다 — 발급 주소를 그대로 응답에 싣는다.
- 유료 단계에는 하루 상한을 건다. 상한을 넘기거나 셀 수 없으면(캐시 미설정·Redis 장애) 발급을 멈추고 빈 목록을 낸다.
- 조회 실패·무매칭도 빈 목록으로 흡수한다(5xx 를 내지 않는 hub degrade 원칙).
- 응답: `{query, photos, count}`. 각 사진은 `photo_uri`·`width_px`·`height_px`·`attributions`·`google_maps_uri`·`flag_content_uri`. 사진을 화면에 쓰는 쪽은 `attributions` 의 제공자 표기를 함께 보여야 한다.
- 스텁 모드: `GOOGLE_MAPS_API_KEY` 가 비어 있으면 고정 스텁 사진으로 동작한다. 키는 요청 헤더로만 전달되며 URL에 실리지 않는다.

### 환경 변수 (사진 출처)

- `GOOGLE_MAPS_API_KEY` — Google Cloud 콘솔 API 키. 비면 스텁.
- `GOOGLE_PLACES_TIMEOUT_SEC`(기본 3.0) · `GOOGLE_PLACES_BIAS_RADIUS_M`(기본 500).
- `GOOGLE_PHOTOS_TOTAL_BUDGET_SEC`(기본 4.0) — 조회 한 건 전체 제한. 세 단계 타임아웃의 합이 BFF 읽기 제한(5초)을 넘기지 않도록 그보다 짧게 둔다. 넘기면 끊고 빈 목록을 낸다.
- `GOOGLE_PLACEID_CACHE_TTL_SEC`(기본 2592000 = 30일) · `GOOGLE_PLACEID_NEG_CACHE_TTL_SEC`(기본 86400 = 1일).
- `GOOGLE_PHOTOS_MAX_COUNT`(기본 3) · `GOOGLE_PHOTOS_MAX_WIDTH_PX`(기본 800).
- `GOOGLE_PHOTOS_DAILY_MEDIA_CAP`(기본 32) — 하루 이미지 주소 발급 상한. 0 이면 사진 조회를 끈다.

## 지하철 경로 조회 (`GET /v1/transit/subway`)

두 좌표 사이를 지하철만으로 가는 경로 중 가장 빠른 하나를 조회한다.

- 쿼리: `start_lat`·`start_lng`·`end_lat`·`end_lng`(모두 필수, 국내 범위).
- 버스가 섞인 경로는 제외한다. 화면이 지하철 전용이라 섞인 경로를 주면 안내와 실제가 어긋난다.
- 응답: `{status, route}`. `status` 는 `ok`·`not_found`·`unavailable` 셋이다. **경로 없음과 조회 불가를 합치지 않는다** — 합치면 외부 장애가 "갈 수 있는 길이 없다"로 표시되어 사용자가 잘못된 결론을 얻는다.
- 캐시 키는 좌표를 반올림해 만든다(`ODSAY_CACHE_COORD_DIGITS`, 기본 4자리 ≈ 10m). 그러지 않으면 같은 건물에서 출발한 요청마다 새 키가 되어 캐시가 사실상 비고 하루 한도가 금방 바닥난다.
- 실패도 짧게 캐시한다(`ODSAY_FAIL_CACHE_TTL_SEC`). 남기지 않으면 외부가 죽어 있는 동안 매 요청이 제한 시간까지 기다린다. 다만 **하루 상한에 걸린 사실은 캐시하지 않는다** — 남기면 자정에 한도가 풀린 뒤에도 남은 TTL 동안 계속 막힌다.
- 조회 실패·제한 시간 초과도 5xx 를 내지 않고 `status:"unavailable"` 로 흡수한다(hub degrade 원칙).
- 스텁 모드: `ODSAY_API_KEY` 가 비어 있으면 좌표에 따라 길이가 달라지는 고정 경로를 낸다.

### 환경 변수 (지하철 경로)

- `ODSAY_API_KEY` — ODsay 인증키. 비면 스텁. 발급처가 앱 등록마다 URI·서버·Android·iOS 로 키를 나눠 주지만 요청에는 어느 쪽인지 실리지 않아 어떤 키를 넣어도 서버에서 호출된다.
- `ODSAY_API_KEY_FALLBACK` — 예비 키. 비면 폴백 없음. 발급처는 등록해 둔 쓰임과 요청이 맞지 않으면 키 인증 자체를 거절하므로, 주 키가 막혔을 때 이 키로 한 번 더 시도한다. **키에 얽힌 실패에만** 다시 부른다 — 좌표가 틀린 것 같은 실패까지 다시 부르면 남은 하루치만 두 배로 쓴다. 예비 키 호출도 하루 상한을 함께 쓴다.
- `ODSAY_REQUEST_TIMEOUT_SEC`(기본 3.0) · `ODSAY_TOTAL_BUDGET_SEC`(기본 4.0) — 뒤의 값은 BFF 읽기 제한(5초)보다 짧아야 degrade 응답이 전달된다.
- `ODSAY_CACHE_TTL_SEC`(기본 21600 = 6시간) · `ODSAY_FAIL_CACHE_TTL_SEC`(기본 60) · `ODSAY_CACHE_COORD_DIGITS`(기본 4).
- `ODSAY_DAILY_CALL_CAP`(기본 900) — 하루 외부 호출 상한. 무료 등급 한도보다 낮게 둔다. 0 이면 조회를 끈다.

## 대중교통 통합 길찾기 (`GET /v1/transit/routes`)

두 좌표 사이를 대중교통으로 가는 방법을 소요시간 순으로 나열한다. 위의 지하철 전용
조회와 달리 **거르지 않는다** — 버스 전용·혼합 경로도 그대로 담는다. "갈 수 있는
방법을 모두 보여주는" 화면은 이쪽을 쓴다.

- 쿼리: `start_lat`·`start_lng`·`end_lat`·`end_lng`(모두 필수, 국내 범위) ·
  `mode`(`all`·`subway`·`bus`, 기본 `all`).
- 응답: `{status, routes}`. `status` 값과 degrade 원칙은 지하철 전용 조회와 같다.
  `routes` 는 소요시간 오름차순, 최대 8건이다. 걸러 낸 뒤 남은 것이 없으면
  `not_found` 다 — 조회는 됐는데 그 수단으로 갈 방법이 없는 상태라 외부 장애
  (`unavailable`)와 구분해야 화면이 다른 문구를 낼 수 있다.

### `mode` — 화면이 고른 이동수단

앱의 "지하철"·"버스" 버튼이 같은 엔드포인트를 서로 다른 `mode` 로 부른다.

- `bus` — **버스 전용만**. 지하철이 섞인 후보를 뺀다. 버튼이 "버스"인데 목록
  맨 위가 지하철 전용이면 버튼과 결과가 어긋난다.
- `subway` — **지하철이 든 후보만**. 그중 타는 거리의 대부분이 버스인 것은 뺀다
  (`TRANSIT_BUS_DOMINANCE_RATIO`, 기본 0.80). 지하철을 두세 정거장 타려고 버스를
  한 시간 타는 경로를 "지하철 경로"로 보여주지 않으려는 것이다.
  다만 **그 규칙이 후보를 전부 지우면 규칙을 적용하지 않는다** — 지하철이 든
  길이 분명히 있는데 "없다"고 내보내는 편이 더 나쁘다. 지하철이 아예 없는
  지역이라 비는 것은 사실 그대로이므로 둔다.
- `all` — 거르지 않는다.

비중은 **거리**로 잰다(시간 아님). 같은 거리라도 버스는 신호와 정차로 시간이
늘어나, 시간으로 재면 "버스를 오래 탔다"와 "버스가 막혔다"가 구분되지 않는다.
분모는 타는 거리(지하철+버스)뿐이고 **도보는 뺀다** — 도보는 어느 경로에나 붙는
공통 비용이라 넣으면 버스 비중이 실제보다 낮게 나온다.

기본값 0.80 은 실제 경로 24개 구간 257건을 재서 정했다. 이 선에 걸리는 건 4건
뿐이고 전부 "지하철 2 km 이하 + 버스 10 km 이상" 형태였다. 0.70 으로 내리면
지하철을 13 km 넘게 타는 정상 경로까지 걸린다.

후보에는 `subway_distance_m`·`bus_distance_m`·`bus_distance_ratio` 가 함께
실려 나가므로, 화면이 배지나 정렬에 그대로 쓸 수 있다. 구간에도 `distance_m` 이 있다.
- 후보 하나는 `{total_time_min, fare, transfer_count, total_walk_m, modes, legs}` 다.
  `modes` 는 그 경로에 실제로 등장한 이동수단만 지하철·버스 순으로 담아, 목록 화면이
  후보를 열지 않고도 아이콘을 고를 수 있게 한다.
- 구간(`legs`)에는 지도에 그릴 `geometry`([lat,lng] 좌표열)와 지나는 역·정류장
  이름 `stops` 가 함께 온다. **둘의 결측 처리가 다르다** — `geometry` 는 좌표
  목록이 비면 시작·끝 두 점으로 대체하지만, `stops` 는 대체하지 않고 빈 리스트로
  둔다. "N개 정류장" 표시를 시작·끝만으로 채우면 실제로 몇 곳을 지나는지 오인시킨다.
- 좌표는 `loadLane` 이 아니라 경로 응답에 함께 오는 `passStopList` 로 만든다.
  `loadLane` 은 실호출에서 `-8 mapObject 형식이 잘못되었습니다` 로 계속 실패한다
  (공식 문서와 어긋남). 겸사겸사 외부 호출도 한 번 줄었다.
- 캐시·하루 상한·예비 키 재시도는 지하철 전용 조회의 것을 그대로 쓴다. 캐시 키
  네임스페이스만 `odsay:routes:` 로 나눈다 — 합치면 "지하철만" 결과가 "전체 보기"
  응답으로 새어 나간다.
- 스텁 모드: `ODSAY_API_KEY` 가 비어 있으면 지하철 전용·버스 전용 후보를 하나씩 낸다.
  목록의 아이콘 분기와 지도 렌더를 실호출 없이 확인할 수 있다.

환경 변수는 위 "환경 변수 (지하철 경로)"를 그대로 쓴다. 추가 항목은 없다.

## 따릉이 대여소 조회 (`GET /v1/mobility/bike-stations`)

좌표 주변의 서울 공공자전거 대여소 현황을 조회한다.

- 쿼리: `lat`·`lng`(필수, 국내 범위) · `radius_m`(100~20000, 기본 `SEOUL_BIKE_DEFAULT_RADIUS_M`).
- 발급처가 전량을 통째로 주므로 **전량 스냅샷 한 벌만 캐시**하고 반경 필터는 캐시 뒤에서 한다. 좌표별로 캐시하면 지도를 움직일 때마다 여러 장을 다시 받아 와 하루 한도가 곧 바닥난다.
- 한 번에 주는 행 수가 정해져 있어 나눠 받는다. **마지막 장 판정은 버리기 전 행 수로 한다** — 좌표 없는 행을 걸러낸 뒤의 길이로 보면 한 장이 꽉 차서 왔는데도 덜 왔다고 읽혀 뒤쪽 대여소를 통째로 놓친다.
- 도중에 실패하면 받은 만큼으로 답하되 그 스냅샷은 짧게만 담는다(`SEOUL_BIKE_PARTIAL_CACHE_TTL_SEC`).
- 서비스 지역이 서울이라 그 밖 좌표는 빈 목록이 온다. 실패가 아니므로 `status` 는 `ok` 다.
- 응답: `{status, stations, count}`. `status` 는 `ok`·`unavailable`.
- 스텁 모드: `SEOUL_OPENAPI_KEY` 가 비어 있으면 **요청 좌표 주변에** 대여소 몇 곳을 만들어 낸다. 고정 좌표를 쓰면 지도가 다른 곳을 비출 때 화면이 비어 보여 확인하려던 것을 확인하지 못한다.

### 환경 변수 (따릉이)

- `SEOUL_OPENAPI_KEY` — 서울 열린데이터광장 인증키. 비면 스텁. 발급처가 https 를 받지 않아 **hub 에서 나가는 이 호출만 평문**이다. 그래서 앱이 직접 부르지 않고 hub 를 거치게 한다.
- `SEOUL_BIKE_REQUEST_TIMEOUT_SEC`(기본 3.0) · `SEOUL_BIKE_TOTAL_BUDGET_SEC`(기본 4.0).
- `SEOUL_BIKE_CACHE_TTL_SEC`(기본 300) — 짧아질수록 하루 외부 호출 수가 그대로 비례해 늘어난다. · `SEOUL_BIKE_PARTIAL_CACHE_TTL_SEC`(기본 30).
- `SEOUL_BIKE_PAGE_SIZE`(기본 1000) · `SEOUL_BIKE_MAX_PAGES`(기본 5) — 상한이 없으면 응답의 개수 필드가 이상할 때 요청 하나가 끝나지 않는다.
- `SEOUL_BIKE_DEFAULT_RADIUS_M`(기본 5000).

## 공유 킥보드 조회 (`GET /v1/mobility/pm-vehicles`)

좌표 주변의 공유 킥보드(개인형 이동장치) 위치를 조회한다.

- 쿼리: `lat`·`lng`(필수, 국내 범위) · `radius_m`(100~20000, 기본 1000) · `city`(선택, 시군구명).
- 발급처가 좌표로 받지 않고 **사업자명을 필수로** 받으며 사업자 목록을 주는 오퍼레이션이 없다. 그래서 `PM_PROVIDERS` 에 적어 둔 사업자를 하나씩 물어 합친다 — 한 번 조회에 사업자 수만큼 호출이 나가므로 결과를 지역 단위로 캐시한다.
- **일부 사업자만 실패하면 받은 만큼으로 `ok` 를 낸다.** 한 사업자의 장애로 나머지가 함께 사라지면 화면이 실제보다 비어 보인다. 사업자 전부에서 실패해야 `unavailable` 이다.
- 인증 단계에서 막힌 응답은 본문 껍데기부터 다르다(`OpenAPI_ServiceResponse`). 이것을 정상 경로로 읽으면 **조회가 된 줄 알고 화면이 조용히 빈 채로 남아** 키가 막힌 사실을 아무도 모르게 되므로, 클라이언트가 따로 판별해 실패로 올린다.
- 응답: `{status, vehicles, count}`. `status` 는 `ok`·`unavailable`.
- 스텁 모드: 키가 없으면 **요청 좌표 주변에** 기기 몇 대를 만들어 낸다. 배터리 잔량을 넓게 흩어 두어 잔량별 표시를 키 없이도 확인할 수 있다.

> **실측 기록(2026-08-11).** 인증은 통과하나(가짜 키는 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` 로 거부됨) **사업자 8종 × 지역 5종 = 40개 조합 전부 `totalCount 0`** 이었다. 배선은 정상이고 발급처에 데이터가 없는 상태다. 데이터가 들어오면 코드 변경 없이 그대로 나온다.

### 환경 변수 (공유 킥보드)

- `PM_SERVICE_KEY` — data.go.kr 인증키. 비면 `KMA_SERVICE_KEY` 를 대신 쓰고, 둘 다 비면 스텁.
- `PM_PROVIDERS` — 조회할 사업자 목록(콤마 구분).
- `PM_REQUEST_TIMEOUT_SEC`(기본 5.0) · `PM_TOTAL_BUDGET_SEC`(기본 4.0).
- `PM_CACHE_TTL_SEC`(기본 120) · `PM_FAIL_CACHE_TTL_SEC`(기본 30) · `PM_NUMOFROWS`(기본 100) · `PM_DEFAULT_RADIUS_M`(기본 1000).

## 룰 엔진 (`POST /v1/rules/*`)

외부 호출 없이 결정적으로 계산 가능한 규칙 3종. 좌표/점수 계산은 순수 함수(`app/rules/rule_engine.py`), 금지구역 교차는 PostGIS(`hub_data.forbidden_zones`)가 담당한다.

- `POST /v1/rules/filter/mobility-radius` — 출발지 기준 이동수단 반경 필터.
  - 본문: `{origin:{lat,lng}, mobility, candidates:[{lat,lng,...}]}` (candidates ≤ 100)
  - 반경: 도보/walk 3km · 자전거 10km · 킥보드 10km · 자동차/대중교통 무제한(전부 통과). 알 수 없는 `mobility` 는 422.
  - 응답: `{filtered, radius_m, dropped}` (radius_m 무제한이면 null)
- `POST /v1/rules/score/indoor-bonus` — 강수 시 실내 우선 가점.
  - 본문: `{pois:[{content_id, indoor_flag, base_score(0~10)}], day_pop_max(0~100)}` (pois ≤ 100)
  - `day_pop_max ≥ 50` 이고 `indoor_flag` 면 점수에 +0.15.
  - 응답: `{scored:[{content_id, score}]}`
- `POST /v1/rules/filter/forbidden-zones` — 폴리라인 금지구역 교차 판정.
  - 본문: `{polyline:[{lat,lng}]}` (2~500점)
  - `hub_data.forbidden_zones` 와 `ST_Intersects`. 테이블이 비어 있으면 교차 없음(필터 사실상 비활성). DB 장애 시 503.
  - 응답: `{intersects, zones, suggested_detour}` (PoC 에서 detour 는 항상 null)

## 공개 endpoint 인증 (`AUTH_ENFORCED`)

- 기본 `AUTH_ENFORCED=false` — 공개 endpoint(`/v1/*`)는 무인증(현행 데모 동작).
- `AUTH_ENFORCED=true` — 모든 `/v1/*` 요청에 헤더 `X-Internal-Token`(= `INTERNAL_SERVICE_TOKEN`)을 요구한다. 불일치/누락 시 401. `INTERNAL_SERVICE_TOKEN` 이 비어 있으면 부팅을 중단한다(fail-fast).
- `/health` 는 `AUTH_ENFORCED` 와 무관하게 항상 무인증. `/internal/*` 는 별도로 CIDR 화이트리스트 + `X-Internal-Token` 으로 보호된다.

## License

MIT — see [LICENSE](LICENSE).
