"""내부 endpoint — KMA polling 즉시 트리거."""
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

_PRIVATE_CIDRS = [
    ipaddress.ip_network(c.strip())
    for c in settings.HUB_INTERNAL_TRUSTED_CIDRS.split(",")
    if c.strip()
]


def _is_trusted(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in n for n in _PRIVATE_CIDRS)


async def internal_guard(request: Request) -> None:
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


router = APIRouter(prefix="/internal", dependencies=[Depends(internal_guard)])


@router.post("/kma/run-now")
async def run_now(
    which: Literal["short", "mid", "housekeep"],
) -> dict[str, object]:
    if which == "short":
        asyncio.create_task(short_term_polling_loop())
    elif which == "mid":
        asyncio.create_task(mid_term_polling_loop())
    else:
        await housekeeping_job()
    return {"ok": True, "triggered": which}
