"""search module pure-function unit tests — Phase 5 (Task 5.1–5.4) + min-max fusion.

Coverage:
  5.1  fusion.py         : reciprocal_rank_fusion pure function
  5.1b fusion.py         : min_max_score_fusion pure function (w_bm25=0.7 strategy)
  5.2  repository.py     : build_knn_body / build_bm25_body DSL builder
  5.3  embeddings.py     : MOCK_QUERY_VECTOR invariants
  5.4  service.py        : SearchService orchestration (fake repo injection, zero OpenSearch / Bedrock)

Design principles (aligned with existing test conventions):
  - all 🟢 no docker / no network / zero Bedrock calls
  - conftest already sets ANALYZER_MOCK_MODE=true + settings.analyzer_mock_mode=True
  - async tests are written directly as async def (conftest asyncio_mode=auto)
  - the fake repo is implemented as a minimal duck-typed class, without unittest.mock
"""
from __future__ import annotations

import math

# ---------- 5.1 RRF pure function ----------
from search_engine.fusion import min_max_score_fusion, reciprocal_rank_fusion


class TestRRF:
    """Task 5.1 — reciprocal_rank_fusion pure function."""

    def test_dual_list_b_score_and_rank(self):
        """b appears in both lists, score == 1/61 + 1/62, and ranks first (highest score).

        List A: ["a", "b"]  → a rank=1, b rank=2
        List B: ["b", "c"]  → b rank=1, c rank=2
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

        # b ranks first
        assert result[0][0] == "b", f"b 應排第一，實際排名：{[x[0] for x in result]}"

    def test_k_parameter_changes_scores(self):
        """A smaller k makes the advantage of top-ranked documents more pronounced (higher score); a larger k flattens scores.

        Using the same list with different k, compare the rank=1 score: k↓ → score(rank1)↑.
        """
        docs = ["x", "y", "z"]
        result_k10 = reciprocal_rank_fusion([docs], k=10)
        result_k60 = reciprocal_rank_fusion([docs], k=60)

        score_k10 = dict(result_k10)["x"]
        score_k60 = dict(result_k60)["x"]

        assert score_k10 > score_k60, (
            f"k=10 時 rank1 分數應大於 k=60；得 {score_k10} vs {score_k60}"
        )

        # comparison formula: 1/(10+1) > 1/(60+1)
        assert math.isclose(score_k10, 1 / 11, rel_tol=1e-9)
        assert math.isclose(score_k60, 1 / 61, rel_tol=1e-9)

    def test_empty_outer_list_returns_empty(self):
        """An empty outer list ([]) returns an empty list, without raising."""
        result = reciprocal_rank_fusion([])
        assert result == []

    def test_both_inner_empty_returns_empty(self):
        """Both inner lists empty ([[], []]) returns an empty list, without raising."""
        result = reciprocal_rank_fusion([[], []])
        assert result == []

    def test_single_sided_empty_equals_single_rank(self):
        """When one list is empty, the fused result equals the single-path ranking (only the non-empty side counts).

        List A: ["x", "y"]   List B: []
        Expected: x score=1/61, y score=1/62, order x > y.
        """
        result_with_empty = reciprocal_rank_fusion([["x", "y"], []])
        result_single = reciprocal_rank_fusion([["x", "y"]])

        assert result_with_empty == result_single, (
            f"一邊空應等同單路排名：{result_with_empty} != {result_single}"
        )  # one side empty should equal the single-path ranking

        ids = [doc_id for doc_id, _ in result_with_empty]
        assert ids == ["x", "y"]

    def test_tie_break_deterministic_same_input(self):
        """Running the same input twice gives identical results (tie-break is deterministic)."""
        lists = [["p", "q", "r"], ["r", "q", "p"]]
        result1 = reciprocal_rank_fusion(lists)
        result2 = reciprocal_rank_fusion(lists)
        assert result1 == result2, "同輸入兩次結果不一致！"

    def test_tie_break_alphabetical_order(self):
        """On ties, tie-break by doc_id in ascending lexicographic order (a < b < c).

        With only two lists and two doc_ids at the same rank, the scores are
        necessarily equal; verify the smaller-lexicographic id ranks first.
        """
        # a and b each appear at the same rank: rank=1 on both paths
        result = reciprocal_rank_fusion([["a"], ["b"]])
        # both have the same score (each 1/(60+1)), lexicographic order a < b
        assert result[0][0] == "a", f"同分時 a 應排第一（字典序）：{result}"
        assert result[1][0] == "b"
        assert math.isclose(result[0][1], result[1][1], rel_tol=1e-12), (
            "a 與 b 應同分"
        )  # a and b should have the same score

    def test_single_list_ordering(self):
        """With a single list, rank 1 has the highest score and the original order is preserved."""
        result = reciprocal_rank_fusion([["first", "second", "third"]])
        ids = [doc_id for doc_id, _ in result]
        assert ids == ["first", "second", "third"]

        scores = [score for _, score in result]
        # scores strictly decreasing
        assert scores[0] > scores[1] > scores[2]


# ---------- 5.1b min_max_score_fusion pure function ----------


class TestMinMaxScoreFusion:
    """Task 5.1b — min_max_score_fusion pure function (aligned with investigate_hybrid_fusion.py semantics).

    Key checks:
    - per-path min-max normalization correctness (each path normalizes its own score range)
    - correct weighting (with w_bm25=0.7, a high-scoring BM25 doc ranks first)
    - one-sided absence (the missing path contributes 0)
    - empty input does not raise
    - all-equal-scores / single element normalizes to 1.0
    - deterministic tie-break on ties (doc_id lexicographic order)
    """

    def test_normalization_per_path(self):
        """Each path does its own min-max normalization; differing score ranges don't affect each other.

        knn: [(a, 10.0), (b, 0.0)] → norm_a=1.0, norm_b=0.0
        bm25: [(a, 5.0), (c, 1.0)] → norm_a=1.0, norm_c=0.0

        w_knn=0.5, w_bm25=0.5:
          a: 0.5*1.0 + 0.5*1.0 = 1.0
          b: 0.5*0.0 + 0.5*0.0 (not in bm25) = 0.0
          c: 0.5*0.0 (not in knn) + 0.5*0.0 = 0.0
        """
        knn = [("a", 10.0), ("b", 0.0)]
        bm25 = [("a", 5.0), ("c", 1.0)]
        result = min_max_score_fusion(knn, bm25, w_bm25=0.5, w_knn=0.5)
        id_to_score = dict(result)

        assert math.isclose(id_to_score["a"], 1.0, rel_tol=1e-9), f"a score={id_to_score['a']}"
        assert math.isclose(id_to_score["b"], 0.0, abs_tol=1e-9), f"b score={id_to_score['b']}"
        assert math.isclose(id_to_score["c"], 0.0, abs_tol=1e-9), f"c score={id_to_score['c']}"

    def test_bm25_weight_07_makes_bm25_high_score_rank_first(self):
        """With w_bm25=0.7, a high-scoring BM25 doc takes priority over a knn-only high-rank doc.

        knn:  [(knn_top, 100.0), (shared, 50.0)]  → norm: knn_top=1.0, shared=0.5
        bm25: [(shared, 100.0), (bm25_top, 80.0)] → norm: shared=1.0, bm25_top=0.0

        wait — make bm25_top's score lower than shared's so bm25_top ranks lower
        on the bm25 path, but let the shared doc (high BM25 score, present on both
        paths) rank ahead of knn_top (present only in knn).

        knn:  [(knn_top, 10.0), (shared, 5.0)]  → norm: knn_top=1.0, shared=0.0
        bm25: [(shared, 10.0), (other, 0.0)]    → norm: shared=1.0, other=0.0

        w_knn=0.3, w_bm25=0.7:
          knn_top: 0.3*1.0 + 0.7*0.0 = 0.3
          shared:  0.3*0.0 + 0.7*1.0 = 0.7  ← high BM25 score wins
          other:   0.3*0.0 + 0.7*0.0 = 0.0
        """
        knn = [("knn_top", 10.0), ("shared", 5.0)]
        bm25 = [("shared", 10.0), ("other", 0.0)]
        result = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        ids = [doc_id for doc_id, _ in result]

        assert ids[0] == "shared", (
            f"BM25 高分 doc 應排第一，實際排名：{ids}"
        )  # the high-scoring BM25 doc should rank first
        id_to_score = dict(result)
        assert math.isclose(id_to_score["shared"], 0.7, rel_tol=1e-9)
        assert math.isclose(id_to_score["knn_top"], 0.3, rel_tol=1e-9)

    def test_single_side_missing_contributes_zero(self):
        """When only one path has values, the absent path contributes 0.0.

        knn:  [(a, 5.0), (b, 0.0)]  → norm: a=1.0, b=0.0
        bm25: [] (empty)
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
        """When both paths are empty, returns an empty list without raising."""
        result = min_max_score_fusion([], [], w_bm25=0.7, w_knn=0.3)
        assert result == []

    def test_single_element_per_path_normalizes_to_one(self):
        """When each path has only one element (or all scores equal), it normalizes to 1.0 (aligned with the investigate script).

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
        # b ranks first (0.7 > 0.3)
        assert result[0][0] == "b"

    def test_all_same_score_normalizes_to_one(self):
        """When all docs on a path have the same score (hi==lo), each normalizes to 1.0."""
        knn = [("x", 3.0), ("y", 3.0), ("z", 3.0)]
        result = min_max_score_fusion(knn, [], w_bm25=0.7, w_knn=0.3)
        id_to_score = dict(result)
        for doc_id in ("x", "y", "z"):
            assert math.isclose(id_to_score[doc_id], 0.3 * 1.0, rel_tol=1e-9)

    def test_tie_break_deterministic_same_input(self):
        """Running the same input twice gives identical results (tie-break is deterministic)."""
        knn = [("p", 5.0), ("q", 3.0)]
        bm25 = [("q", 5.0), ("r", 1.0)]
        result1 = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        result2 = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        assert result1 == result2

    def test_tie_break_alphabetical_order(self):
        """On ties, tie-break by doc_id in ascending lexicographic order (a < b).

        Each path has only one doc (each norm=1.0), w_knn=w_bm25=0.5:
          knn: [(b, 10.0)]  → norm_b=1.0 → fused_b=0.5
          bm25: [(a, 10.0)] → norm_a=1.0 → fused_a=0.5
        a==b same score, lexicographic order a < b → a ranks first.
        """
        result = min_max_score_fusion([("b", 10.0)], [("a", 10.0)], w_bm25=0.5, w_knn=0.5)
        assert result[0][0] == "a", f"同分時 a 應排第一（字典序），實際：{result}"
        assert math.isclose(result[0][1], result[1][1], rel_tol=1e-9)

    def test_results_strictly_descending(self):
        """The fused result is sorted strictly in descending score order."""
        knn = [("a", 10.0), ("b", 5.0), ("c", 1.0)]
        bm25 = [("b", 10.0), ("c", 5.0), ("d", 1.0)]
        result = min_max_score_fusion(knn, bm25, w_bm25=0.7, w_knn=0.3)
        scores = [s for _, s in result]
        # ties allowed (tie-break already guarantees determinism), only verify descending order
        assert scores == sorted(scores, reverse=True)


# ---------- 5.2 DSL builder ----------

from search_engine.repository import build_bm25_body, build_knn_body


class TestBuildKnnBody:
    """Task 5.2 — build_knn_body DSL structure verification."""

    def test_size_equals_k(self):
        body = build_knn_body(vector=[0.0] * 1024, k=20)
        assert body["size"] == 20

    def test_knn_embedding_vector_passthrough(self):
        """vector is passed through into query.knn.embedding.vector verbatim, with no transformation."""
        vec = [1.0] + [0.0] * 1023
        body = build_knn_body(vector=vec, k=10)
        assert body["query"]["knn"]["embedding"]["vector"] == vec

    def test_knn_k_in_body(self):
        """The knn sub-query's k field matches the top-level size."""
        body = build_knn_body(vector=[0.5] * 10, k=7)
        assert body["query"]["knn"]["embedding"]["k"] == 7

    def test_structure_keys(self):
        """DSL structure check: size + query.knn.embedding exist."""
        body = build_knn_body(vector=[0.0], k=5)
        assert "size" in body
        assert "query" in body
        assert "knn" in body["query"]
        assert "embedding" in body["query"]["knn"]
        knn_emb = body["query"]["knn"]["embedding"]
        assert "vector" in knn_emb
        assert "k" in knn_emb


