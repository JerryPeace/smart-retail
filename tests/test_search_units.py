"""search 模組純函式單元測試 — Phase 5（Task 5.1–5.4）+ min-max fusion.

涵蓋範圍：
  5.1  fusion.py         : reciprocal_rank_fusion 純函式
  5.1b fusion.py         : min_max_score_fusion 純函式（w_bm25=0.7 策略）
  5.2  repository.py     : build_knn_body / build_bm25_body DSL builder
  5.3  embeddings.py     : MOCK_QUERY_VECTOR 不變量
  5.4  service.py        : SearchService 編排（fake repo 注入，零 OpenSearch / Bedrock）

設計原則（對齊既有測試慣例）：
  - 全部 🟢 無 docker / 無網路 / 零 Bedrock 呼叫
  - conftest 已設 ANALYZER_MOCK_MODE=true + settings.analyzer_mock_mode=True
  - async test 直接寫 async def（conftest asyncio_mode=auto）
  - fake repo 以 duck-typing 最小 class 實作，不引入 unittest.mock
"""
from __future__ import annotations

import math

# ---------- 5.1 RRF 純函式 ----------
from search_engine.fusion import min_max_score_fusion, reciprocal_rank_fusion


class TestRRF:
    """Task 5.1 — reciprocal_rank_fusion 純函式。"""

    def test_dual_list_b_score_and_rank(self):
        """b 同時出現在兩清單，score == 1/61 + 1/62，且排第一（最高分）。

        清單 A: ["a", "b"]  → a rank=1, b rank=2
        清單 B: ["b", "c"]  → b rank=1, c rank=2
        b score = 1/(60+2) + 1/(60+1) = 1/62 + 1/61
        a score = 1/(60+1) = 1/61
        c score = 1/(60+2) = 1/62
        """
        result = reciprocal_rank_fusion([["a", "b"], ["b", "c"]])
        id_to_score = dict(result)

        expected_b = 1 / 62 + 1 / 61
        assert math.isclose(id_to_score["b"], expected_b, rel_tol=1e-9), (
            f"b score {id_to_score['b']} != 1/62 + 1/61 = {expected_b}"
        )

        # b 排第一
        assert result[0][0] == "b", f"b 應排第一，實際排名：{[x[0] for x in result]}"

    def test_k_parameter_changes_scores(self):
        """較小 k 讓高排名文件優勢更明顯（分數變大）；較大 k 分數更扁平。

        以同一清單、不同 k 比較 rank=1 的分數：k↓ → score(rank1)↑。
        """
        docs = ["x", "y", "z"]
        result_k10 = reciprocal_rank_fusion([docs], k=10)
        result_k60 = reciprocal_rank_fusion([docs], k=60)

        score_k10 = dict(result_k10)["x"]
        score_k60 = dict(result_k60)["x"]

        assert score_k10 > score_k60, (
            f"k=10 時 rank1 分數應大於 k=60；得 {score_k10} vs {score_k60}"
        )

        # 比較公式：1/(10+1) > 1/(60+1)
        assert math.isclose(score_k10, 1 / 11, rel_tol=1e-9)
        assert math.isclose(score_k60, 1 / 61, rel_tol=1e-9)

    def test_empty_outer_list_returns_empty(self):
        """空外層清單（[]）回空 list，不拋例外。"""
        result = reciprocal_rank_fusion([])
        assert result == []

    def test_both_inner_empty_returns_empty(self):
        """兩個內層清單皆空（[[], []]）回空 list，不拋例外。"""
        result = reciprocal_rank_fusion([[], []])
        assert result == []

    def test_single_sided_empty_equals_single_rank(self):
        """一邊清單為空時，融合結果等同單路排名（只考慮非空那邊）。

        清單 A: ["x", "y"]   清單 B: []
        期望：x score=1/61, y score=1/62，順序 x > y。
        """
        result_with_empty = reciprocal_rank_fusion([["x", "y"], []])
        result_single = reciprocal_rank_fusion([["x", "y"]])

        assert result_with_empty == result_single, (
            f"一邊空應等同單路排名：{result_with_empty} != {result_single}"
        )

        ids = [doc_id for doc_id, _ in result_with_empty]
        assert ids == ["x", "y"]

    def test_tie_break_deterministic_same_input(self):
        """同輸入跑兩次結果完全相同（tie-break deterministic）。"""
        lists = [["p", "q", "r"], ["r", "q", "p"]]
        result1 = reciprocal_rank_fusion(lists)
        result2 = reciprocal_rank_fusion(lists)
        assert result1 == result2, "同輸入兩次結果不一致！"

    def test_tie_break_alphabetical_order(self):
        """同分情況下，tie-break 以 doc_id 字典序升序（a < b < c）。

        只有兩個清單、兩個 doc_id 同 rank，score 一定相同，
        驗證較小字典序 id 排前。
        """
        # a、b 各出現在相同排名：兩路 rank=1
        result = reciprocal_rank_fusion([["a"], ["b"]])
        # 兩者 score 相同（都是 1/(60+1)），字典序 a < b
        assert result[0][0] == "a", f"同分時 a 應排第一（字典序）：{result}"
        assert result[1][0] == "b"
        assert math.isclose(result[0][1], result[1][1], rel_tol=1e-12), (
            "a 與 b 應同分"
        )

    def test_single_list_ordering(self):
        """單一清單時 rank 1 的 score 最高，保持原序。"""
        result = reciprocal_rank_fusion([["first", "second", "third"]])
        ids = [doc_id for doc_id, _ in result]
        assert ids == ["first", "second", "third"]

        scores = [score for _, score in result]
        # 分數嚴格遞減
        assert scores[0] > scores[1] > scores[2]


