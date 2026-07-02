# map-service-hub

MAP 서비스의 외부 데이터 게이트웨이. FastAPI + PostGIS + APScheduler. 외부 API를 단일 진입점으로 캡슐화하며 3-tier 캐시·결정적 룰 엔진·OSRM 프록시를 제공한다.

## 역할

- 외부 API 통합 호출: TourAPI 4.0 · KMA(단기/중기) · Kakao Local · Kakao Mobility · Naver Blog
- OSRM(foot/bicycle) 프록시 — docker network 내부 osrm-foot:5000 / osrm-bicycle:5001 호출
- 3-tier cascade 캐시: L1 Redis → L2 PostgreSQL(`hub_data`) → L3 외부 API
- APScheduler 기반 KMA 사전 폴링 — 등록된 좌표만 갱신하여 사용자 경로 외부 호출을 사실상 제거
- 결정적 룰 엔진 — 모빌리티 반경(도보 3km · 자전거 10km · 킥보드 7km · 자동차 무제한) · 강수 PoP 50%+ 실내 우선 · 금지구역 교차
- PostGIS 공간 연산 (반경 내 장소 · 버퍼 내 장애물 등)
- 자체 schema `hub_data` 단독 쓰기

## 폴더 구조

```
map-service-hub/
├── Dockerfile                    Python 3.12-slim + libgeos/libproj
├── requirements.txt              핵심 의존성 (fastapi · sqlalchemy[asyncio] · geoalchemy2 · alembic · apscheduler 등)
└── app/
    ├── __init__.py
    ├── main.py                   FastAPI 진입점 + /health
    ├── routers/hub_routers.py    /v1/places · /v1/weather · /v1/route · /v1/rules
    ├── clients/hub_clients.py    외부 API 클라이언트 6종
    ├── cache/hub_cache.py        Redis L1 + PostgreSQL L2 cascade
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
- osrm-foot · osrm-bicycle 컨테이너
- 외부 API 키 (Kakao Local/Mobility · KMA · TourAPI · Naver)

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

## License

MIT — see [LICENSE](LICENSE).
