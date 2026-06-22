"""FastAPI app entry point"""
from pathlib import Path

from dotenv import load_dotenv

# 把 .env.local 注入 os.environ — boto3 / langchain_aws 只認 os.environ,
# pydantic-settings 只把 .env.local 讀進 Settings 物件不會 export。
# 必須在 import 任何 boto3/langchain 套件之前執行。
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
    get_opensearch_client()  # 建 AsyncOpenSearch 物件（lazy connect，不阻塞啟動）
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
    await close_opensearch_client()  # 釋放 aiohttp session，避免 unclosed session warning


def _preheat(fn: Callable[[], object], label: str) -> None:
    """Lifespan 預熱：把 cached client 先建起來（best-effort）。

    mock 模式跳過（不需外部服務，也避免在沒 AWS 憑證的開發機噴錯）。
    失敗只 log warning，不擋 app 啟動 —— 真正呼叫時還會再試。

    Args:
        fn:    無引數 callable，呼叫後建立並快取 client。
        label: 用於 log 訊息的服務名稱字串。
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

# === CORS（本機開發用）===
# 讓本機靜態搜尋 UI（ui/search.html，可經 file:// 或任意埠開啟）能直接 fetch /search。
# POC 本機限定：allow_origins=["*"] 僅供開發，正式上線須收斂白名單。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# === 全域 exception handlers (FastAPI 原生,讓 router 不必各自 try/except) ===
@app.exception_handler(NotFoundError)
async def _not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    # domain「查無資源」→ 404。訊息只含資源/id,安全可回 client。
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # 未預期錯誤:完整 traceback 只進 log,client 拿通用訊息 (不洩內部細節)。
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(health.router)
app.include_router(pipelines.router)
app.include_router(recommendations.router)
app.include_router(evaluations.router)
app.include_router(search.router)
