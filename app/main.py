from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.db.hub_db import dispose_hub_db
from app.routers.hub_routers import router as hub_router
from app.routers.internal_router import router as internal_router
from app.scheduler.hub_scheduler import (
    build_scheduler,
    mid_term_polling_loop,
    short_term_polling_loop,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("hub: APScheduler started")
    asyncio.create_task(short_term_polling_loop())
    asyncio.create_task(mid_term_polling_loop())
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await dispose_hub_db()
        logger.info("hub: scheduler/db disposed")


app = FastAPI(title="map-service-hub", version="0.0.1-poc", lifespan=lifespan)
app.include_router(hub_router)
app.include_router(internal_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "hub"}
