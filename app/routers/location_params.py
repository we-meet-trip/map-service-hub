"""요청에 실려 온 좌표를 꺼낸다.

좌표는 감싸서 온다(app.crypto.location_seal). 여기서는 그 봉투를 열어 값으로
되돌리고, 열 수 없으면 요청을 거절한다.

**열쇠가 설정돼 있으면 값으로 온 좌표는 받지 않는다.** 봉투를 못 열었을 때
평문으로 물러서면, 감싸는 쪽이 조용히 고장 나도 아무도 알아차리지 못한 채
예전처럼 평문이 흐른다. 그 상태는 감싸지 않은 것과 같으면서 감쌌다고 믿게
만든다는 점에서 더 나쁘다.

열쇠가 없으면 예전처럼 값으로 받는다. 상대 서비스가 아직 감쌀 줄 모르는
동안 넘어가기 위한 것이며, 그때는 부팅 시 한 번 경고를 남긴다.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from app.config import settings
from app.crypto.location_seal import SealError, open_seal

logger = logging.getLogger(__name__)

KOREA_LAT = (33.0, 43.0)
KOREA_LNG = (124.0, 132.0)


def sealing_required() -> bool:
    """열쇠가 있으면 봉투만 받는다."""
    return bool(settings.LOCATION_WIRE_KEY.get_secret_value())


def _reject(reason: str) -> HTTPException:
    # 사유를 밖으로 자세히 알리지 않는다. 어느 단계에서 막혔는지 알려 주면
    # 그것만으로 무엇을 바꿔 가며 시도할지가 정해진다.
    logger.warning("location seal rejected reason=%s", reason)
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="invalid location"
    )


def _in_korea(lat: float, lng: float) -> bool:
    return KOREA_LAT[0] <= lat <= KOREA_LAT[1] and KOREA_LNG[0] <= lng <= KOREA_LNG[1]


def _opened(request: Request, loc: str | None, fields: tuple[str, ...]) -> dict:
    if loc is None:
        raise _reject("missing sealed location")
    try:
        payload = open_seal(loc)
    except SealError as e:
        raise _reject(str(e)) from e
    values = {}
    for name in fields:
        value = payload.get(name)
        if not isinstance(value, (int, float)):
            raise _reject(f"missing field {name}")
        values[name] = float(value)
    # 좌표를 쓴 사실을 남긴다. 위치 자체는 남기지 않는다 — 기록이 또 하나의
    # 위치 저장소가 되면 감싼 의미가 없다.
    logger.info(
        "location seal opened path=%s caller=%s",
        request.url.path,
        request.client.host if request.client else "unknown",
    )
    return values


def resolve_point(
    request: Request, loc: str | None, lat: float | None, lng: float | None
) -> tuple[float, float]:
    """한 지점을 꺼낸다."""
    if sealing_required():
        values = _opened(request, loc, ("lat", "lng"))
        lat, lng = values["lat"], values["lng"]
    elif lat is None or lng is None:
        raise _reject("missing lat/lng")
    if not _in_korea(lat, lng):
        raise _reject("out of range")
    return lat, lng


def resolve_pair(
    request: Request,
    loc: str | None,
    start_lat: float | None,
    start_lng: float | None,
    end_lat: float | None,
    end_lng: float | None,
) -> tuple[float, float, float, float]:
    """출발·도착 두 지점을 꺼낸다."""
    if sealing_required():
        values = _opened(
            request, loc, ("start_lat", "start_lng", "end_lat", "end_lng")
        )
        start_lat = values["start_lat"]
        start_lng = values["start_lng"]
        end_lat = values["end_lat"]
        end_lng = values["end_lng"]
    elif None in (start_lat, start_lng, end_lat, end_lng):
        raise _reject("missing start/end")
    if not _in_korea(start_lat, start_lng) or not _in_korea(end_lat, end_lng):
        raise _reject("out of range")
    return start_lat, start_lng, end_lat, end_lng


def resolve_legs(request: Request, loc: str | None, legs: list) -> list:
    """경로 요청의 구간 목록을 꺼낸다.

    구간에는 좌표뿐 아니라 장소 이름도 들어간다. 이름만으로도 어디를 다니는지가
    드러나므로 함께 감싸고, 함께 꺼낸다.
    """
    if sealing_required():
        if loc is None:
            raise _reject("missing sealed legs")
        try:
            payload = open_seal(loc)
        except SealError as e:
            raise _reject(str(e)) from e
        opened = payload.get("legs")
        if not isinstance(opened, list):
            raise _reject("sealed legs missing")
        legs = opened
        logger.info(
            "location seal opened path=%s caller=%s legs=%d",
            request.url.path,
            request.client.host if request.client else "unknown",
            len(legs),
        )
    if not 1 <= len(legs) <= 20:
        raise _reject("legs out of range")
    return legs
