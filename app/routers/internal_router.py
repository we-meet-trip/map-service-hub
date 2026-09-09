"""내부 endpoint — KMA polling 즉시 트리거.

운영/디버깅 목적으로 cron 스케줄을 기다리지 않고 단기/중기 폴링과
housekeeping 을 즉시 실행할 수 있는 /internal/* 경로를 제공한다.

보호 메커니즘(2중):
  1) CIDR 화이트리스트 — 사설 IP 대역에서만 호출 가능
  2) 공유 비밀 헤더 X-Internal-Token — settings.HUB_ADMIN_INTERNAL_TOKEN 과 일치 필요

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
    MID_TASK_NAME,
    SHORT_TASK_NAME,
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


def _check_token(request: Request, expected: str) -> None:
    client = request.client.host if request.client else ""
    token = request.headers.get("X-Internal-Token")
    if (not _is_trusted(client) or not expected.strip() or token is None
            or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="internal access denied")


async def internal_guard(request: Request) -> None:
    """일반 서비스용 CIDR 및 자격 검사."""
    _check_token(request, settings.INTERNAL_SERVICE_TOKEN.get_secret_value())


async def internal_admin_guard(request: Request) -> None:
    """관리 기능은 일반 서비스와 다른 환경별 관리 전용 자격만 허용한다."""
    ordinary = settings.INTERNAL_SERVICE_TOKEN.get_secret_value()
    dedicated = settings.HUB_ADMIN_INTERNAL_TOKEN.get_secret_value()
    if not dedicated.strip() or hmac.compare_digest(dedicated.encode("utf-8"), ordinary.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="internal access denied")
    _check_token(request, dedicated)


# 백그라운드 폴링 태스크 강참조 보관소. asyncio.create_task 결과를
# 보관하지 않으면 이벤트 루프가 약참조만 들고 있어, await asyncio.sleep
# 구간 등에서 GC 가 실행 중 태스크를 수거할 수 있다. 완료 시
# add_done_callback 으로 자동 제거한다.
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# 폴링 태스크 이름. lifespan(app.main)이 부팅 직후 띄우는 태스크, 신선도
# 감시 잡과 같은 이름이어야 중복 실행 가드가 서로를 알아본다.
_SHORT_TASK = SHORT_TASK_NAME
_MID_TASK = MID_TASK_NAME


def _track(task: asyncio.Task) -> None:
    """create_task 결과를 모듈 집합에 강참조로 보관(완료 시 자동 제거)."""
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _find_running_task(name: str) -> "asyncio.Task | None":
    """이름이 name 인 진행 중(미완료) 백그라운드 태스크를 찾는다(없으면 None).

    이 모듈이 만든 것만이 아니라 **루프에 떠 있는 모든 태스크**를 본다.
    부팅 직후 lifespan 이 띄우는 폴링은 이 모듈의 집합에 들어오지 않아,
    자기 집합만 보면 기동 20분 안에 들어온 수동 실행 요청이 중복 가드를
    그냥 통과해 같은 발표분을 두 번 긁는다.

    순회 중 집합 변경을 피하려 스냅샷으로 순회한다.
    """
    try:
        candidates = asyncio.all_tasks()
    except RuntimeError:
        # 실행 중인 루프가 없는 문맥(단위 테스트 등)에서는 자기 집합만 본다.
        candidates = set(_BACKGROUND_TASKS)
    for task in list(candidates):
        if task.get_name() == name and not task.done():
            return task
    return None


# 수동 폴링/정리는 관리 전용 자격으로 제한한다.
router = APIRouter(prefix="/internal", dependencies=[Depends(internal_admin_guard)])


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
    409 Conflict 를 반환한다(중복 KMA 호출/429·DB 락 경합 예방).
    부팅 직후 lifespan 이 띄운 폴링도 같은 이름을 쓰므로 함께 걸린다.
    남는 구멍은 cron 스케줄과의 동시 실행 하나다 — APScheduler 의
    max_instances=1 은 cron 끼리만 보호한다.

    반환: {"ok": True, "triggered": <which>}.
    호출처: 내부 운영자 / 운영 스크립트.
    """
    if which == "short":
        if _find_running_task(_SHORT_TASK) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="short_term_polling_loop already running",
            )
        task = asyncio.create_task(short_term_polling_loop())
        task.set_name(_SHORT_TASK)
        _track(task)
    elif which == "mid":
        if _find_running_task(_MID_TASK) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="mid_term_polling_loop already running",
            )
        task = asyncio.create_task(mid_term_polling_loop())
        task.set_name(_MID_TASK)
        _track(task)
    else:
        await housekeeping_job()
    return {"ok": True, "triggered": which}