# ---------- 5.1b min_max_score_fusion 純函式 ----------


class TestMinMaxScoreFusion:
    """Task 5.1b — min_max_score_fusion 純函式（對齊 investigate_hybrid_fusion.py 語意）。

    重點驗證：
    - per-path min-max 正規化正確性（兩路各自不同 score range 各自正規化）
    - 加權正確（w_bm25=0.7 時 BM25 高分 doc 排前）
    - 單邊缺漏（另一路貢獻 0）
    - 空輸入不拋例外
    - 全同分/單一元素正規化為 1.0
    - 同分 tie-break deterministic（doc_id 字典序）
    """

    def test_normalization_per_path(self):
        """每路各自做 min-max 正規化，兩路 score range 不同不互相影響。

        knn: [(a, 10.0), (b, 0.0)] → norm_a=1.0, norm_b=0.0
        bm25: [(a, 5.0), (c, 1.0)] → norm_a=1.0, norm_c=0.0

        w_knn=0.5, w_bm25=0.5:
          a: 0.5*1.0 + 0.5*1.0 = 1.0
          b: 0.5*0.0 + 0.5*0.0（不在 bm25）= 0.0
          c: 0.5*0.0（不在 knn）+ 0.5*0.0 = 0.0
        """
        knn = [("a", 10.0), ("b", 0.0)]
        bm25 = [("a", 5.0), ("c", 1.0)]
        result = min_max_score_fusion(knn, bm25, w_bm25=0.5, w_knn=0.5)
        id_to_score = dict(result)

        assert math.isclose(id_to_score["a"], 1.0, rel_tol=1e-9), f"a score={id_to_score['a']}"
        assert math.isclose(id_to_score["b"], 0.0, abs_tol=1e-9), f"b score={id_to_score['b']}"
        assert math.isclose(id_to_score["c"], 0.0, abs_tol=1e-9), f"c score={id_to_score['c']}"

    def test_bm25_weight_07_makes_bm25_high_score_rank_first(self):
        """w_bm25=0.7 時 BM25 高分 doc 優先於 knn-only high-rank doc。

        knn:  [(knn_top, 100.0), (shared, 50.0)]  → norm: knn_top=1.0, shared=0.5
        bm25: [(shared, 100.0), (bm25_top, 80.0)] → norm: shared=1.0, bm25_top=0.0

        wait — 讓 bm25_top score 比 shared 低讓 bm25_top 在 bm25 路排名靠後，
        但要讓 shared doc（同時在兩路中 bm25 高分）排前 knn_top（只在 knn）。

        knn:  [(knn_top, 10.0), (shared, 5.0)]  → norm: knn_top=1.0, shared=0.0
        bm25: [(shared, 10.0), (other, 0.0)]    → norm: shared=1.0, other=0.0

        w_knn=0.3, w_bm25=0.7:
          knn_top: 0.3*1.0 + 0.7*0.0 = 0.3
          shared:  0.3*0.0 + 0.7*1.0 = 0.7  ← BM25 高分贏
          other:   0.3*0.0 + 0.7*0.0 = 0.0
        """
        knn = [("knn_top", 10.0), ("shared", 5.0)]
        bm25 = [("shared", 10.0), ("other", 0.0)]
        result = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        ids = [doc_id for doc_id, _ in result]

        assert ids[0] == "shared", (
            f"BM25 高分 doc 應排第一，實際排名：{ids}"
        )
        id_to_score = dict(result)
        assert math.isclose(id_to_score["shared"], 0.7, rel_tol=1e-9)
        assert math.isclose(id_to_score["knn_top"], 0.3, rel_tol=1e-9)

    def test_single_side_missing_contributes_zero(self):
        """只有一路有值時，缺席路貢獻 0.0。

        knn:  [(a, 5.0), (b, 0.0)]  → norm: a=1.0, b=0.0
        bm25: []（空）
        w_knn=0.3, w_bm25=0.7:
          a: 0.3*1.0 = 0.3
          b: 0.3*0.0 = 0.0
        """
        knn = [("a", 5.0), ("b", 0.0)]
        result = min_max_score_fusion(knn, [], w_bm25=0.7, w_knn=0.3)
        id_to_score = dict(result)

        assert math.isclose(id_to_score["a"], 0.3, rel_tol=1e-9)
        assert math.isclose(id_to_score["b"], 0.0, abs_tol=1e-9)

    def test_both_empty_returns_empty(self):
        """兩路皆空時回空 list，不拋例外。"""
        result = min_max_score_fusion([], [], w_bm25=0.7, w_knn=0.3)
        assert result == []

    def test_single_element_per_path_normalizes_to_one(self):
        """每路只有一個元素（或全同分）時，正規化為 1.0（對齊 investigate 腳本）。

        knn:  [(a, 99.0)]  → hi==lo → norm_a=1.0
        bm25: [(b, 0.5)]   → hi==lo → norm_b=1.0
        w_knn=0.3, w_bm25=0.7:
          a: 0.3*1.0 = 0.3
          b: 0.7*1.0 = 0.7
        """
        result = min_max_score_fusion([("a", 99.0)], [("b", 0.5)], w_bm25=0.7, w_knn=0.3)
        id_to_score = dict(result)

        assert math.isclose(id_to_score["a"], 0.3, rel_tol=1e-9)
        assert math.isclose(id_to_score["b"], 0.7, rel_tol=1e-9)
        # b 排第一（0.7 > 0.3）
        assert result[0][0] == "b"

    def test_all_same_score_normalizes_to_one(self):
        """同路所有 doc 同分（hi==lo）時，各自正規化為 1.0。"""
        knn = [("x", 3.0), ("y", 3.0), ("z", 3.0)]
        result = min_max_score_fusion(knn, [], w_bm25=0.7, w_knn=0.3)
        id_to_score = dict(result)
        for doc_id in ("x", "y", "z"):
            assert math.isclose(id_to_score[doc_id], 0.3 * 1.0, rel_tol=1e-9)

    def test_tie_break_deterministic_same_input(self):
        """同輸入跑兩次結果完全相同（tie-break deterministic）。"""
        knn = [("p", 5.0), ("q", 3.0)]
        bm25 = [("q", 5.0), ("r", 1.0)]
        result1 = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        result2 = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        assert result1 == result2

    def test_tie_break_alphabetical_order(self):
        """同分時 doc_id 字典序升序 tie-break（a < b）。

        兩路各只有一個 doc（各自 norm=1.0），w_knn=w_bm25=0.5：
          knn: [(b, 10.0)]  → norm_b=1.0 → fused_b=0.5
          bm25: [(a, 10.0)] → norm_a=1.0 → fused_a=0.5
        a==b 同分，字典序 a < b → a 排前。
        """
        result = min_max_score_fusion([("b", 10.0)], [("a", 10.0)], w_bm25=0.5, w_knn=0.5)
        assert result[0][0] == "a", f"同分時 a 應排第一（字典序），實際：{result}"
        assert math.isclose(result[0][1], result[1][1], rel_tol=1e-9)

    def test_results_strictly_descending(self):
        """融合結果依分數嚴格降序排列。"""
        knn = [("a", 10.0), ("b", 5.0), ("c", 1.0)]
        bm25 = [("b", 10.0), ("c", 5.0), ("d", 1.0)]
        result = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        scores = [s for _, s in result]
        # 允許同分（tie-break 已確保 deterministic），只驗降序
        assert scores == sorted(scores, reverse=True)


