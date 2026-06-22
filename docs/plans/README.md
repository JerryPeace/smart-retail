# Plans

各 phase 的工作計畫文件。每份計畫含:目標、前置條件、待釐清問題、工作項目、不做的事。

| 計畫 | Phase | 狀態 |
|------|-------|------|
| [data-governance.md](./data-governance.md) | 1.5 — ETL 資料治理 | ✅ 完成(scope pivot,實際建出 `/analyses/sales` 月度分析,見文件 §9) |
| [promo-forecast-data-fitness.md](./promo-forecast-data-fitness.md) | 2.0 — 專戶促銷預測 POC | 🟡 等 PO 對齊 4 個關鍵問題後啟動(資料 fitness 結論已完成) |
| [promo-forecast-moea-business-scope.md](./promo-forecast-moea-business-scope.md) | 2.0 動作 3 — 33 家專戶經濟部所營事業盤點 | ✅ 已完成 batch 查詢,等 PO 拍板擴充 mapping (~24 條) |
| [product-search-vectorization.md](./product-search-vectorization.md) | 3.0 — 電商商品語意/Hybrid 搜尋 POC(本地 docker OpenSearch + Titan v2) | 🟡 計劃待審；Phase 1=原始資料載入本地 OpenSearch + Titan v2 向量化 |

> 完整架構背景請看 [../architecture/architecture.md](../architecture/architecture.md)
> 業務脈絡關鍵 fact 存在 memory:`~/.claude/projects/.../memory/MEMORY.md`
