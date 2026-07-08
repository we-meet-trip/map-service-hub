"""공개 endpoint(/v1/*) 선택적 인증 가드.

기본(AUTH_ENFORCED=false)에서는 아무 검사도 하지 않아 현행 데모 동작
(공개 endpoint 무인증)을 그대로 보존한다. AUTH_ENFORCED=true 면
/internal/* 과 동일한 공유 비밀 헤더 X-Internal-Token 을 요구한다. 단
공개 endpoint 이므로 internal_guard 와 달리 CIDR 화이트리스트는 적용하지
않는다.

호출 관계:
  - hub_routers.router / rules_router.router 의
    APIRouter(dependencies=[Depends(public_guard)]) 에 의해 각 /v1/* 라우트
    진입 직전 자동 실행.
  - /health 는 main 의 app 에 직접 선언되어 라우터 의존성 밖이므로 본
    가드를 거치지 않는다(AUTH_ENFORCED 와 무관하게 항상 무인증).
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from app.config import settings


async def public_guard(request: Request) -> None:
    """public_guard — /v1/* 공개 endpoint 의 선택적 인증 가드

    settings.AUTH_ENFORCED 가 false 면 즉시 통과(no-op)한다. true 면 헤더
    X-Internal-Token 이 INTERNAL_SERVICE_TOKEN 과 정확히 일치해야 하며,
    누락(None) 또는 불일치 시 401 을 발생한다.

    internal_guard 와 달리 CIDR 검사는 하지 않는다(공개 endpoint). 타이밍
    공격 방지를 위해 상수시간 비교(hmac.compare_digest)를 사용한다. 헤더
    값은 UTF-8 바이트로 인코딩한 뒤 비교하므로, 비-ASCII 헤더(Starlette 가
    latin-1 로 디코딩)가 들어와도 예외 없이 불일치(401)로 fail-closed 된다.

    request: FastAPI 가 주입하는 Request 객체.
    반환: None (성공). 실패 시 HTTPException(401) 을 발생.
    """
    if not settings.AUTH_ENFORCED:
        return
    token = request.headers.get("X-Internal-Token")
    expected = settings.INTERNAL_SERVICE_TOKEN.get_secret_value()
    if token is None or not hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid internal token",
        )
