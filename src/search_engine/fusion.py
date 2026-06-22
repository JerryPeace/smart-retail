"""Reciprocal Rank Fusion 與 Min-Max Score Fusion 純函式。

設計取捨（design §7 / §11）：
- reciprocal_rank_fusion：吃 Sequence[Sequence[str]]（doc id 清單）而非 raw hits——
  零 OpenSearch 型別耦合，單元測試餵字串清單即可，無需模擬 OpenSearch 回應結構。
- min_max_score_fusion：吃 [(doc_id, raw_score), ...]（每路帶 _score 的 hit tuple）——
  對齊 investigate_hybrid_fusion.py 的 minmax_fusion 語意（per-query per-path 正規化）。
- metadata join（martName、price…）是 service 的職責，不在此處。
- k=60 為 RRF 原始論文與業界慣例預設值；不開放到 API query string。
- 同分以 doc_id 字典序 tie-break，保證結果 deterministic（測試可重現、線上穩定）。
- 空清單 / 單邊缺漏合法：缺席的清單就是不貢獻分數。
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence


def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[str]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """將多路已排序 doc id 清單以 RRF 公式融合成單一排名。

    公式：score(doc) = Σ_lists 1 / (k + rank)，rank 從 1 起算。

    Args:
        result_lists: 每個元素是「已排序的 doc id 清單」（最相關排前）。
                      可包含空清單；空清單不貢獻任何分數（合法）。
        k:            RRF 平滑常數，預設 60（論文慣例）。較小的 k 讓高排名文件
                      優勢更明顯；較大的 k 使分數更平坦。

    Returns:
        依 score 降序排列的 (doc_id, score) list。
        同分 doc 以 doc_id 字典序升序 tie-break（保證 deterministic）。
    """
    scores: dict[str, float] = defaultdict(float)

    for ranked_list in result_lists:
        for rank, doc_id in enumerate(ranked_list, start=1):
            scores[doc_id] += 1.0 / (k + rank)

    # 降序排分；同分以 doc_id 字典序升序（tie-break deterministic）
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def min_max_score_fusion(
    knn_scored: list[tuple[str, float]],
    bm25_scored: list[tuple[str, float]],
    w_bm25: float,
    w_knn: float,
) -> list[tuple[str, float]]:
    """Min-Max Score Fusion：對每路 raw _score 做 per-query 正規化後加權合併。

    語意對齊 scripts/etl/investigate_hybrid_fusion.py 的 minmax_fusion 函式，
    確保 prod 結果與離線調查的 rel@10=79 一致。

    正規化規則（per-path）：
    - 若路內只有一個 doc 或所有 doc 的 _score 完全相同（hi == lo），
      正規化為 1.0（對齊 investigate 腳本的「全同分回 1.0」）。
    - 空路（hits=[]）不貢獻任何分數（缺席 = 0.0）。

    融合公式：
        fused_score(doc) = w_knn * norm_knn_score + w_bm25 * norm_bm25_score
    某 doc 不在某路時，該路貢獻 0.0。

    Args:
        knn_scored:  k-NN 路的 [(doc_id, raw_score), ...]，依 OpenSearch 回傳順序。
        bm25_scored: BM25 路的 [(doc_id, raw_score), ...]，依 OpenSearch 回傳順序。
        w_bm25:      BM25 路的加權係數（例如 0.7）。
        w_knn:       k-NN 路的加權係數（例如 0.3；通常 = 1 - w_bm25）。

    Returns:
        依融合分降序排列的 (doc_id, fused_score) list。
        同分 doc 以 doc_id 字典序升序 tie-break（保證 deterministic）。
    """

    def _normalize(scored: list[tuple[str, float]]) -> dict[str, float]:
        """對單路做 min-max 正規化，回 {doc_id: norm_score}。"""
        if not scored:
            return {}
        vals = [s for _, s in scored]
        lo, hi = min(vals), max(vals)
        if hi == lo:
            # 全同分（含只有單一 doc）→ 一律正規化為 1.0（對齊 investigate 腳本）
            return {doc_id: 1.0 for doc_id, _ in scored}
        return {doc_id: (s - lo) / (hi - lo) for doc_id, s in scored}

    norm_knn = _normalize(knn_scored)
    norm_bm25 = _normalize(bm25_scored)

    fused: dict[str, float] = defaultdict(float)
    for doc_id, v in norm_knn.items():
        fused[doc_id] += w_knn * v
    for doc_id, v in norm_bm25.items():
        fused[doc_id] += w_bm25 * v

    # 降序排分；同分以 doc_id 字典序升序（tie-break deterministic）
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))