class TestBuildBm25Body:
    """Task 5.2 — build_bm25_body DSL structure verification."""

    def test_multi_match_fields(self):
        """fields must be ["martName", "feature", "keyword"] (the Phase 1 index fields)."""
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
        """DSL structure check: size + query.multi_match exist."""
        body = build_bm25_body(query_text="x", k=3)
        assert "size" in body
        assert "query" in body
        assert "multi_match" in body["query"]
        mm = body["query"]["multi_match"]
        assert "query" in mm
        assert "fields" in mm


# ---------- 5.3 mock vector invariants ----------

from search_engine.embeddings import MOCK_QUERY_VECTOR


class TestMockQueryVector:
    """Task 5.3 — MOCK_QUERY_VECTOR length and L2 norm invariants."""

    def test_length_1536(self):
        assert len(MOCK_QUERY_VECTOR) == 1536, (
            f"長度應為 1536，得 {len(MOCK_QUERY_VECTOR)}"
        )  # length should be 1536

    def test_l2_norm_equals_one(self):
        """L2 norm == 1.0 (unit vector, valid in inner-product space). Allows 1e-9 float error."""
        norm_sq = sum(v * v for v in MOCK_QUERY_VECTOR)
        assert math.isclose(norm_sq, 1.0, abs_tol=1e-9), (
            f"L2 norm^2 應為 1.0，得 {norm_sq}"
        )  # L2 norm^2 should be 1.0

    def test_first_component_is_one(self):
        """[1.0] + [0.0]*1535 structure check: the first component is 1.0."""
        assert MOCK_QUERY_VECTOR[0] == 1.0

    def test_remaining_components_are_zero(self):
        """Components 2–1536 are all 0.0 (unit vector structure)."""
        assert all(v == 0.0 for v in MOCK_QUERY_VECTOR[1:])


