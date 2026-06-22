"""search router — the GET /search endpoint.

Design trade-offs (design §8.2 / §1.1 / §11):
- Only does parameter validation and calls the service; doesn't touch the repository / client, doesn't raise HTTPException.
- Unexpected errors (OpenSearch connection failures, etc.) propagate up to main.py's global Exception handler, converted to 500.
- No results is the service returning results=[] (HTTP 200), not handled in the router.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from recommender.deps import SearchServiceDep
from search_engine.schemas import SearchResponse

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search(
    service: SearchServiceDep,
    q: str = Query(min_length=1),
    size: int = Query(default=10, ge=1, le=100),
    bm25_weight: float | None = Query(default=None, ge=0.0, le=1.0),
):
    """Hybrid product search (k-NN + BM25 + min-max fusion).

    Args:
        q:           the search keyword (required, at least 1 character).
        size:        number of results to return (default 10, range 1–100).
        bm25_weight: manually specified BM25 weight (0–1); if omitted, goes through auto_route (if enabled) or the fixed default.

    Returns:
        A SearchResponse: the original query + product list + the actual weight applied / route label.
        No results returns HTTP 200 + results=[].
    """
    return await service.search(q, size=size, bm25_weight=bm25_weight)
