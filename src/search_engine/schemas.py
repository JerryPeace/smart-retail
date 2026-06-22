"""search domain DTOs — the "allowlist" fields the router returns to the client.

Responsibility: define the two Pydantic schemas SearchResultItem and SearchResponse.
Does not hold the OpenSearch raw hit structure (that's the repository's internal concern).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SearchResultItem(BaseModel):
    """A single product search result.

    score is the RRF fusion score (not the OpenSearch _score; the two paths' _score have different scales and aren't comparable).
    brand / price / category are optional fields (may be missing from the index).
    """

    model_config = ConfigDict(from_attributes=True)

    mart_id: str
    mart_name: str
    score: float
    brand: str | None = None
    price: float | None = None
    category: str | None = None


class SearchResponse(BaseModel):
    """The search endpoint's return structure.

    query: the original query string (returned as-is, for the client's convenience in cross-referencing).
    results: the product list sorted by RRF fusion score descending; no results returns an empty list (HTTP 200).
    """

    query: str
    results: list[SearchResultItem]
    # Routing observation fields (filled on auto_route or manual override):
    # applied_bm25_weight = the BM25 weight actually used in this fusion; route_label = the routing result or "manual".
    # Default None (not filled when auto_route is off and there's no manual override, backward compatible).
    applied_bm25_weight: float | None = None
    route_label: str | None = None
