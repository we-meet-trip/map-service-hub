from __future__ import annotations

import os

os.environ.setdefault(
    "HUB_DATABASE_URL",
    "postgresql+psycopg://test:test@localhost/test",
)
os.environ.setdefault("KMA_SERVICE_KEY", "test-service-key")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-token")
