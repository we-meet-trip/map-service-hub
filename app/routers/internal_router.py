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
import hmac
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
    # 타이밍 공격 방지를 위해 상수시간 비교(hmac.compare_digest)를 사용한다.
    # 헤더 미존재(None)는 불일치로 처리해 기존 의미(불일치 시 403)를 보존한다.
    # 헤더 값은 UTF-8 바이트로 인코딩한 뒤 비교한다. str 끼리 비교하면
    # 비-ASCII 헤더(Starlette 가 latin-1 로 디코딩)에서 compare_digest 가
    # TypeError 를 던져 403 대신 500 이 나간다. bytes 비교는 그런 입력에도
    # 예외 없이 불일치(403)로 fail-closed 된다.
    token = request.headers.get("X-Internal-Token")
    expected = settings.INTERNAL_SERVICE_TOKEN.get_secret_value()
    if token is None or not hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid internal token",
        )


# 백그라운드 폴링 태스크 강참조 보관소. asyncio.create_task 결과를
# 보관하지 않으면 이벤트 루프가 약참조만 들고 있어, await asyncio.sleep
# 구간 등에서 GC 가 실행 중 태스크를 수거할 수 있다. 완료 시
# add_done_callback 으로 자동 제거한다.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    """create_task 결과를 모듈 집합에 강참조로 보관(완료 시 자동 제거)."""
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _find_running_task(name: str) -> "asyncio.Task | None":
    """이름이 name 인 진행 중(미완료) 백그라운드 태스크를 찾는다(없으면 None).

    순회 중 set 변경을 피하려 스냅샷(list)으로 순회한다. run_now 가
    동일 종류의 폴링 루프 중복 실행을 막는 가드에 사용한다.
    """
    for task in list(_BACKGROUND_TASKS):
        if task.get_name() == name and not task.done():
            return task
    return None


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

    중복 실행 방지: 동일 종류(short/mid)의 폴링 루프가 이미 진행 중이면
    409 Conflict 를 반환한다(중복 KMA 호출/429·DB 락 경합 예방). 단 이
    가드는 본 HTTP 트리거 간 중복만 막으며, cron 스케줄과의 동시 실행까지
    막지는 않는다(APScheduler max_instances=1 은 cron-cron 만 보호).

    반환: {"ok": True, "triggered": <which>}.
    호출처: 내부 운영자 / 운영 스크립트.
    """
    if which == "short":
        if _find_running_task("short") is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="short_term_polling_loop already running",
            )
        task = asyncio.create_task(short_term_polling_loop())
        task.set_name("short")
        _track(task)
    elif which == "mid":
        if _find_running_task("mid") is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="mid_term_polling_loop already running",
            )
        task = asyncio.create_task(mid_term_polling_loop())
        task.set_name("mid")
        _track(task)
    else:
        await housekeeping_job()
    return {"ok": True, "triggered": which}
