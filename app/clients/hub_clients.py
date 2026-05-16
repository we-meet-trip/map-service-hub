# 외부 API 클라이언트 6종을 본 모듈에 정의한다.
# 모두 httpx.AsyncClient 기반으로 비동기 호출을 캡슐화한다.


class KakaoLocalClient:
    """Kakao Local Search API 호출을 캡슐화하는 클라이언트.

    장소 키워드 검색과 좌표 기반 검색을 제공한다.
    """
    pass


class KakaoMobilityClient:
    """Kakao Mobility API 호출을 캡슐화하는 클라이언트.

    자동차 경로 탐색을 제공한다.
    """
    pass


class TourAPIClient:
    """TourAPI KorService 호출을 캡슐화하는 클라이언트.

    관광 정보(장소 메타데이터·이미지·상세 설명)를 조회한다.
    """
    pass


class KMAClient:
    """기상청(KMA) 단기·중기 예보 API 호출을 캡슐화하는 클라이언트.

    좌표·발표 시각 기반으로 강수·기온 등 예보를 조회한다.
    """
    pass


class NaverBlogClient:
    """Naver Blog 검색 API 호출을 캡슐화하는 클라이언트.

    장소 보강을 위한 블로그 텍스트를 조회한다.
    """
    pass


class OSRMClient:
    """OSRM 라우팅 엔진 호출을 캡슐화하는 클라이언트.

    foot·bicycle 프로파일에 대해 경로·소요 시간을 조회한다.
    """
    pass