# ---------- 5.2 DSL builder ----------

from search_engine.repository import build_bm25_body, build_knn_body


class TestBuildKnnBody:
    """Task 5.2 — build_knn_body DSL 結構驗證。"""

    def test_size_equals_k(self):
        body = build_knn_body(vector=[0.0] * 1024, k=20)
        assert body["size"] == 20

    def test_knn_embedding_vector_passthrough(self):
        """vector 原樣帶入 query.knn.embedding.vector，不做任何轉換。"""
        vec = [1.0] + [0.0] * 1023
        body = build_knn_body(vector=vec, k=10)
        assert body["query"]["knn"]["embedding"]["vector"] == vec

    def test_knn_k_in_body(self):
        """knn 子查詢的 k 欄位與 top-level size 一致。"""
        body = build_knn_body(vector=[0.5] * 10, k=7)
        assert body["query"]["knn"]["embedding"]["k"] == 7

    def test_structure_keys(self):
        """DSL 結構確認：size + query.knn.embedding 存在。"""
        body = build_knn_body(vector=[0.0], k=5)
        assert "size" in body
        assert "query" in body
        assert "knn" in body["query"]
        assert "embedding" in body["query"]["knn"]
        knn_emb = body["query"]["knn"]["embedding"]
        assert "vector" in knn_emb
        assert "k" in knn_emb