# ---------- 5.4 SearchService orchestration (fake repo injection) ----------

from search_engine.schemas import SearchResponse, SearchResultItem
from search_engine.service import SearchService


def _make_hit(doc_id: str, mart_name: str, score: float = 1.0, **extra) -> dict:
    """Build an OpenSearch hit (the structure service expects).

    service.py takes doc_id from hit["_id"], the raw score from hit["_score"],
    and metadata from hit["_source"].

    Args:
        doc_id:    OpenSearch _id.
        mart_name: product name (written into _source.martName).
        score:     OpenSearch _score, defaults to 1.0 (used by min-max fusion; when all scores on one path are equal, it normalizes to 1.0).
        **extra:   other _source fields (brand, price, categoryLevel1Name, etc.).
    """
    source = {"martName": mart_name}
    source.update(extra)
    return {"_id": doc_id, "_score": score, "_source": source}


class FakeSearchRepository:
    """Duck-typed fake repo — lets you inject pre-built (knn_hits, bm25_hits).

    The SearchService constructor expects repo to have an async hybrid_msearch method.
    """

    def __init__(
        self, knn_hits: list[dict], bm25_hits: list[dict]
    ) -> None:
        self._knn_hits = knn_hits
        self._bm25_hits = bm25_hits
        self.call_count = 0  # verify it's called exactly once

    async def hybrid_msearch(
        self, vector: list[float], query_text: str, k: int
    ) -> tuple[list[dict], list[dict]]:
        self.call_count += 1
        return self._knn_hits, self._bm25_hits


