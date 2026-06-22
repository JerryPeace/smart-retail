"""AsyncOpenSearch client lifecycle — process-level lru_cache singleton.

Design trade-offs (design §9.1 / §11):
- Follows llm.py's @lru_cache pattern: construction is synchronous with no await point, so naturally race-free.
- AsyncOpenSearch construction doesn't open a network connection (lazy connect); building it at startup just readies the object.
- shutdown must explicitly await client.close() to release the aiohttp session, otherwise uvicorn shutdown
  emits an "Unclosed client session" warning; close_opensearch_client() handles this.
- Local security off: no auth, no TLS, aligning with the Phase 1 docker OpenSearch setup.
"""
from __future__ import annotations

from functools import lru_cache

from recommender.config import settings


@lru_cache(maxsize=1)
def get_opensearch_client():
    """Return the (cached) AsyncOpenSearch singleton.

    Construction doesn't open a network connection (lazy connect), so OpenSearch being offline doesn't block app startup.
    Within a process only one instance is built, sharing the aiohttp connection pool across requests.

    Returns:
        An opensearchpy.AsyncOpenSearch instance.
    """
    from opensearchpy import AsyncOpenSearch  # noqa: PLC0415 lazy import, following llm.py

    return AsyncOpenSearch(
        hosts=[settings.opensearch_host],
        use_ssl=False,
        verify_certs=False,
        ssl_show_warn=False,
    )


async def close_opensearch_client() -> None:
    """Called on lifespan shutdown: close the aiohttp session and clear the lru_cache.

    Only close if the cache has a value (avoids calling cache_info erroring when it was never constructed).
    After closing, cache_clear() ensures the next call to get_opensearch_client() gets a new instance.
    """
    if get_opensearch_client.cache_info().currsize > 0:
        client = get_opensearch_client()
        await client.close()
    get_opensearch_client.cache_clear()
