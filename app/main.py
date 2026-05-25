from fastapi import FastAPI

from app.routers.hub_routers import router as hub_router

app = FastAPI(title="map-service-hub", version="0.0.1-poc")
app.include_router(hub_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "hub"}