class TestSearchService:
    """Task 5.4 — SearchService orchestration verification (mock mode, fake repo injection).

    Precondition: conftest already ensures settings.analyzer_mock_mode = True,
          so _embed_query returns MOCK_QUERY_VECTOR directly, with zero Bedrock calls.
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
    # basic return type and structure
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
    # field mapping: _id → mart_id, _source.martName → mart_name, score = RRF score
    # ------------------------------------------------------------------

    async def test_field_mapping_mart_id(self):
        """_id is correctly mapped to mart_id."""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-001", "靈芝飲")],
            bm25_hits=[],
        )
        result = await svc.search("靈芝", size=5)
        assert result.results[0].mart_id == "sku-001"

    async def test_field_mapping_mart_name(self):
        """_source.martName is correctly mapped to mart_name."""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-002", "掃地機器人")],
            bm25_hits=[],
        )
        result = await svc.search("掃地", size=5)
        assert result.results[0].mart_name == "掃地機器人"

    async def test_field_mapping_score_is_fusion_score(self):
        """score is the min-max fusion score (not the OpenSearch raw _score).

        Single path, single doc: norm=1.0 (single element, hi==lo → normalizes to 1.0).
        knn path w_knn = 1 - settings.search_bm25_weight = 0.3, bm25 path empty.
        fused_score = 0.3 * 1.0 + 0.7 * 0.0 = 0.3.
        """
        from recommender.config import settings as _settings

        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-100", "測試品", score=5.0)],
            bm25_hits=[],
        )
        result = await svc.search("查詢", size=5)
        w_knn = 1.0 - _settings.search_bm25_weight
        expected_score = w_knn * 1.0  # single path, single doc norm=1.0, bm25 empty
        assert math.isclose(result.results[0].score, expected_score, rel_tol=1e-9), (
            f"融合分 {result.results[0].score} != 預期 {expected_score}"
        )  # fused score != expected

    async def test_field_mapping_optional_fields(self):
        """The optional brand / price / category fields are correctly mapped from _source."""
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
        assert item.category == "通訊"  # index field categoryLevel1Name → category

    async def test_price_zero_is_preserved_not_none(self):
        """price=0.0 is a valid value and must not be wiped to None by `or None` (code review Bug 2)."""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-free", "贈品", price=0.0)],
            bm25_hits=[],
        )
        result = await svc.search("贈品", size=5)
        assert result.results[0].price == 0.0

    async def test_missing_optional_fields_are_none(self):
        """When _source lacks brand / price / category, the corresponding fields are None (no crash)."""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("sku-300", "基礎商品")],  # no brand/price/category
            bm25_hits=[],
        )
        result = await svc.search("基礎", size=5)
        item = result.results[0]
        assert item.brand is None
        assert item.price is None
        assert item.category is None

    # ------------------------------------------------------------------
    # fusion ordering: a doc appearing on both sides ranks higher
    # ------------------------------------------------------------------

    async def test_fused_doc_ranks_first(self):
        """A doc appearing in both knn and bm25 has the highest fused score and should rank first.

        knn:  [("shared", 10.0), ("knn-only", 5.0)]
          → norm: shared=1.0, knn-only=0.0
        bm25: [("shared", 10.0), ("bm25-only", 5.0)]
          → norm: shared=1.0, bm25-only=0.0

        w_bm25=0.7, w_knn=0.3:
          shared:   0.3*1.0 + 0.7*1.0 = 1.0 (highest)
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
        )  # the fused doc should rank first
        # shared: w_knn*1.0 + w_bm25*1.0 = 1.0
        assert math.isclose(result.results[0].score, 1.0, rel_tol=1e-9)

    async def test_fusion_ordering_across_all_docs(self):
        """After fusing many docs, scores are descending and the results list is descending; the top doc from both paths ranks first.

        knn:  [(a,10.0),(b,5.0),(c,1.0)]  → norm: a=1.0,b≈0.44,c=0.0
        bm25: [(a,10.0),(d,5.0),(e,1.0)]  → norm: a=1.0,d≈0.44,e=0.0

        a is the top score on both paths → highest fused score → ranks first.
        """
        svc, _ = self._make_service(
            knn_hits=[
                _make_hit("a", "A", score=10.0),
                _make_hit("b", "B", score=5.0),
                _make_hit("c", "C", score=1.0),
            ],
            bm25_hits=[
                _make_hit("a", "A", score=10.0),  # a is the top score on both paths → highest fused score
                _make_hit("d", "D", score=5.0),
                _make_hit("e", "E", score=1.0),
            ],
        )
        result = await svc.search("test", size=10)
        scores = [item.score for item in result.results]
        assert scores == sorted(scores, reverse=True), "results 應依 score 降序"
        assert result.results[0].mart_id == "a"

    # ------------------------------------------------------------------
    # top-size truncation
    # ------------------------------------------------------------------

    async def test_top_size_truncation(self):
        """With size=2, return at most 2 items even when there are more hits."""
        knn_hits = [_make_hit(f"doc{i}", f"商品{i}") for i in range(5)]
        bm25_hits = []
        svc, _ = self._make_service(knn_hits, bm25_hits)
        result = await svc.search("query", size=2)
        assert len(result.results) == 2

    async def test_size_larger_than_hits_returns_all(self):
        """When size exceeds the actual hit count, return them all (no padding)."""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("x", "X"), _make_hit("y", "Y")],
            bm25_hits=[],
        )
        result = await svc.search("test", size=50)
        assert len(result.results) == 2

    # ------------------------------------------------------------------
    # both sides empty → results=[] without raising
    # ------------------------------------------------------------------

    async def test_both_empty_returns_empty_results(self):
        """When both knn and bm25 are empty, return results=[] without raising."""
        svc, _ = self._make_service(knn_hits=[], bm25_hits=[])
        result = await svc.search("不存在的查詢", size=10)
        assert result.results == []
        assert isinstance(result, SearchResponse)

    async def test_both_empty_query_preserved(self):
        """When both sides are empty, the original query text is still returned correctly."""
        svc, _ = self._make_service(knn_hits=[], bm25_hits=[])
        result = await svc.search("特殊查詢詞", size=5)
        assert result.query == "特殊查詢詞"

    # ------------------------------------------------------------------
    # query text passthrough
    # ------------------------------------------------------------------

    async def test_query_passthrough(self):
        """SearchResponse.query equals the query string that was passed in."""
        svc, _ = self._make_service(
            knn_hits=[_make_hit("d1", "商品D")],
            bm25_hits=[],
        )
        result = await svc.search("靈芝保健飲", size=5)
        assert result.query == "靈芝保健飲"

    # ------------------------------------------------------------------
    # mock mode check: no Bedrock calls (only the fake repo is called once)
    # ------------------------------------------------------------------

    async def test_mock_mode_only_calls_fake_repo(self):
        """In mock mode _embed_query returns MOCK_QUERY_VECTOR and the fake repo is called exactly once."""
        svc, repo = self._make_service(
            knn_hits=[_make_hit("m1", "Mock商品")],
            bm25_hits=[],
        )
        # SearchService.__init__ reads settings.analyzer_mock_mode (conftest set it to True)
        assert svc.mock_mode is True

        await svc.search("test", size=5)
        assert repo.call_count == 1, "hybrid_msearch 應被呼叫恰好一次"

    async def test_candidate_k_uses_multiplier(self):
        """candidate window candidate_k = settings.search_candidate_multiplier * size.

        Default multiplier is 2 (aligned with the offline-investigation best strategy
        minmax_b70_pool20); verified by intercepting the k argument on the call via the fake repo.
        """
        from recommender.config import settings as _settings

        class CapturingRepo:
            """Records the k argument passed when hybrid_msearch is called."""
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


# ---------- 5.5 SearchService weight resolution (fake repo) ----------

from recommender.config import settings


class TestSearchServiceWeight:
    """Weight resolution priority: manual override > fixed default (auto_route has been removed)."""

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
