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

    # 격자 사이 간격. 구독 격자가 시군구 단위로 늘어난 뒤로는 한 라운드가
    # 곧 격자 수 × 이 값이라, 1.5 초로 두면 한 바퀴에만 시간 예산의 삼분의
    # 일 가까이가 든다. 그러면 외부가 잠깐 흔들렸을 때 다시 시도할 여유가
    # 한두 번밖에 남지 않는다. 하루 호출 수는 이 값과 무관하다.
    KMA_POLL_INTERVAL_SEC: float = 0.5
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
    # 블로그 리뷰 결과 L1 캐시 TTL(6시간).
    NAVER_BLOG_CACHE_TTL_SEC: int = 21600
    # /v1/reviews display 파라미터 미지정 시 기본 표시 건수.
    NAVER_BLOG_DEFAULT_DISPLAY: int = 5

    # [장소 보강 - Google 장소 사진]
    # GOOGLE_MAPS_API_KEY 가 비어 있으면 실제 호출 대신 고정 스텁 응답을
    # 사용한다. 키는 X-Goog-Api-Key 헤더로만 전달되며 URL 쿼리에는 실리지
    # 않는다.
    #
    # 조회는 세 단계로 나뉜다. 검색어+좌표로 장소를 특정하고, 그 장소의
    # 사진 목록을 받고, 사진마다 이미지 URL 을 발급받는다. 앞의 두 단계는
    # 응답에 담을 필드를 최소로 지정하면 과금이 없고, 마지막 URL 발급만
    # 건당 과금된다. 그래서 상한도 URL 발급 횟수에만 건다.
    GOOGLE_MAPS_API_KEY: SecretStr = SecretStr("")
    GOOGLE_PLACES_TIMEOUT_SEC: float = 3.0
    # 사진 조회 한 건 전체에 거는 제한 시간(초). 위 타임아웃은 호출 하나에만
    # 걸리는데 조회는 세 단계를 이어 밟으므로, 단계마다 느려지면 합이 호출
    # 측이 기다려 주는 시간을 넘긴다. 그러면 hub 가 준비해 둔 "사진 없음"
    # 응답 대신 끊긴 연결이 전달된다. 전체 제한을 그보다 짧게 잡아, 늦어질
    # 때도 hub 가 직접 빈 목록으로 답하게 한다.
    GOOGLE_PHOTOS_TOTAL_BUDGET_SEC: float = 4.0
    # 검색을 좌표 주변으로 한정하는 반경(m). 같은 상호가 전국에 널린
    # 프랜차이즈에서 엉뚱한 지점이 잡히지 않게 좁게 둔다.
    GOOGLE_PLACES_BIAS_RADIUS_M: int = 500
    # 장소 식별자 캐시 TTL(30일). 식별자는 외부 약관이 장기 보관을 허용하는
    # 유일한 값이라 캐시에 담는다. 사진 이름과 이미지 URL 은 담지 않는다.
    GOOGLE_PLACEID_CACHE_TTL_SEC: int = 2592000
    # 검색해도 대응하는 장소가 없었다는 사실의 캐시 TTL(1일). 이것을 두지
    # 않으면 사진이 없는 장소를 열 때마다 검색이 다시 나간다. 새로 등록된
    # 장소가 하루 안에는 잡히도록 짧게 잡는다.
    GOOGLE_PLACEID_NEG_CACHE_TTL_SEC: int = 86400
    # 장소당 최대 사진 수와 요청 이미지 폭(px).
    GOOGLE_PHOTOS_MAX_COUNT: int = 3
    GOOGLE_PHOTOS_MAX_WIDTH_PX: int = 800
    # 하루에 발급할 수 있는 이미지 URL 총량. 0 으로 두면 사진 조회를 끈다.
    #
    # 주의: 이 값은 요금이 붙지 않는 범위를 넘는다. 발급처의 무료 한도는 달
    # 1,000장이고 여기 값 × 31일은 그보다 많다 — 넘는 만큼 발급처 요금제를
    # 따라 비용이 생긴다. 하루 32장이면 한도 안에 머물지만 그 수는 장소당
    # 세 장씩 잡아 하루 열 곳 남짓이라, 사람이 여럿 쓰는 동안 오전에 바닥나
    # 나머지 시간 내내 사진 없이 나갔다. 화면을 우선해 그 선을 넘긴다.
    GOOGLE_PHOTOS_DAILY_MEDIA_CAP: int = 50

    # [지하철 경로 - ODsay]
    # ODSAY_API_KEY 가 비어 있으면 실제 호출 대신 고정 스텁 응답을 쓴다.
    # 키는 URL 쿼리에 실려 나가므로 오류 본문·예외 메시지를 그대로 로그에
    # 올리지 않는다(_redact_secret 이 가린다).
    #
    # 발급처는 앱 등록마다 URI·서버·Android·iOS 로 키를 나눠 주는데, 요청에는
    # 어느 쪽인지 실리지 않아 어떤 키를 넣어도 여기서 호출된다.
    ODSAY_API_KEY: SecretStr = SecretStr("")
    # 예비 키. 비워 두면 폴백 없이 동작한다.
    #
    # 발급처는 등록해 둔 쓰임과 요청이 맞지 않으면 키 인증 자체를 거절한다.
    # 서버에서 부르는 지금 배선은 어느 키로도 통하는 것을 확인했지만, 콘솔에서
    # 플랫폼 키를 정리하거나 등록 정보를 바꾸는 순간 한쪽이 막힐 수 있다. 그때
    # 기능이 통째로 죽는 대신 다른 키로 한 번 더 시도하게 한다.
    #
    # 하루 한도가 키마다 따로인지 계정 단위인지는 확인하지 못했다. 그래서 이
    # 설비의 목적은 한도를 늘리는 것이 아니라 키 하나가 막혔을 때 살아남는 것이다.
    ODSAY_API_KEY_FALLBACK: SecretStr = SecretStr("")
    ODSAY_REQUEST_TIMEOUT_SEC: float = 3.0
    # 조회 한 건 전체에 거는 제한 시간(초). 호출 측이 기다려 주는 시간보다
    # 짧아야, 늦어질 때도 hub 가 직접 "조회 불가"로 답할 수 있다.
    ODSAY_TOTAL_BUDGET_SEC: float = 4.0
    # 성공·경로없음 결과의 캐시 TTL(6시간). 지하철 시간표는 거의 바뀌지 않는다.
    ODSAY_CACHE_TTL_SEC: int = 21600
    # 실패 결과도 잠깐 기억한다. 이것이 없으면 외부가 죽어 있는 동안 매 요청이
    # 제한 시간까지 기다린다. 복구를 오래 못 알아채지 않도록 짧게 잡는다.
    ODSAY_FAIL_CACHE_TTL_SEC: int = 60
    # 하루에 나갈 수 있는 외부 호출 총량. 무료 등급의 하루 한도가 낮아
    # 그 아래로 잡는다. 0 이면 조회를 끈다.
    ODSAY_DAILY_CALL_CAP: int = 900
    # 캐시 키를 만들 때 좌표를 반올림할 소수 자릿수. 4 자리면 10 m 남짓이라
    # 같은 건물에서 출발한 요청이 한 칸으로 모인다.
    ODSAY_CACHE_COORD_DIGITS: int = 4

    # [따릉이 대여소 - 서울 열린데이터광장]
    # SEOUL_OPENAPI_KEY 가 비어 있으면 고정 스텁 응답을 쓴다. 키가 URL 경로에
    # 실리는 형태라 여기도 로그 가리기가 필요하다.
    #
    # 발급처가 https 를 받지 않아 이 호출만 평문으로 나간다. 앱이 직접 부르지
    # 않고 hub 를 거치게 한 이유 중 하나다.
    SEOUL_OPENAPI_KEY: SecretStr = SecretStr("")
    SEOUL_BIKE_REQUEST_TIMEOUT_SEC: float = 3.0
    SEOUL_BIKE_TOTAL_BUDGET_SEC: float = 4.0
    # 전량 스냅샷 캐시 TTL(5분). 좌표별로 캐시하지 않는 이유는 라우터 주석에
    # 적었다. 이 값이 짧아질수록 하루 외부 호출 수가 그대로 비례해 늘어난다.
    SEOUL_BIKE_CACHE_TTL_SEC: int = 300
    # 일부만 받아 온 스냅샷의 TTL(초). 다음 요청에서 다시 채우도록 짧게 둔다.
    SEOUL_BIKE_PARTIAL_CACHE_TTL_SEC: int = 30
    # 한 번에 받아 오는 행 수와 최대 페이지 수. 발급처가 한 번에 주는 양이
    # 정해져 있어 나눠 받는다. 상한을 두지 않으면 응답의 개수 필드가 이상할 때
    # 요청 하나가 끝나지 않는다.
    SEOUL_BIKE_PAGE_SIZE: int = 1000
    SEOUL_BIKE_MAX_PAGES: int = 5
    # 요청 좌표에서 잘라 보낼 기본 반경(m).
    SEOUL_BIKE_DEFAULT_RADIUS_M: int = 5000

    # [공유 킥보드 - 국토교통부 퍼스널모빌리티]
    # PM_SERVICE_KEY 가 비어 있으면 KMA_SERVICE_KEY 를 그대로 쓴다 — 같은
    # 발급처(data.go.kr)의 한 계정 키로 여러 서비스가 열려 있는 경우가 흔해
    # 키를 두 번 적지 않아도 되게 한다. 대기오염 쪽과 같은 방식이다.
    # 둘 다 비면 스텁으로 동작한다.
    #
    # 키가 쿼리에 실려 나가므로 오류 본문·예외 메시지를 그대로 로그에 올리지
    # 않는다.
    PM_SERVICE_KEY: SecretStr = SecretStr("")
    # 호출 하나에 거는 제한 시간은 조회 전체에 거는 시간보다 짧아야 한다.
    # 뒤집히면 전체 시간이 먼저 끝나 버려, 호출 쪽 제한은 한 번도 쓰이지
    # 못하고 사업자별 실패와 전체 시간 초과가 구분되지 않는다.
    PM_REQUEST_TIMEOUT_SEC: float = 3.0
    PM_TOTAL_BUDGET_SEC: float = 4.0
    # 사업자마다 따로 물어야 해서 한 번 조회에 호출이 여러 번 나간다. 그만큼
    # 캐시가 중요하다. 위치가 계속 움직이는 값이라 짧게 잡는다.
    PM_CACHE_TTL_SEC: int = 120
    PM_FAIL_CACHE_TTL_SEC: int = 30
    # 한 사업자당 받아 올 최대 대수.
    PM_NUMOFROWS: int = 100
    # 요청 좌표에서 잘라 보낼 기본 반경(m). 킥보드는 걸어가서 타는 것이라
    # 대여소보다 좁게 둔다.
    PM_DEFAULT_RADIUS_M: int = 1000
    # 조회할 사업자 목록(콤마 구분). 발급처가 사업자명을 필수로 받는데 목록을
    # 따로 주지 않아 여기에 적어 둔다.
    PM_PROVIDERS: str = "Beam,GCOO,SWING,씽씽,킥고잉,Lime,지쿠,알파카"

    # 키가 채워져 있어도 강제로 스텁 응답만 쓰고 싶을 때 True 로 둔다.
    PLACES_STUB_MODE: bool = False

    # [현재 날씨 - 초단기실황 + 대기오염]
    # AIRKOREA_SERVICE_KEY 는 data.go.kr 대기오염정보 서비스 키. 비어 있으면
    # KMA_SERVICE_KEY 를 그대로 쓴다 — 두 서비스가 한 계정 키로 열려 있는
    # 경우가 흔해서, 키를 두 번 적지 않아도 되게 한다. 둘 다 비면 스텁.
    AIRKOREA_SERVICE_KEY: SecretStr = SecretStr("")
    # 아래 곁들이 정보 예산 안에서 끝나야 한다. 예산이 먼저 끝나면 이 호출이
    # 도중에 잘리는데, 그러면 실패를 기억해 두는 자리(아래 실패 캐시)까지
    # 함께 잘려 다음 요청도 똑같이 기다리게 된다.
    AIRKOREA_REQUEST_TIMEOUT_SEC: float = 1.5
    # 현재 날씨 한 건에 거는 시간. 위 개별 타임아웃은 호출 하나에만 걸리는데
    # 이 조회는 실황·기록·예보·대기오염을 이어 밟으므로, 합이 호출 측이
    # 기다려 주는 시간을 넘기면 응답 대신 끊긴 연결이 전달된다. 둘을 합쳐도
    # 호출 측 제한보다 짧게 잡아, 늦어질 때도 hub 가 직접 답하게 한다.
    #
    # 실황 예산: 이 값이 없으면 카드를 그릴 수 없어 초과 시 502.
    # 곁들이 예산: 어제 비교·오늘 예보·미세먼지. 초과해도 그 항목만 빠진다.
    WEATHER_NOW_OBSERVATION_BUDGET_SEC: float = 2.0
    WEATHER_NOW_ENRICH_BUDGET_SEC: float = 2.0
    # 미리 받아 둔 값을 "지금 값"으로 내보낼 수 있는 한계(시간).
    #
    # 둘 다 매시 받아 두므로 정상일 때는 한 시간 안쪽 값이다. 이 한계는
    # 발급처가 몇 번 연속 실패했을 때 화면을 지킬 만큼은 넉넉하고, 그 이상
    # 끊기면 낡은 값을 지금 값이라고 내보내지 않을 만큼은 짧게 잡는다.
    # 한계를 넘으면 해당 항목을 응답에서 비운다(화면은 그 자리를 감춘다).
    WEATHER_NOW_MAX_AGE_HOURS: int = 2
    AIR_MAX_AGE_HOURS: int = 3
    # 실황은 매시 발표라 짧게, 대기오염은 매시 갱신이라 조금 더 길게 잡는다.
    WEATHER_NOW_CACHE_TTL_SEC: int = 300
    AIRKOREA_CACHE_TTL_SEC: int = 900
    # 대기오염 조회 실패도 잠깐 기억한다. 키가 대기오염 서비스에 신청돼
    # 있지 않거나 외부가 죽어 있으면 매 요청이 타임아웃까지 기다리게 되어,
    # 부가 정보 하나 때문에 현재 날씨 응답 전체가 느려진다.
    AIRKOREA_FAIL_CACHE_TTL_SEC: int = 60
    # 시도 단위 측정소 목록 요청 크기. 측정소가 100 곳을 넘는 시도가 있어
    # 기본값을 그보다 넉넉히 잡는다 — 잘리면 뒤쪽 측정소가 후보에서 빠져
    # 요청한 시군구와 먼 곳의 농도가 대표로 뽑힌다.
    AIRKOREA_NUMOFROWS: int = 200
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

    # 루트 로거 레벨. uvicorn 은 자기 로거만 구성하고 루트 로거에는 핸들러를
    # 붙이지 않으므로, app/main.py 의 _configure_logging 이 부팅 시 루트
    # 핸들러가 비어 있을 때만 stdout 핸들러를 붙여 app.* 로그를 살린다.
    LOG_LEVEL: str = "INFO"


# 프로세스 단위 싱글톤. 임포트 시점에 환경변수 검증이 완료된다.
# 다른 모듈들은 `from app.config import settings` 로 직접 참조한다.
settings = Settings()
