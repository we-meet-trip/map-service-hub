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
    │                             · /v1/directions/batch
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