class TestBuildBm25Body:
    """Task 5.2 — build_bm25_body DSL 結構驗證。"""

    def test_multi_match_fields(self):
        """fields 必須是 ["martName", "feature", "keyword"]（Phase 1 索引欄位）。"""
        body = build_bm25_body(query_text="靈芝", k=10)
        fields = body["query"]["multi_match"]["fields"]
        assert fields == ["martName", "feature", "keyword"]

    def test_query_text_passthrough(self):
        body = build_bm25_body(query_text="掃地機器人", k=5)
        assert body["query"]["multi_match"]["query"] == "掃地機器人"

    def test_size_equals_k(self):
        body = build_bm25_body(query_text="test", k=15)
        assert body["size"] == 15

    def test_structure_keys(self):
        """DSL 結構確認：size + query.multi_match 存在。"""
        body = build_bm25_body(query_text="x", k=3)
        assert "size" in body
        assert "query" in body
        assert "multi_match" in body["query"]
        mm = body["query"]["multi_match"]
        assert "query" in mm
        assert "fields" in mm


# ---------- 5.3 mock 向量不變量 ----------

from search_engine.embeddings import MOCK_QUERY_VECTOR


class TestMockQueryVector:
    """Task 5.3 — MOCK_QUERY_VECTOR 長度與 L2 norm 不變量。"""

    def test_length_1536(self):
        assert len(MOCK_QUERY_VECTOR) == 1536, (
            f"長度應為 1536，得 {len(MOCK_QUERY_VECTOR)}"
        )

    def test_l2_norm_equals_one(self):
        """L2 norm == 1.0（單位向量，innerproduct 空間合法）。允許 1e-9 浮點誤差。"""
        norm_sq = sum(v * v for v in MOCK_QUERY_VECTOR)
        assert math.isclose(norm_sq, 1.0, abs_tol=1e-9), (
            f"L2 norm^2 應為 1.0，得 {norm_sq}"
        )

    def test_first_component_is_one(self):
        """[1.0] + [0.0]*1535 結構確認：第一個分量為 1.0。"""
        assert MOCK_QUERY_VECTOR[0] == 1.0

    def test_remaining_components_are_zero(self):
        """第 2–1536 個分量皆為 0.0（單位向量結構）。"""
        assert all(v == 0.0 for v in MOCK_QUERY_VECTOR[1:])


# ---------- 5.4 SearchService 編排（fake repo 注入）----------

from search_engine.schemas import SearchResponse, SearchResultItem
from search_engine.service import SearchService


