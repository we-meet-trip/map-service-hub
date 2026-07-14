"""GPX 트랙 파일을 내려받아 대표 좌표와 경계 상자를 계산한다.

코스 정보 응답에는 좌표가 들어 있지 않고 GPX 파일 URL 만 제공되므로,
코스를 지도에 한 점으로 표현하기 위해 트랙의 시작점을 대표 좌표로 삼고
전체 트랙의 bounding box 를 함께 계산한다.

본 모듈은 표준 라이브러리 XML 파서와 httpx 만 사용한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CourseGeometry:
    """GPX 에서 추출한 코스 기하 요약.

    start_lat / start_lng: 트랙 첫 점. 코스의 대표 좌표로 쓴다.
    min_lat / min_lng / max_lat / max_lng: 전체 트랙의 경계 상자.
    point_count: 파싱한 좌표 점 수.
    """

    start_lat: float
    start_lng: float
    min_lat: float
    min_lng: float
    max_lat: float
    max_lng: float
    point_count: int


def _local(tag: str) -> str:
    """네임스페이스 접두사를 제거한 로컬 태그명을 돌려준다.

    GPX 는 기본 네임스페이스를 쓰므로 ElementTree 태그가
    "{ns}trkpt" 형태로 온다. 마지막 '}' 이후만 취한다.
    """
    return tag.rsplit("}", 1)[-1]


def parse_points(gpx_text: str) -> list[tuple[float, float]]:
    """GPX 문자열에서 (lat, lon) 점들을 순서대로 추출한다.

    트랙 점(trkpt)을 우선 사용하고, 없으면 경로점(rtept)·웨이포인트(wpt)
    순으로 대체한다. lat/lon 속성은 GPX 표준 순서(위도, 경도)다.
    파싱 불가한 입력은 ValueError 로 알린다.
    """
    try:
        root = ET.fromstring(gpx_text)
    except ET.ParseError as e:
        raise ValueError(f"invalid gpx: {e}") from e
    for kind in ("trkpt", "rtept", "wpt"):
        pts: list[tuple[float, float]] = []
        for el in root.iter():
            if _local(el.tag) != kind:
                continue
            lat = el.get("lat")
            lon = el.get("lon")
            if lat is None or lon is None:
                continue
            try:
                pts.append((float(lat), float(lon)))
            except ValueError:
                continue
        if pts:
            return pts
    return []


def summarize(
    points: list[tuple[float, float]],
) -> CourseGeometry | None:
    """좌표 점 목록에서 대표 좌표와 경계 상자를 계산한다.

    points 가 비어 있으면 None 을 반환한다.
    """
    if not points:
        return None
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return CourseGeometry(
        start_lat=points[0][0],
        start_lng=points[0][1],
        min_lat=min(lats),
        min_lng=min(lngs),
        max_lat=max(lats),
        max_lng=max(lngs),
        point_count=len(points),
    )


async def fetch_geometry(
    client: httpx.AsyncClient, gpx_url: str
) -> CourseGeometry | None:
    """GPX URL 을 내려받아 CourseGeometry 로 요약한다.

    client: 재사용할 httpx 비동기 클라이언트.
    gpx_url: 코스의 GPX 파일 URL.
    반환: 좌표를 하나라도 얻으면 CourseGeometry, 아니면 None.
        다운로드/파싱 실패는 None 으로 흡수하고 경고 로그만 남긴다
        (한 코스의 GPX 실패가 전체 동기화를 멈추지 않도록).
    """
    try:
        r = await client.get(gpx_url)
        r.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("gpx fetch failed url=%s err=%s", gpx_url, e)
        return None
    try:
        points = parse_points(r.text)
    except ValueError as e:
        logger.warning("gpx parse failed url=%s err=%s", gpx_url, e)
        return None
    return summarize(points)
