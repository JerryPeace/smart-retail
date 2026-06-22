"""search 領域 DTO — router 回給 client 的「白名單」欄位。

職責：定義 SearchResultItem 與 SearchResponse 兩個 Pydantic schema。
不放 OpenSearch raw hit 結構（那是 repository 的內部事）。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SearchResultItem(BaseModel):
    """單筆商品搜尋結果。

    score 為 RRF 融合分（不是 OpenSearch _score；兩路 _score 量綱不同不可比）。
    brand / price / category 為可選欄位（索引中可能缺漏）。
    """

    model_config = ConfigDict(from_attributes=True)

    mart_id: str
    mart_name: str
    score: float
    brand: str | None = None
    price: float | None = None
    category: str | None = None


class SearchResponse(BaseModel):
    """搜尋端點回傳結構。

    query: 原始查詢字串（原樣帶回，方便 client 對照）。
    results: 依 RRF 融合分降序排列的商品清單；查無結果回空 list（HTTP 200）。
    """

    query: str
    results: list[SearchResultItem]
    # 路由觀察欄位（auto_route 或 manual 覆寫時填）：
    # applied_bm25_weight = 本次融合實際用的 BM25 權重；route_label = 判型結果或 "manual"。
    # 預設 None（auto_route 關且無手動覆寫時不填，向後相容）。
    applied_bm25_weight: float | None = None
    route_label: str | None = None
