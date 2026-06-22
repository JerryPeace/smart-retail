"""Pytest configuration and shared fixtures.

Pre-conditions for e2e tests (test_pipeline_e2e.py):
    docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres

The ANALYZER_MOCK_MODE env var MUST be set before any recommender import because
Settings is instantiated at module-import time (config.py:52). This module is
loaded by pytest before any test file, so setting it here guarantees correctness.

Caveat: main.py calls load_dotenv(override=True) which reads .env.local and can
override ANALYZER_MOCK_MODE back to false. To defend against this we (a) set the
env var before the import, AND (b) forcibly patch settings.analyzer_mock_mode to
True after the import so no subsequent service initialisation reads False.
"""
import os

# === Step 1: set env var before any recommender import ===
os.environ["ANALYZER_MOCK_MODE"] = "true"

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from recommender.main import app  # noqa: E402

# === Step 2: main.py runs load_dotenv(override=True) which may reset the env
# var to the .env.local value.  Re-assert both so every downstream Settings
# consumer (services instantiated per-request via Depends) sees mock=True.
os.environ["ANALYZER_MOCK_MODE"] = "true"
from recommender.config import settings as _settings  # noqa: E402
_settings.analyzer_mock_mode = True


@pytest.fixture()
async def client() -> AsyncClient:
    """Async HTTP client backed by the FastAPI ASGI app.

    Uses ASGITransport so tests run entirely in-process — no real TCP socket,
    no port binding.  The lifespan (DB init + LLM preheat) is executed once
    per client context, which means each test that uses this fixture gets a
    fresh lifespan cycle.

    In mock mode the LLM preheat is skipped (main.py:42-43), so no AWS
    credentials are required.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
