"""내부 endpoint — KMA polling 즉시 트리거.

운영/디버깅 목적으로 cron 스케줄을 기다리지 않고 단기/중기 폴링과
housekeeping 을 즉시 실행할 수 있는 /internal/* 경로를 제공한다.

보호 메커니즘(2중):
  1) CIDR 화이트리스트 — 사설 IP 대역에서만 호출 가능
  2) 공유 비밀 헤더 X-Internal-Token — settings.INTERNAL_SERVICE_TOKEN 과 일치 필요

호출 관계:
  - app.main 이 본 모듈의 router 를 include
  - run_now 가 hub_scheduler 의 폴링 루프/housekeeping 을 직접 호출
"""
from __future__ import annotations

import asyncio
import ipaddress
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config import settings
from app.scheduler.hub_scheduler import (
    housekeeping_job,
    mid_term_polling_loop,
    short_term_polling_loop,
)

# 모듈 임포트 시점에 settings 의 콤마 구분 CIDR 문자열을
# ipaddress.IPv4Network/IPv6Network 객체 리스트로 변환해 캐싱한다.
# 빈 토큰은 split 결과에서 제거.
_PRIVATE_CIDRS = [
    ipaddress.ip_network(c.strip())
    for c in settings.HUB_INTERNAL_TRUSTED_CIDRS.split(",")
    if c.strip()
]


def _is_trusted(ip_str: str) -> bool:
    """_is_trusted — IP 문자열이 신뢰 CIDR 에 속하는지 판정

    ip_str: 클라이언트 IP 문자열. 형식이 IP 가 아니면(빈 문자열 포함)
        False 반환.

    반환: 신뢰 CIDR 중 어느 하나에라도 포함되면 True.
    호출처: internal_guard / 테스트 test_internal_guard.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in n for n in _PRIVATE_CIDRS)


async def internal_guard(request: Request) -> None:
    """internal_guard — /internal/* endpoint 의 의존성 가드

    요청 본 처리 전에 호출되어 두 조건을 모두 검사하고, 실패 시 403 발생.
      1) request.client.host 가 신뢰 CIDR 에 속하는가
      2) 헤더 X-Internal-Token 이 INTERNAL_SERVICE_TOKEN 과 정확히 일치하는가

    request: FastAPI 가 주입하는 Request 객체.
    반환: None (성공). 실패 시 HTTPException(403) 을 발생.

    호출처: 본 모듈의 APIRouter(dependencies=[Depends(internal_guard)])
        에 의해 모든 /internal/* 라우트 진입 직전 자동 실행.
    """
    client = request.client.host if request.client else ""
    if not _is_trusted(client):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"internal endpoint denied for {client}",
        )
    if (
        request.headers.get("X-Internal-Token")
        != settings.INTERNAL_SERVICE_TOKEN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid internal token",
        )


# 모든 라우트에 prefix "/internal" 과 internal_guard 의존성을 자동 부여.
router = APIRouter(prefix="/internal", dependencies=[Depends(internal_guard)])


@router.post("/kma/run-now")
async def run_now(
    which: Literal["short", "mid", "housekeep"],
) -> dict[str, object]:
    """POST /internal/kma/run-now — 폴링/하우스키핑 즉시 트리거

    which: 무엇을 트리거할지 선택.
        "short"     — short_term_polling_loop 를 백그라운드 태스크로 시작
        "mid"       — mid_term_polling_loop 를 백그라운드 태스크로 시작
        "housekeep" — housekeeping_job 을 await 로 직접 실행(완료까지 대기)

    short/mid 은 장시간 루프이므로 asyncio.create_task 로 분리해
    응답을 즉시 반환한다. housekeep 은 짧으므로 await.

    반환: {"ok": True, "triggered": <which>}.
    호출처: 내부 운영자 / 운영 스크립트.
    """
    if which == "short":
        asyncio.create_task(short_term_polling_loop())
    elif which == "mid":
        asyncio.create_task(mid_term_polling_loop())
    else:
        await housekeeping_job()
    return {"ok": True, "triggered": which}
