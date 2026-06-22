"""search router — GET /search 端點。

設計取捨（design §8.2 / §1.1 / §11）：
- 只做參數驗證與呼叫 service，不碰 repository / client，不拋 HTTPException。
- 未預期錯誤（OpenSearch 連線失敗等）直接往上飄給 main.py 全域 Exception handler 轉 500。
- 查無結果是 service 回 results=[]（HTTP 200），不在 router 處理。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from recommender.deps import SearchServiceDep
from search_engine.schemas import SearchResponse

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search(
    service: SearchServiceDep,
    q: str = Query(min_length=1),
    size: int = Query(default=10, ge=1, le=100),
    bm25_weight: float | None = Query(default=None, ge=0.0, le=1.0),
):
    """Hybrid 商品搜尋（k-NN + BM25 + min-max 融合）。

    Args:
        q:           搜尋關鍵字（必填，最少 1 個字元）。
        size:        回傳筆數（預設 10，範圍 1–100）。
        bm25_weight: 手動指定 BM25 權重（0–1）；省略則走 auto_route（若開）或固定預設。

    Returns:
        SearchResponse：query 原文 + 商品清單 + 實際採用權重 / 判型標籤。
        查無結果回 HTTP 200 + results=[]。
    """
    return await service.search(q, size=size, bm25_weight=bm25_weight)
