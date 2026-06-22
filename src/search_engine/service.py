"""SearchService — embed → msearch → min-max score fusion → DTO 編排。

設計取捨（design §8.1 / §5 / §11）：
- mock 判斷點在 service（對齊 AgentService 既有模式：__init__ 讀 settings.analyzer_mock_mode）。
- mock mode：_embed_query 直接回 MOCK_QUERY_VECTOR，零 Bedrock 呼叫、零憑證需求。
- 真 embedding 走 Cohere v4 query embedder 的 aembed_query（embeddings.py，底層以
  asyncio.to_thread 包同步 boto3 呼叫），不在 event loop 直接跑同步 embed（阻塞 event
  loop 影響全部 in-flight request）。
- 候選窗 candidate_k = settings.search_candidate_multiplier × size：給融合更寬的單邊視窗，預設倍數 2。
- 融合策略：min-max score fusion（w_bm25=settings.search_bm25_weight，現行 0.2）。
  換 Cohere v4 後從 Titan 時代 0.7 重調——向量腿乾淨、最優權重往向量側移（見 config 註解）。
  reciprocal_rank_fusion 保留在 fusion.py（不刪，仍被其單元測試覆蓋），service 不再使用。
- 查無結果回 results=[]、HTTP 200：「搜尋沒中」是正常業務結果不是錯誤。
- service 回 Pydantic DTO（SearchResponse），不回 raw hit dict。
"""
from __future__ import annotations

from recommender.config import settings
from search_engine.embeddings import MOCK_QUERY_VECTOR, get_bedrock_embeddings
from search_engine.fusion import min_max_score_fusion
from search_engine.repository import SearchRepository
from search_engine.schemas import SearchResponse, SearchResultItem


class SearchService:
    """編排 search 全鏈路：embed → msearch → min-max score fusion → DTO。

    職責邊界：
    - 讀 settings.analyzer_mock_mode 與 settings.search_bm25_weight（service 層讀 config 合法）。
    - 呼叫 repo.hybrid_msearch 取兩組 raw hits（含 _score）。
    - 以 min_max_score_fusion 融合排名（w_bm25 可由 settings 調整）。
    - 將 OpenSearch hit 映射為 SearchResultItem DTO，回 SearchResponse。
    - 不組 DSL、不碰 HTTP、不拋 HTTPException。
    """

    def __init__(self, repo: SearchRepository) -> None:
        """初始化 SearchService。

        Args:
            repo: SearchRepository 實例（由 deps.py 注入）。
        """
        self._repo = repo
        self.mock_mode = settings.analyzer_mock_mode

    async def search(
        self, query: str, size: int = 10, bm25_weight: float | None = None
    ) -> SearchResponse:
        """執行 hybrid 搜尋並回 SearchResponse。

        BM25 權重解析優先序（高 → 低）：
        1. bm25_weight 顯式參數（手動覆寫，route_label="manual"）。
        2. settings.search_bm25_weight（固定預設）。

        Args:
            query:       查詢字串。
            size:        回傳筆數上限（預設 10，router 限制 1–100）。
            bm25_weight: 手動指定 BM25 權重（0–1）；None 則用固定預設。

        Returns:
            SearchResponse（含 query 原文、results、實際採用權重與判型標籤）。
            查無結果回 results=[]，不拋例外。
        """
        vector = await self._embed_query(query)
        # 每邊候選窗：settings.search_candidate_multiplier × size。
        # 預設倍數 2 對齊離線調查（minmax_b70_pool20 在 pool=20 達 rel@10=79 最佳）。
        candidate_k = settings.search_candidate_multiplier * size

        knn_hits, bm25_hits = await self._repo.hybrid_msearch(vector, query, candidate_k)

        # 取 (doc_id, raw_score) tuple 供 min-max score fusion
        # OpenSearch hit 本身帶 _score（不改 repository 簽名）
        knn_scored = [(hit["_id"], float(hit.get("_score", 0.0))) for hit in knn_hits]
        bm25_scored = [(hit["_id"], float(hit.get("_score", 0.0))) for hit in bm25_hits]

        w_bm25, route_label = self._resolve_bm25_weight(bm25_weight)
        w_knn = 1.0 - w_bm25
        fused = min_max_score_fusion(knn_scored, bm25_scored, w_bm25=w_bm25, w_knn=w_knn)

        # 建 _id → _source map，供融合後 join metadata
        id_to_source: dict[str, dict] = {
            hit["_id"]: hit.get("_source", {}) for hit in knn_hits + bm25_hits
        }

        # 取 top-size，映射為 SearchResultItem
        items: list[SearchResultItem] = []
        for doc_id, fusion_score in fused[:size]:
            source = id_to_source.get(doc_id, {})
            items.append(
                SearchResultItem(
                    mart_id=doc_id,
                    mart_name=source.get("martName", ""),
                    score=fusion_score,
                    brand=source.get("brand") or None,
                    price=source.get("price"),  # 不可用 `or None`:price=0.0 是合法值會被抹掉
                    category=source.get("categoryLevel1Name") or None,  # index 欄位是 categoryLevel1Name
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
        """決定本次融合的 BM25 權重（手動覆寫 > 固定預設）。

        Returns:
            (w_bm25, route_label)。route_label 為 "manual"（手動覆寫）/ None（固定預設）。
        """
        if bm25_weight is not None:
            return bm25_weight, "manual"
        return settings.search_bm25_weight, None

    async def _embed_query(self, query: str) -> list[float]:
        """將查詢字串轉換為向量。

        mock mode：直接回 MOCK_QUERY_VECTOR（零 Bedrock 呼叫）。
        真實 mode：呼叫 Cohere Embed v4 query embedder（async，不阻塞 event loop）。

        Returns:
            1536 維 L2 正規化 float 向量（與 doc 端 / 索引 mapping 同維度）。
        """
        if self.mock_mode:
            return MOCK_QUERY_VECTOR

        embed = get_bedrock_embeddings(
            model_id=settings.bedrock_embed_model_id,
            region=settings.bedrock_embed_region,
            profile=settings.aws_profile,
            dimensions=settings.embed_dimensions,
        )
        # aembed_query：以 asyncio.to_thread 包同步 boto3 Cohere 呼叫，不阻塞 event loop。
        return await embed.aembed_query(query)
