"""hub-service 환경설정.

환경변수 또는 프로젝트 루트의 .env 파일로부터 값을 읽어 모듈 최하단의
싱글톤 `settings` 에 바인딩한다. 의존성을 가진 모듈들은 이 객체를 직접
임포트해서 사용한다.

호출처:
  - app.db.hub_db        — HUB_DATABASE_URL
  - app.scheduler.*      — KMA_* 폴링/타임아웃/재시도 파라미터
  - app.clients.hub_clients — KMA_REQUEST_TIMEOUT_SEC / NUMOFROWS / RATE_LIMIT
  - app.routers.internal_router — INTERNAL_SERVICE_TOKEN / HUB_INTERNAL_TRUSTED_CIDRS
"""
from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수 기반 설정 컨테이너.

    model_config: .env 파일을 utf-8 로 읽고, 정의되지 않은 키는 무시한다
        (extra="ignore"). 다른 서비스(user/agent)와 .env 를 공유할 때
        무관한 키를 만나도 검증 에러가 나지 않도록 하기 위함이다.

    [필수 - 환경변수에서 반드시 주입되어야 하는 값]
    HUB_DATABASE_URL: SQLAlchemy 비동기 DSN. asyncpg 또는 psycopg_async
        드라이버 명세를 포함한 문자열을 그대로 받아 create_async_engine 에
        전달된다. Alembic env.py 는 같은 값을 동기 드라이버 명세로 치환한다.
    KMA_SERVICE_KEY: 기상청 단기/중기 예보 API 호출에 쓰는 서비스 키.

    [선택 - 기본값 있음]
    INTERNAL_SERVICE_TOKEN: /internal/* endpoint 호출 시 헤더
        X-Internal-Token 과 일치 여부를 검사하는 공유 비밀. 기본 빈 문자열
        은 미설정 상태를 의미하므로 운영 환경에서는 반드시 별도 값을 주입.

    [KMA 폴링 튜닝 파라미터]
    KMA_POLL_INTERVAL_SEC: 격자/지역 코드 한 건씩 폴링 사이의 대기 시간(초).
        API rate limit 회피용 1초대 미세 대기.
    KMA_RETRY_INTERVAL_SEC: 한 번의 라운드(모든 격자 처리)가 끝난 후
        실패분 재시도까지 대기하는 시간(초).
    KMA_RETRY_MAX_DURATION_SEC: 단일 폴링 루프(단기/중기 각각)의 전체
        제한 시간(초). 이 시간을 넘기면 미완료 grid 가 남아 있어도 종료.
    KMA_REQUEST_TIMEOUT_SEC: httpx.AsyncClient 의 단일 요청 타임아웃(초).
    KMA_NUMOFROWS: 단기예보 페이지당 row 수. 한 좌표의 단기예보는
        다수의 시간/카테고리 row 로 구성되어 페이지 크기를 크게 잡는다.
    KMA_RATE_LIMIT_SLEEP_SEC: HTTP 429(Too Many Requests) 수신 시
        재시도 전에 추가로 대기하는 시간(초).

    [내부 endpoint 보호]
    HUB_INTERNAL_TRUSTED_CIDRS: /internal/* 호출을 허용할 CIDR 목록을
        콤마로 구분한 문자열. 기본은 사설 IP 대역
        (172.16/12, 10/8, 192.168/16). internal_router 에서 파싱되어
        ipaddress.ip_network 객체 리스트로 변환된다.
    AUTH_ENFORCED: true 면 /v1/* 공개 endpoint 도 X-Internal-Token 을
        요구한다. 기본 false = 현행 데모 동작(공개 endpoint 무인증).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    HUB_DATABASE_URL: str
    # 시크릿은 SecretStr 로 감싸 로그/repr 노출을 방지한다(사용 시 .get_secret_value()).
    KMA_SERVICE_KEY: SecretStr
    INTERNAL_SERVICE_TOKEN: SecretStr = SecretStr("")

    KMA_POLL_INTERVAL_SEC: float = 1.5
    KMA_RETRY_INTERVAL_SEC: int = 30
    KMA_RETRY_MAX_DURATION_SEC: int = 1200
    KMA_REQUEST_TIMEOUT_SEC: float = 10.0
    KMA_NUMOFROWS: int = 1200
    KMA_RATE_LIMIT_SLEEP_SEC: float = 2.0

    # [장소 소스 - 카카오 로컬]
    # KAKAO_REST_API_KEY 가 빈 문자열이면 실제 호출 대신 고정 스텁 응답을
    # 사용한다(키 발급 전 인터페이스 검증용). 좌표 검색 반경/페이지 크기는
    # 카카오가 허용하는 상한(radius 20000m, size 15) 안에서 기본값을 둔다.
    KAKAO_REST_API_KEY: SecretStr = SecretStr("")
    KAKAO_REQUEST_TIMEOUT_SEC: float = 5.0
    KAKAO_CACHE_TTL_SEC: int = 3600
    KAKAO_DEFAULT_RADIUS_M: int = 5000
    KAKAO_DEFAULT_SIZE: int = 15

    # [장소 소스 - 두루누비 코스]
    # TOUR_API_SERVICE_KEY 가 빈 문자열이면 스텁 응답을 사용한다.
    # 코스 데이터는 주기 동기화로 미리 적재하며, 좌표가 응답에 없어
    # 각 코스의 GPX 파일을 내려받아 대표 좌표를 계산한다.
    TOUR_API_SERVICE_KEY: SecretStr = SecretStr("")
    DURUNUBI_REQUEST_TIMEOUT_SEC: float = 10.0
    DURUNUBI_NUMOFROWS: int = 100
    DURUNUBI_SYNC_INTERVAL_HOURS: int = 168
    DURUNUBI_GPX_TIMEOUT_SEC: float = 10.0
    # 행정구역 중심 좌표에서 코스를 골라낼 때의 반경(m).
    DURUNUBI_RADIUS_M: int = 20000

    # [장소 보강 - 네이버 블로그 리뷰]
    # NAVER_CLIENT_ID/SECRET 이 비어 있으면 실제 호출 대신 고정 스텁 응답을
    # 사용한다(키 발급 전 인터페이스 검증용). 두 시크릿은 요청 헤더
    # (X-Naver-Client-Id / X-Naver-Client-Secret)로만 전달되며 URL 쿼리에는
    # 실리지 않는다.
    NAVER_CLIENT_ID: SecretStr = SecretStr("")
    NAVER_CLIENT_SECRET: SecretStr = SecretStr("")
    NAVER_BLOG_TIMEOUT_SEC: float = 3.0
    # 블로그 리뷰 결과 L1 캐시 TTL. SoT §7.1 L1 6h.
    NAVER_BLOG_CACHE_TTL_SEC: int = 21600
    # /v1/reviews display 파라미터 미지정 시 기본 표시 건수.
    NAVER_BLOG_DEFAULT_DISPLAY: int = 5

    # 키가 채워져 있어도 강제로 스텁 응답만 쓰고 싶을 때 True 로 둔다.
    PLACES_STUB_MODE: bool = False

    # [현재 날씨 - 초단기실황 + 대기오염]
    # AIRKOREA_SERVICE_KEY 는 data.go.kr 대기오염정보 서비스 키. 비어 있으면
    # KMA_SERVICE_KEY 를 그대로 쓴다 — 두 서비스가 한 계정 키로 열려 있는
    # 경우가 흔해서, 키를 두 번 적지 않아도 되게 한다. 둘 다 비면 스텁.
    AIRKOREA_SERVICE_KEY: SecretStr = SecretStr("")
    AIRKOREA_REQUEST_TIMEOUT_SEC: float = 5.0
    # 실황은 매시 발표라 짧게, 대기오염은 매시 갱신이라 조금 더 길게 잡는다.
    WEATHER_NOW_CACHE_TTL_SEC: int = 300
    AIRKOREA_CACHE_TTL_SEC: int = 900
    # 실황 스냅샷 보관 일수. 어제 비교에 하루면 충분하지만, 조회가 없던
    # 날이 끼어도 비교가 끊기지 않도록 여유를 둔다. 하우스키핑이 이 기간을
    # 넘긴 row 를 지운다(안 지우면 격자 수 × 시각만큼 계속 쌓인다).
    WEATHER_SNAPSHOT_RETENTION_DAYS: int = 3

    # [경로 라우팅 - 자체 호스팅 OSRM]
    # 도보(foot)/자전거(bicycle) 프로파일 OSRM 인스턴스의 base URL.
    # 빈 문자열이면 실제 호출 대신 결정적 스텁 지오메트리를 사용한다(데이터
    # 빌드 전 인터페이스 검증용). mode→인스턴스 선택: walk→FOOT,
    # bicycle/scooter→BICYCLE. transit(버스)은 라우팅 대상이 아니다.
    OSRM_FOOT_BASE_URL: str = ""
    OSRM_BICYCLE_BASE_URL: str = ""
    OSRM_TIMEOUT_SEC: float = 3.0
    # 경로 결과 L1 캐시 TTL(초). 자체 데이터라 외부 ToS 제약이 없어 길게 둔다.
    ROUTE_CACHE_TTL_SEC: int = 604800  # 7일
    # 한 leg 폴리라인의 최대 점 수. 초과 시 단순화로 강제 축소(페이로드·룰 상한).
    ROUTE_MAX_POINTS: int = 200

    # [장소 결과 L1 캐시 - Redis]
    # 카카오 검색 결과를 짧게 캐싱해 동일 질의의 반복 외부 호출을 줄인다.
    REDIS_URL: str = "redis://redis:6379"
    REDIS_DB_CACHE: int = 4

    HUB_INTERNAL_TRUSTED_CIDRS: str = (
        "172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"
    )

    # true 면 /v1/* 공개 endpoint 도 X-Internal-Token 을 요구한다.
    # 기본 false = 현행 데모 동작(공개 endpoint 무인증). /health 는 항상 예외.
    AUTH_ENFORCED: bool = False


# 프로세스 단위 싱글톤. 임포트 시점에 환경변수 검증이 완료된다.
# 다른 모듈들은 `from app.config import settings` 로 직접 참조한다.
settings = Settings()
