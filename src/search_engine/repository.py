"""search repository — DSL builder（純函式）+ hybrid_msearch I/O。

設計取捨（design §6 / §11）：
- DSL 從 scripts/etl/verify_search_os.py 的 knn_search / bm25_search lift 進來改 async。
  knn：embedding 欄 + vector/k；BM25：multi_match 打 martName/feature/keyword。
- 純函式不讀 settings（host/index 由建構子注入，避免 repo 直接依賴全域 settings 反模式）。
- 純函式與 I/O 分離：單元測試可直接斷言 dict 結構，不需啟動 OpenSearch。
- SearchRepository 只做 msearch I/O，不做融合、不做 DTO 轉換（那是 service 的職責）。
- msearch 任一 per-response error → 直接 raise（fail fast）；單邊降級是 Phase 2b。
"""
from __future__ import annotations


class SearchRepository:
    """OpenSearch I/O — msearch 呼叫。

    DSL 建構與 I/O 分離：build_knn_body / build_bm25_body 為 module-level 純函式，
    此 class 只負責組 body、發 msearch、回 raw hits。

    設計重點：
    - os_client 與 index 由 deps.py 注入，repository 不讀 settings（反模式）。
    - 兩路查詢以單次 msearch 發出（一次 round-trip，server 端並行）。
    - 任一 per-response error → raise（fail fast，全域 handler 轉 500）。
    """

    def __init__(self, os_client, index: str) -> None:
        """初始化 SearchRepository。

        Args:
            os_client: AsyncOpenSearch 實例（由 deps.py 注入）。
            index:     OpenSearch 索引名稱（由 deps.py 傳入 settings.opensearch_index）。
        """
        self._client = os_client
        self._index = index

    async def hybrid_msearch(
        self,
        vector: list[float],
        query_text: str,
        k: int,
    ) -> tuple[list[dict], list[dict]]:
        """一次 msearch 併發 k-NN 與 BM25，回 (knn_hits, bm25_hits) 兩組 raw hits。

        body 為「header dict + query dict」交錯清單（NDJSON 語意），
        opensearch-py 接受 list[dict] 自動序列化。

        Args:
            vector:     查詢向量（長度 1536，與索引 embedding 欄維度一致）。
            query_text: 查詢字串（BM25 用）。
            k:          每路取 top-k 筆。

        Returns:
            (knn_hits, bm25_hits) 各為 OpenSearch hits list（list[dict]）。

        Raises:
            Exception: 任一 per-response 含 "error" key 時 fail fast（全域 handler 轉 500）。
        """
        body = [
            {"index": self._index},
            build_knn_body(vector, k),
            {"index": self._index},
            build_bm25_body(query_text, k),
        ]
        resp = await self._client.msearch(body=body)
        responses = resp["responses"]

        # 任一邊含 error → fail fast（降級是 Phase 2b 韌性功能，本次不做）
        for i, r in enumerate(responses):
            if "error" in r:
                raise RuntimeError(
                    f"OpenSearch msearch 第 {i} 個子查詢回傳 error: {r['error']}"
                )

        knn_hits: list[dict] = responses[0]["hits"]["hits"]
        bm25_hits: list[dict] = responses[1]["hits"]["hits"]
        return knn_hits, bm25_hits


def build_knn_body(vector: list[float], k: int) -> dict:
    """建立 k-NN 向量搜尋 query body。

    對齊 scripts/etl/verify_search_os.py knn_search 的 DSL 結構。
    knn 欄位名稱為 `embedding`（Phase 1 嵌入時的欄位名，不可更改）。

    Args:
        vector: 查詢向量（長度必須與索引 embedding 欄維度一致，1536）。
        k:      取 top-k 筆。

    Returns:
        OpenSearch search body dict。
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
    """建立 BM25 multi_match 搜尋 query body。

    對齊 scripts/etl/verify_search_os.py bm25_search 的 DSL 結構。
    搜尋欄位：martName / feature / keyword（Phase 1 索引時的文字欄位）。

    Args:
        query_text: 查詢字串。
        k:          取 top-k 筆。

    Returns:
        OpenSearch search body dict。
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
