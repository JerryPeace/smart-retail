"""AsyncOpenSearch client 生命週期 — process 層級 lru_cache 單例。

設計取捨（design §9.1 / §11）：
- 比照 llm.py 的 @lru_cache 模式：建構同步無 await point，天然無 race。
- AsyncOpenSearch 建構不發網路連線（lazy connect），startup 建好只是把物件備妥。
- shutdown 須顯式 await client.close() 釋放 aiohttp session，否則 uvicorn 關閉會噴
  「Unclosed client session」warning；close_opensearch_client() 負責收斂這件事。
- 本地 security off：無認證、無 TLS，對齊 Phase 1 的 docker OpenSearch 設定。
"""
from __future__ import annotations

from functools import lru_cache

from recommender.config import settings


@lru_cache(maxsize=1)
def get_opensearch_client():
    """回 (快取的) AsyncOpenSearch 單例。

    建構時不發網路連線（lazy connect），OpenSearch 離線也不擋 app 啟動。
    process 內只建一個 instance，跨 request 共用 aiohttp connection pool。

    Returns:
        opensearchpy.AsyncOpenSearch 實例。
    """
    from opensearchpy import AsyncOpenSearch  # noqa: PLC0415 lazy import 比照 llm.py

    return AsyncOpenSearch(
        hosts=[settings.opensearch_host],
        use_ssl=False,
        verify_certs=False,
        ssl_show_warn=False,
    )


async def close_opensearch_client() -> None:
    """lifespan shutdown 呼叫：關閉 aiohttp session 並清 lru_cache。

    cache 有值才 close（避免在未曾建構的情況下呼叫 cache_info 出錯）。
    close 後 cache_clear()，確保下次呼叫 get_opensearch_client() 得到新 instance。
    """
    if get_opensearch_client.cache_info().currsize > 0:
        client = get_opensearch_client()
        await client.close()
    get_opensearch_client.cache_clear()
