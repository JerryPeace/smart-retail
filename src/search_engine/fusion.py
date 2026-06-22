"""Pure functions for Reciprocal Rank Fusion and Min-Max Score Fusion.

Design trade-offs (design §7 / §11):
- reciprocal_rank_fusion: takes Sequence[Sequence[str]] (lists of doc ids) rather than raw hits——
  zero OpenSearch type coupling; unit tests just feed lists of strings, no need to mock the
  OpenSearch response structure.
- min_max_score_fusion: takes [(doc_id, raw_score), ...] (per-path hit tuples carrying _score)——
  aligns with the minmax_fusion semantics in investigate_hybrid_fusion.py (per-query per-path normalization).
- metadata join (martName, price…) is the service's responsibility, not done here.
- k=60 is the default from the original RRF paper and industry convention; not exposed in the API query string.
- Ties are broken by doc_id lexicographic order, guaranteeing deterministic results (reproducible tests, stable in prod).
- Empty lists / one-sided gaps are legal: an absent list simply contributes no score.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence


def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[str]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """Fuse multiple sorted doc id lists into a single ranking using the RRF formula.

    Formula: score(doc) = Σ_lists 1 / (k + rank), where rank starts at 1.

    Args:
        result_lists: each element is a "sorted list of doc ids" (most relevant first).
                      May include empty lists; an empty list contributes no score (legal).
        k:            RRF smoothing constant, default 60 (paper convention). A smaller k
                      gives top-ranked documents a stronger advantage; a larger k flattens scores.

    Returns:
        A list of (doc_id, score) sorted by score descending.
        Tied docs are broken by doc_id lexicographic ascending order (guarantees determinism).
    """
    scores: dict[str, float] = defaultdict(float)

    for ranked_list in result_lists:
        for rank, doc_id in enumerate(ranked_list, start=1):
            scores[doc_id] += 1.0 / (k + rank)

    # Sort by score descending; ties broken by doc_id lexicographic ascending (deterministic tie-break)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def min_max_score_fusion(
    knn_scored: list[tuple[str, float]],
    bm25_scored: list[tuple[str, float]],
    w_bm25: float,
    w_knn: float,
) -> list[tuple[str, float]]:
    """Min-Max Score Fusion: per-query normalize each path's raw _score, then weighted-merge.

    Semantically aligned with the minmax_fusion function in scripts/etl/investigate_hybrid_fusion.py,
    ensuring prod results match the offline investigation's rel@10=79.

    Normalization rules (per-path):
    - If a path has only one doc or all docs have identical _score (hi == lo),
      normalize to 1.0 (aligning with the investigate script's "all-equal returns 1.0").
    - An empty path (hits=[]) contributes no score (absent = 0.0).

    Fusion formula:
        fused_score(doc) = w_knn * norm_knn_score + w_bm25 * norm_bm25_score
    When a doc is not in a given path, that path contributes 0.0.

    Args:
        knn_scored:  the k-NN path's [(doc_id, raw_score), ...], in OpenSearch return order.
        bm25_scored: the BM25 path's [(doc_id, raw_score), ...], in OpenSearch return order.
        w_bm25:      the BM25 path's weighting coefficient (e.g. 0.7).
        w_knn:       the k-NN path's weighting coefficient (e.g. 0.3; usually = 1 - w_bm25).

    Returns:
        A list of (doc_id, fused_score) sorted by fused score descending.
        Tied docs are broken by doc_id lexicographic ascending order (guarantees determinism).
    """

    def _normalize(scored: list[tuple[str, float]]) -> dict[str, float]:
        """Min-max normalize a single path, returning {doc_id: norm_score}."""
        if not scored:
            return {}
        vals = [s for _, s in scored]
        lo, hi = min(vals), max(vals)
        if hi == lo:
            # All-equal scores (including a single doc) → always normalize to 1.0 (aligns with investigate script)
            return {doc_id: 1.0 for doc_id, _ in scored}
        return {doc_id: (s - lo) / (hi - lo) for doc_id, s in scored}

    norm_knn = _normalize(knn_scored)
    norm_bm25 = _normalize(bm25_scored)

    fused: dict[str, float] = defaultdict(float)
    for doc_id, v in norm_knn.items():
        fused[doc_id] += w_knn * v
    for doc_id, v in norm_bm25.items():
        fused[doc_id] += w_bm25 * v

    # Sort by score descending; ties broken by doc_id lexicographic ascending (deterministic tie-break)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))
