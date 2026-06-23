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

    HUB_INTERNAL_TRUSTED_CIDRS: str = (
        "172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"
    )


# 프로세스 단위 싱글톤. 임포트 시점에 환경변수 검증이 완료된다.
# 다른 모듈들은 `from app.config import settings` 로 직접 참조한다.
settings = Settings()
