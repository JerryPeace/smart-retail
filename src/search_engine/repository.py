"""search repository — DSL builder (pure functions) + hybrid_msearch I/O.

Design trade-offs (design §6 / §11):
- The DSL was lifted from scripts/etl/verify_search_os.py's knn_search / bm25_search and made async.
  knn: embedding field + vector/k; BM25: multi_match over martName/feature/keyword.
- Pure functions don't read settings (host/index injected via the constructor, avoiding the anti-pattern
  of the repo depending directly on global settings).
- Pure functions are separated from I/O: unit tests can assert on the dict structure directly, without starting OpenSearch.
- SearchRepository only does msearch I/O, no fusion, no DTO conversion (that's the service's responsibility).
- Any per-response error in msearch → raise immediately (fail fast); one-sided degradation is Phase 2b.
"""
from __future__ import annotations


class SearchRepository:
    """OpenSearch I/O — msearch calls.

    DSL construction is separated from I/O: build_knn_body / build_bm25_body are module-level pure functions,
    and this class is only responsible for assembling the body, issuing the msearch, and returning raw hits.

    Design highlights:
    - os_client and index are injected by deps.py; the repository does not read settings (anti-pattern).
    - The two-path query is issued as a single msearch (one round-trip, parallelized server-side).
    - Any per-response error → raise (fail fast, the global handler converts it to 500).
    """

    def __init__(self, os_client, index: str) -> None:
        """Initialize SearchRepository.

        Args:
            os_client: an AsyncOpenSearch instance (injected by deps.py).
            index:     the OpenSearch index name (passed in by deps.py as settings.opensearch_index).
        """
        self._client = os_client
        self._index = index

    async def hybrid_msearch(
        self,
        vector: list[float],
        query_text: str,
        k: int,
    ) -> tuple[list[dict], list[dict]]:
        """Run k-NN and BM25 concurrently in a single msearch, returning (knn_hits, bm25_hits) as two sets of raw hits.

        The body is an interleaved list of "header dict + query dict" (NDJSON semantics);
        opensearch-py accepts a list[dict] and serializes it automatically.

        Args:
            vector:     the query vector (length 1536, matching the index embedding field dimension).
            query_text: the query string (used by BM25).
            k:          take top-k per path.

        Returns:
            (knn_hits, bm25_hits), each an OpenSearch hits list (list[dict]).

        Raises:
            Exception: fail fast when any per-response contains an "error" key (the global handler converts it to 500).
        """
        body = [
            {"index": self._index},
            build_knn_body(vector, k),
            {"index": self._index},
            build_bm25_body(query_text, k),
        ]
        resp = await self._client.msearch(body=body)
        responses = resp["responses"]

        # Any side containing error → fail fast (degradation is a Phase 2b resilience feature, not done here)
        for i, r in enumerate(responses):
            if "error" in r:
                raise RuntimeError(
                    f"OpenSearch msearch 第 {i} 個子查詢回傳 error: {r['error']}"
                )

        knn_hits: list[dict] = responses[0]["hits"]["hits"]
        bm25_hits: list[dict] = responses[1]["hits"]["hits"]
        return knn_hits, bm25_hits


def build_knn_body(vector: list[float], k: int) -> dict:
    """Build the k-NN vector search query body.

    Aligns with the DSL structure of scripts/etl/verify_search_os.py's knn_search.
    The knn field name is `embedding` (the field name used during Phase 1 embedding, must not change).

    Args:
        vector: the query vector (length must match the index embedding field dimension, 1536).
        k:      take top-k.

    Returns:
        An OpenSearch search body dict.
    """
    return {
        "size": k,
        "query": {
            "knn": {
                "embedding": {
                    "vector": vector,
                    "k": k,
                }
            }
        },
    }


def build_bm25_body(query_text: str, k: int) -> dict:
    """Build the BM25 multi_match search query body.

    Aligns with the DSL structure of scripts/etl/verify_search_os.py's bm25_search.
    Search fields: martName / feature / keyword (the text fields from Phase 1 indexing).

    Args:
        query_text: the query string.
        k:          take top-k.

    Returns:
        An OpenSearch search body dict.
    """
    return {
        "size": k,
        "query": {
            "multi_match": {
                "query": query_text,
                "fields": ["martName", "feature", "keyword"],
            }
        },
    }