def _make_hit(doc_id: str, mart_name: str, score: float = 1.0, **extra) -> dict:
    """建立 OpenSearch hit 格式（service 期望的結構）。

    service.py 從 hit["_id"] 取 doc_id，從 hit["_score"] 取 raw score，
    從 hit["_source"] 取 metadata。

    Args:
        doc_id:    OpenSearch _id。
        mart_name: 商品名稱（寫入 _source.martName）。
        score:     OpenSearch _score，預設 1.0（min-max fusion 用；同一路全同分時正規化為 1.0）。
        **extra:   其他 _source 欄位（brand、price、categoryLevel1Name 等）。
    """
    source = {"martName": mart_name}
    source.update(extra)
    return {"_id": doc_id, "_score": score, "_source": source}


class FakeSearchRepository:
    """Duck-typed fake repo — 可注入預製的 (knn_hits, bm25_hits)。

    SearchService 建構子期望 repo 有 hybrid_msearch async 方法。
    """

    def __init__(
        self, knn_hits: list[dict], bm25_hits: list[dict]
    ) -> None:
        self._knn_hits = knn_hits
        self._bm25_hits = bm25_hits
        self.call_count = 0  # 驗證只被呼叫一次

    async def hybrid_msearch(
        self, vector: list[float], query_text: str, k: int
    ) -> tuple[list[dict], list[dict]]:
        self.call_count += 1
        return self._knn_hits, self._bm25_hits


