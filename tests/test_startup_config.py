import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from alembic import command
from alembic.config import Config
from pydantic import SecretStr


def test_offline_migrations_accept_percent_encoded_password(monkeypatch):
    root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+psycopg://test:synthetic%40p%2F%25@127.0.0.1/test")
    output = io.StringIO()
    config = Config(output_buffer=output)
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("version_locations", str(root / "migrations/revisions"))
    config.set_main_option("path_separator", "os")
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE" in sql and "alembic_version" in sql
    assert "synthetic" not in sql


def test_startup_does_not_create_unconfigured_external_clients(monkeypatch):
    import app.main as main

    settings = main.settings
    for name in (
        "KAKAO_REST_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
        "GOOGLE_MAPS_API_KEY", "KMA_SERVICE_KEY", "AIRKOREA_SERVICE_KEY",
        "ODSAY_API_KEY", "ODSAY_API_KEY_FALLBACK", "SEOUL_OPENAPI_KEY", "PM_SERVICE_KEY",
    ):
        monkeypatch.setattr(settings, name, SecretStr(""))
    for name in ("OSRM_FOOT_BASE_URL", "OSRM_BICYCLE_BASE_URL"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    constructors = []
    for name in ("KakaoLocalClient", "NaverBlogClient", "GooglePlacesClient", "KMAClient",
                 "AirKoreaClient", "OdsayClient", "SeoulBikeClient", "PmClient", "OsrmClient"):
        constructor = Mock(side_effect=AssertionError("unconfigured provider called"))
        monkeypatch.setattr(main, name, constructor)
        constructors.append(constructor)
    scheduler = Mock()
    monkeypatch.setattr(main, "build_scheduler", lambda: scheduler)
    cache = Mock(aclose=AsyncMock())
    monkeypatch.setattr(main, "RedisCache", lambda *_: cache)
    monkeypatch.setattr(main, "dispose_hub_db", AsyncMock())
    for name in ("short_term_polling_loop", "mid_term_polling_loop", "nowcast_polling_loop",
                 "air_polling_loop", "durunubi_sync_loop"):
        monkeypatch.setattr(main, name, AsyncMock())

    async def run():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)

    asyncio.run(run())
    for constructor in constructors:
        constructor.assert_not_called()
    cache.aclose.assert_awaited_once()
    scheduler.shutdown.assert_called_once()
