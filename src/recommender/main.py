"""FastAPI app entry point"""
from pathlib import Path

from dotenv import load_dotenv

# Inject .env.local into os.environ — boto3 / langchain_aws only read from os.environ,
# while pydantic-settings only reads .env.local into the Settings object and does not export it.
# Must run before importing any boto3/langchain package.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env.local", override=True)

import logging  # noqa: E402
from collections.abc import Callable  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from recommender.api import evaluations, health, pipelines, recommendations  # noqa: E402
from recommender.config import settings  # noqa: E402
from recommender.db import init_db  # noqa: E402
from recommender.errors import NotFoundError  # noqa: E402
from recommender.llm import get_bedrock_llm  # noqa: E402
from search_engine import router as search  # noqa: E402
from search_engine.client import close_opensearch_client, get_opensearch_client  # noqa: E402
from search_engine.embeddings import get_bedrock_embeddings  # noqa: E402

# Apply the configured log level (settings.log_level <- LOG_LEVEL env, default INFO).
# Without this, LOG_LEVEL had no effect. Set LOG_LEVEL=DEBUG in .env.local to see more.
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # === Startup ===
    await init_db()
    _preheat(
        lambda: get_bedrock_llm(
            model=settings.bedrock_model_id,
            region=settings.bedrock_region,
            temperature=0.3,
            max_tokens=4096,
        ),
        "Bedrock LLM client",
    )
    get_opensearch_client()  # build the AsyncOpenSearch object (lazy connect, does not block startup)
    _preheat(
        lambda: get_bedrock_embeddings(
            model_id=settings.bedrock_embed_model_id,
            region=settings.bedrock_embed_region,
            profile=settings.aws_profile,
            dimensions=settings.embed_dimensions,
        ),
        "BedrockEmbeddings client",
    )
    yield
    # === Shutdown ===
    await close_opensearch_client()  # release the aiohttp session to avoid the unclosed session warning


def _preheat(fn: Callable[[], object], label: str) -> None:
    """Lifespan preheat: build the cached client up front (best-effort).

    Skipped in mock mode (no external services needed, and avoids errors on a dev machine without AWS credentials).
    On failure it only logs a warning and does not block app startup — the actual call will retry later.

    Args:
        fn:    a no-argument callable that, when called, builds and caches the client.
        label: the service name string used in log messages.
    """
    if settings.analyzer_mock_mode:
        return
    try:
        fn()
        logger.info("%s 預熱完成", label)
    except Exception:
        logger.warning("%s 預熱失敗 (啟動續行,首次呼叫時重試)", label, exc_info=True)


app = FastAPI(
    title="Marketing Cleaner POC",
    description="S3 ETL + Bedrock LLM 分析 + HubSpot 同步",
    version="0.1.0",
    lifespan=lifespan,
)

# === CORS (for local development) ===
# Lets the local static search UI (ui/search.html, openable via file:// or any port) fetch /search directly.
# POC local-only: allow_origins=["*"] is for development only; production must narrow this to a whitelist.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# === Global exception handlers (FastAPI native, so routers don't each need their own try/except) ===
@app.exception_handler(NotFoundError)
async def _not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    # domain "resource not found" → 404. The message only contains the resource/id, so it's safe to return to the client.
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Unexpected error: the full traceback only goes to the log, the client gets a generic message (no internal details leaked).
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(health.router)
app.include_router(pipelines.router)
app.include_router(recommendations.router)
app.include_router(evaluations.router)
app.include_router(search.router)