class TestSearchService:
    """Task 5.4 — SearchService 編排驗證（mock mode，fake repo 注入）。

    前提：conftest 已確保 settings.analyzer_mock_mode = True，
          _embed_query 會直接回 MOCK_QUERY_VECTOR，零 Bedrock 呼叫。
    """

    def _make_service(
        self,
        knn_hits: list[dict],
        bm25_hits: list[dict],
    ) -> tuple[SearchService, FakeSearchRepository]:
        repo = FakeSearchRepository(knn_hits, bm25_hits)
        svc = SearchService(repo=repo)
        return svc, repo

    # ------------------------------------------------------------------
    # 基礎回傳型別與結構
    # ------------------------------------------------------------------

    async def test_returns_search_response_type(self):
        svc, _ = self._make_service(
            knn_hits=[_make_hit("doc1", "商品A")],
            bm25_hits=[_make_hit("doc2", "商品B")],
        )
        result = await svc.search("test", size=10)
        assert isinstance(result, SearchResponse)

    async def test_result_items_are_search_result_item(self):
        svc, _ = self._make_service(
            knn_hits=[_make_hit("d1", "商品一")],
            bm25_hits=[],
        )
        result = await svc.search("x", size=5)
        for item in result.results:
            assert isinstance(item, SearchResultItem)

    # ------------------------------------------------------------------
    # 欄位映射：_id → mart_id，_source.martName → mart_name，score = RRF 分
    # ------------------------------------------------------------------

    async def test_field_mapping_mart_id(self):
        """_id 正確映射為 mart_id。"""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-001", "靈芝飲")],
            bm25_hits=[],
        )
        result = await svc.search("靈芝", size=5)
        assert result.results[0].mart_id == "sku-001"

    async def test_field_mapping_mart_name(self):
        """_source.martName 正確映射為 mart_name。"""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-002", "掃地機器人")],
            bm25_hits=[],
        )
        result = await svc.search("掃地", size=5)
        assert result.results[0].mart_name == "掃地機器人"

    async def test_field_mapping_score_is_fusion_score(self):
        """score 為 min-max fusion 分數（非 OpenSearch raw _score）。

        單路單 doc：norm=1.0（單一元素，hi==lo → 正規化為 1.0）。
        knn 路 w_knn = 1 - settings.search_bm25_weight = 0.3，bm25 路空。
        fused_score = 0.3 * 1.0 + 0.7 * 0.0 = 0.3。
        """
        from recommender.config import settings as _settings

        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-100", "測試品", score=5.0)],
            bm25_hits=[],
        )
        result = await svc.search("查詢", size=5)
        w_knn = 1.0 - _settings.search_bm25_weight
        expected_score = w_knn * 1.0  # 單路單 doc norm=1.0，bm25 空
        assert math.isclose(result.results[0].score, expected_score, rel_tol=1e-9), (
            f"融合分 {result.results[0].score} != 預期 {expected_score}"
        )

    async def test_field_mapping_optional_fields(self):
        """brand / price / category optional 欄位從 _source 正確映射。"""
        svc, _ = self._make_service(
            knn_hits=[
                _make_hit(
                    "sku-200", "iPhone",
                    brand="Apple", price=35900.0, categoryLevel1Name="通訊",
                )
            ],
            bm25_hits=[],
        )
        result = await svc.search("手機", size=5)
        item = result.results[0]
        assert item.brand == "Apple"
        assert item.price == 35900.0
        assert item.category == "通訊"  # index 欄位 categoryLevel1Name → category

    async def test_price_zero_is_preserved_not_none(self):
        """price=0.0 是合法值,不可被 `or None` 抹成 None(code review Bug 2)。"""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-free", "贈品", price=0.0)],
            bm25_hits=[],
        )
        result = await svc.search("贈品", size=5)
        assert result.results[0].price == 0.0

    async def test_missing_optional_fields_are_none(self):
        """_source 缺 brand / price / category 時，對應欄位為 None（不 crash）。"""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-300", "基礎商品")],  # 無 brand/price/category
            bm25_hits=[],
        )
        result = await svc.search("基礎", size=5)
        item = result.results[0]
        assert item.brand is None
        assert item.price is None
        assert item.category is None

    # ------------------------------------------------------------------
    # 融合排序：兩邊都出現的 doc 排名更前
    # ------------------------------------------------------------------

    async def test_fused_doc_ranks_first(self):
        """同時出現在 knn 與 bm25 的 doc，融合分最高應排第一。

        knn:  [("shared", 10.0), ("knn-only", 5.0)]
          → norm: shared=1.0, knn-only=0.0
        bm25: [("shared", 10.0), ("bm25-only", 5.0)]
          → norm: shared=1.0, bm25-only=0.0

        w_bm25=0.7, w_knn=0.3:
          shared:   0.3*1.0 + 0.7*1.0 = 1.0（最高）
          knn-only: 0.3*0.0 + 0.7*0.0 = 0.0
          bm25-only:0.3*0.0 + 0.7*0.0 = 0.0
        """
        svc, _ = self._make_service(
            knn_hits=[
                _make_hit("shared", "融合商品", score=10.0),
                _make_hit("knn-only", "向量商品", score=5.0),
            ],
            bm25_hits=[
                _make_hit("shared", "融合商品", score=10.0),
                _make_hit("bm25-only", "詞面商品", score=5.0),
            ],
        )
        result = await svc.search("查詢", size=10)
        assert result.results[0].mart_id == "shared", (
            f"融合 doc 應排第一，實際：{[r.mart_id for r in result.results]}"
        )
        # shared: w_knn*1.0 + w_bm25*1.0 = 1.0
        assert math.isclose(result.results[0].score, 1.0, rel_tol=1e-9)

    async def test_fusion_ordering_across_all_docs(self):
        """多 doc 融合後 score 降序，且 results 清單降序；兩路第一名 doc 排最前。

        knn:  [(a,10.0),(b,5.0),(c,1.0)]  → norm: a=1.0,b≈0.44,c=0.0
        bm25: [(a,10.0),(d,5.0),(e,1.0)]  → norm: a=1.0,d≈0.44,e=0.0

        a 在兩路都是最高分 → 融合分最高 → 排第一。
        """
        svc, _ = self._make_service(
            knn_hits=[
                _make_hit("a", "A", score=10.0),
                _make_hit("b", "B", score=5.0),
                _make_hit("c", "C", score=1.0),
            ],
            bm25_hits=[
                _make_hit("a", "A", score=10.0),  # a 出現在兩路最高分 → 最高 fused score
                _make_hit("d", "D", score=5.0),
                _make_hit("e", "E", score=1.0),
            ],
        )
        result = await svc.search("test", size=10)
        scores = [item.score for item in result.results]
        assert scores == sorted(scores, reverse=True), "results 應依 score 降序"
        assert result.results[0].mart_id == "a"

    # ------------------------------------------------------------------
    # top-size 截斷
    # ------------------------------------------------------------------

    async def test_top_size_truncation(self):
        """size=2 時最多回傳 2 筆，即使 hits 更多。"""
        knn_hits = [_make_hit(f"doc{i}", f"商品{i}") for i in range(5)]
        bm25_hits = []
        svc, _ = self._make_service(knn_hits, bm25_hits)
        result = await svc.search("query", size=2)
        assert len(result.results) == 2

    async def test_size_larger_than_hits_returns_all(self):
        """size 大於實際 hit 數時，回傳全部（不補空）。"""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("x", "X"), _make_hit("y", "Y")],
            bm25_hits=[],
        )
        result = await svc.search("test", size=50)
        assert len(result.results) == 2

    # ------------------------------------------------------------------
    # 兩邊皆空 → results=[] 不拋例外
    # ------------------------------------------------------------------

    async def test_both_empty_returns_empty_results(self):
        """knn 與 bm25 皆空時，回 results=[]，不拋例外。"""
        svc, _ = self._make_service(knn_hits=[], bm25_hits=[])
        result = await svc.search("不存在的查詢", size=10)
        assert result.results == []
        assert isinstance(result, SearchResponse)

    async def test_both_empty_query_preserved(self):
        """兩邊空時，回傳的 query 原文仍正確帶回。"""
        svc, _ = self._make_service(knn_hits=[], bm25_hits=[])
        result = await svc.search("特殊查詢詞", size=5)
        assert result.query == "特殊查詢詞"

    # ------------------------------------------------------------------
    # query 原文帶回
    # ------------------------------------------------------------------

    async def test_query_passthrough(self):
        """SearchResponse.query 等於傳入的查詢字串。"""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("d1", "商品D")],
            bm25_hits=[],
        )
        result = await svc.search("靈芝保健飲", size=5)
        assert result.query == "靈芝保健飲"

    # ------------------------------------------------------------------
    # mock mode 確認：無 Bedrock 呼叫（只 fake repo 被呼叫一次）
    # ------------------------------------------------------------------

    async def test_mock_mode_only_calls_fake_repo(self):
        """mock mode 下 _embed_query 回 MOCK_QUERY_VECTOR，fake repo 被呼叫恰好一次。"""
        svc, repo = self._make_service(
            knn_hits=[_make_hit("m1", "Mock商品")],
            bm25_hits=[],
        )
        # SearchService.__init__ 讀 settings.analyzer_mock_mode（conftest 已設 True）
        assert svc.mock_mode is True

        await svc.search("test", size=5)
        assert repo.call_count == 1, "hybrid_msearch 應被呼叫恰好一次"

    async def test_candidate_k_uses_multiplier(self):
        """候選窗 candidate_k = settings.search_candidate_multiplier * size。

        預設倍數 2（對齊離線調查 minmax_b70_pool20 最佳策略）；
        透過 fake repo 攔截呼叫時的 k 參數驗證。
        """
        from recommender.config import settings as _settings

        class CapturingRepo:
            """記錄 hybrid_msearch 被呼叫時的 k 參數。"""
            captured_k: int | None = None

            async def hybrid_msearch(self, vector, query_text, k):
                self.captured_k = k
                return [], []

        repo = CapturingRepo()
        svc = SearchService(repo=repo)  # type: ignore[arg-type]

        size = 7
        await svc.search("test", size=size)
        expected_k = _settings.search_candidate_multiplier * size
        assert repo.captured_k == expected_k, (
            f"candidate_k 應為 {_settings.search_candidate_multiplier}×size={expected_k}，"
            f"得 {repo.captured_k}"
        )


# ---------- 5.5 SearchService 權重解析（fake repo）----------

from recommender.config import settings


class TestSearchServiceWeight:
    """權重解析優先序：手動覆寫 > 固定預設（auto_route 已移除）。"""

    def _svc(self) -> SearchService:
        repo = FakeSearchRepository(
            knn_hits=[_make_hit("d1", "商品A")],
            bm25_hits=[_make_hit("d2", "商品B")],
        )
        return SearchService(repo=repo)

    async def test_manual_override_wins(self):
        svc = self._svc()
        r = await svc.search("任意", size=5, bm25_weight=0.9)
        assert r.applied_bm25_weight == 0.9
        assert r.route_label == "manual"

    async def test_default_fixed_weight(self):
        svc = self._svc()
        r = await svc.search("手腳冰冷", size=5)
        assert r.applied_bm25_weight == settings.search_bm25_weight
        assert r.route_label is None
