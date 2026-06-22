"""Cohere Embed v4 query embedder — process 層級快取 boto3 client，跨 request 共用。

設計取捨：
- query 端用 Cohere Embed v4（cohere.embed-v4:0），`input_type=search_query`。
  Cohere 的不對稱編碼（query 用 search_query、document 用 search_document）是其檢索品質
  的關鍵；doc 端見 scripts/etl/embed_products_os.py。
- 回傳向量做 **L2 正規化** → 與 doc 端一致。doc/query 兩端皆單位向量時，索引的 innerproduct
  度量等價 cosine；任一端不正規化就是兩向量活在不同空間、k-NN 分數靜默全錯，故不可省。
- @lru_cache 快取 embedder（boto3 client 建構同步無 await，天然無 race）；aembed_query 以
  asyncio.to_thread 把同步 boto3 呼叫丟執行緒池，不阻塞 event loop。
- MOCK_QUERY_VECTOR 為 1536 維單位向量：mock 路徑零 Bedrock 呼叫、deterministic（驗管線
  不驗語意，固定向量即可），維度與 output_dimension 一致。
- 唯一消費者是 search_engine.service。
"""
from __future__ import annotations

import asyncio
import json
import math
from functools import lru_cache

# 1536 維單位向量（[1, 0, …]）；mock 路徑用，innerproduct 合法且 deterministic。
MOCK_QUERY_VECTOR: list[float] = [1.0] + [0.0] * 1535


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2 正規化成單位向量（與 doc 端 embed_products_os._l2_normalize 一致）。"""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class _CohereQueryEmbedder:
    """Cohere Embed v4 query 端嵌入器（boto3 直呼，input_type=search_query）。"""

    def __init__(
        self, model_id: str, region: str, profile: str | None, dimensions: int
    ) -> None:
        import boto3  # noqa: PLC0415 lazy import（比照 llm.py）

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
                "truncate": "RIGHT",  # Bedrock 截尾值（非 Cohere 原生的 END）；query 短，保險用
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
        """非阻塞嵌入：同步 boto3 呼叫丟 executor，回 L2 正規化向量。"""
        return await asyncio.to_thread(self._embed_sync, query)


@lru_cache(maxsize=4)
def get_bedrock_embeddings(
    model_id: str,
    region: str,
    profile: str | None,
    dimensions: int,
) -> _CohereQueryEmbedder:
    """回 (快取的) Cohere query embedder。

    參數全為 hashable，作為 lru_cache key。process 內同參數只建一次 boto3 client。

    Args:
        model_id:   Bedrock embedding 模型 ID（cohere.embed-v4:0）。必須與 doc 端同模型。
        region:     AWS region（Cohere v4 在 ap-northeast-1）。
        profile:    AWS credentials profile name（lab）；None 用 default credentials。
        dimensions: output_dimension，必須與 doc 端 / 索引 mapping 相同（1536）。

    Returns:
        _CohereQueryEmbedder（可 await aembed_query）。
    """
    return _CohereQueryEmbedder(model_id, region, profile, dimensions)
