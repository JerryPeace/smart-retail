"""SearchService — orchestrates embed → msearch → min-max score fusion → DTO.

Design trade-offs (design §8.1 / §5 / §11):
- The mock decision point is in the service (aligning with the existing AgentService pattern:
  __init__ reads settings.analyzer_mock_mode).
- mock mode: _embed_query returns MOCK_QUERY_VECTOR directly, zero Bedrock calls, zero credential needs.
- Real embedding goes through the Cohere v4 query embedder's aembed_query (embeddings.py, which
  wraps the synchronous boto3 call with asyncio.to_thread under the hood), rather than running a
  synchronous embed directly on the event loop (blocking the event loop would affect all in-flight requests).
- Candidate window candidate_k = settings.search_candidate_multiplier × size: gives fusion a wider per-side window, default multiplier 2.
- Fusion strategy: min-max score fusion (w_bm25=settings.search_bm25_weight, currently 0.2).
  After switching to Cohere v4, retuned from the Titan-era 0.7——the vector leg is clean, so the
  optimal weight shifts toward the vector side (see config comments).
  reciprocal_rank_fusion remains in fusion.py (not deleted, still covered by its unit tests); the service no longer uses it.
- No results returns results=[], HTTP 200: "search found nothing" is a normal business result, not an error.
- The service returns a Pydantic DTO (SearchResponse), not raw hit dicts.
"""
from __future__ import annotations

from recommender.config import settings
from search_engine.embeddings import MOCK_QUERY_VECTOR, get_bedrock_embeddings
from search_engine.fusion import min_max_score_fusion
from search_engine.repository import SearchRepository
from search_engine.schemas import SearchResponse, SearchResultItem


class SearchService:
    """Orchestrates the full search chain: embed → msearch → min-max score fusion → DTO.

    Responsibility boundaries:
    - Reads settings.analyzer_mock_mode and settings.search_bm25_weight (reading config in the service layer is legal).
    - Calls repo.hybrid_msearch to get the two sets of raw hits (with _score).
    - Fuses the ranking with min_max_score_fusion (w_bm25 adjustable via settings).
    - Maps OpenSearch hits to SearchResultItem DTOs and returns a SearchResponse.
    - Does not build DSL, touch HTTP, or raise HTTPException.
    """

    def __init__(self, repo: SearchRepository) -> None:
        """Initialize SearchService.

        Args:
            repo: a SearchRepository instance (injected by deps.py).
        """
        self._repo = repo
        self.mock_mode = settings.analyzer_mock_mode

    async def search(
        self, query: str, size: int = 10, bm25_weight: float | None = None
    ) -> SearchResponse:
        """Execute a hybrid search and return a SearchResponse.

        BM25 weight resolution priority (high → low):
        1. The bm25_weight explicit parameter (manual override, route_label="manual").
        2. settings.search_bm25_weight (fixed default).

        Args:
            query:       the query string.
            size:        max number of results to return (default 10, router limits 1–100).
            bm25_weight: manually specified BM25 weight (0–1); None uses the fixed default.

        Returns:
            A SearchResponse (with the original query, results, the actual weight applied, and the route label).
            No results returns results=[], does not raise.
        """
        vector = await self._embed_query(query)
        # Per-side candidate window: settings.search_candidate_multiplier × size.
        # The default multiplier 2 aligns with the offline investigation (minmax_b70_pool20 hit the best rel@10=79 at pool=20).
        candidate_k = settings.search_candidate_multiplier * size

        knn_hits, bm25_hits = await self._repo.hybrid_msearch(vector, query, candidate_k)

        # Extract (doc_id, raw_score) tuples for min-max score fusion
        # An OpenSearch hit itself carries _score (no need to change the repository signature)
        knn_scored = [(hit["_id"], float(hit.get("_score", 0.0))) for hit in knn_hits]
        bm25_scored = [(hit["_id"], float(hit.get("_score", 0.0))) for hit in bm25_hits]

        w_bm25, route_label = self._resolve_bm25_weight(bm25_weight)
        w_knn = 1.0 - w_bm25
        fused = min_max_score_fusion(knn_scored, bm25_scored, w_bm25=w_bm25, w_knn=w_knn)

        # Build an _id → _source map for joining metadata after fusion
        id_to_source: dict[str, dict] = {
            hit["_id"]: hit.get("_source", {}) for hit in knn_hits + bm25_hits
        }

        # Take top-size and map to SearchResultItem
        items: list[SearchResultItem] = []
        for doc_id, fusion_score in fused[:size]:
            source = id_to_source.get(doc_id, {})
            items.append(
                SearchResultItem(
                    mart_id=doc_id,
                    mart_name=source.get("martName", ""),
                    score=fusion_score,
                    brand=source.get("brand") or None,
                    price=source.get("price"),  # Don't use `or None`: price=0.0 is a legal value that would be wiped out
                    category=source.get("categoryLevel1Name") or None,  # the index field is categoryLevel1Name
                )
            )

        return SearchResponse(
            query=query,
            results=items,
            applied_bm25_weight=w_bm25,
            route_label=route_label,
        )

    def _resolve_bm25_weight(
        self, bm25_weight: float | None
    ) -> tuple[float, str | None]:
        """Decide this fusion's BM25 weight (manual override > fixed default).

        Returns:
            (w_bm25, route_label). route_label is "manual" (manual override) / None (fixed default).
        """
        if bm25_weight is not None:
            return bm25_weight, "manual"
        return settings.search_bm25_weight, None

    async def _embed_query(self, query: str) -> list[float]:
        """Convert the query string into a vector.

        mock mode: returns MOCK_QUERY_VECTOR directly (zero Bedrock calls).
        real mode: calls the Cohere Embed v4 query embedder (async, does not block the event loop).

        Returns:
            A 1536-dim L2-normalized float vector (same dimension as the doc side / index mapping).
        """
        if self.mock_mode:
            return MOCK_QUERY_VECTOR

        embed = get_bedrock_embeddings(
            model_id=settings.bedrock_embed_model_id,
            region=settings.bedrock_embed_region,
            profile=settings.aws_profile,
            dimensions=settings.embed_dimensions,
        )
        # aembed_query: wraps the synchronous boto3 Cohere call with asyncio.to_thread, does not block the event loop.
        return await embed.aembed_query(query)
