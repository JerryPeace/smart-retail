"""Cohere Embed v4 query embedder — process-level cached boto3 client, shared across requests.

Design trade-offs:
- The query side uses Cohere Embed v4 (cohere.embed-v4:0), `input_type=search_query`.
  Cohere's asymmetric encoding (search_query for queries, search_document for documents) is key to its
  retrieval quality; for the doc side see scripts/etl/embed_products_os.py.
- The returned vector is **L2-normalized** → consistent with the doc side. When both doc and query are
  unit vectors, the index's innerproduct metric is equivalent to cosine; if either side is not normalized,
  the two vectors live in different spaces and the k-NN scores are silently all wrong, so this cannot be skipped.
- @lru_cache caches the embedder (boto3 client construction is synchronous with no await, so naturally
  race-free); aembed_query uses asyncio.to_thread to offload the synchronous boto3 call to a thread pool,
  not blocking the event loop.
- MOCK_QUERY_VECTOR is a 1536-dim unit vector: the mock path makes zero Bedrock calls and is deterministic
  (verifying the pipeline, not semantics, so a fixed vector suffices), with the same dimension as output_dimension.
- The sole consumer is search_engine.service.
"""
from __future__ import annotations

import asyncio
import json
import math
from functools import lru_cache

# 1536-dim unit vector ([1, 0, …]); used by the mock path, valid for innerproduct and deterministic.
MOCK_QUERY_VECTOR: list[float] = [1.0] + [0.0] * 1535


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2-normalize into a unit vector (consistent with the doc side's embed_products_os._l2_normalize)."""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class _CohereQueryEmbedder:
    """Cohere Embed v4 query-side embedder (direct boto3 call, input_type=search_query)."""

    def __init__(
        self, model_id: str, region: str, profile: str | None, dimensions: int
    ) -> None:
        import boto3  # noqa: PLC0415 lazy import (following llm.py)

        self._model_id = model_id
        self._dimensions = dimensions
        session = boto3.Session(profile_name=profile, region_name=region)
        self._client = session.client("bedrock-runtime")

    def _embed_sync(self, query: str) -> list[float]:
        body = json.dumps(
            {
                "texts": [query],
                "input_type": "search_query",
                "embedding_types": ["float"],
                "output_dimension": self._dimensions,
                "truncate": "RIGHT",  # Bedrock's truncation value (not Cohere's native END); queries are short, used as a safeguard
            }
        )
        resp = self._client.invoke_model(
            modelId=self._model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(resp["body"].read())
        emb = result["embeddings"]
        vec = emb["float"][0] if isinstance(emb, dict) else emb[0]
        return _l2_normalize(vec)

    async def aembed_query(self, query: str) -> list[float]:
        """Non-blocking embed: offload the synchronous boto3 call to the executor, return an L2-normalized vector."""
        return await asyncio.to_thread(self._embed_sync, query)


@lru_cache(maxsize=4)
def get_bedrock_embeddings(
    model_id: str,
    region: str,
    profile: str | None,
    dimensions: int,
) -> _CohereQueryEmbedder:
    """Return the (cached) Cohere query embedder.

    All parameters are hashable, serving as the lru_cache key. Within a process, the same parameters build the boto3 client only once.

    Args:
        model_id:   the Bedrock embedding model ID (cohere.embed-v4:0). Must be the same model as the doc side.
        region:     the AWS region (Cohere v4 is in ap-northeast-1).
        profile:    the AWS credentials profile name (lab); None uses default credentials.
        dimensions: output_dimension, must be the same as the doc side / index mapping (1536).

    Returns:
        A _CohereQueryEmbedder (whose aembed_query can be awaited).
    """
    return _CohereQueryEmbedder(model_id, region, profile, dimensions)
