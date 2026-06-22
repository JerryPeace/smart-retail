# Marketing Cleaner POC — Claude Code Guide

## 專案速覽

把 本公司 業務資料(產品 × 經銷商)透過 LLM 分析，產出「給某經銷商推薦某商品 + 理由 + 信心度」的結構化推薦報告，供下游(未來 HubSpot)消費。

技術棧：**FastAPI · SQLModel · Alembic · PostgreSQL 17 · LocalStack S3 · LangChain + AWS Bedrock(Sonnet 4.5)**。三層架構：`api/` → `services/` → `repositories/`。

## 領域詞彙(務必對齊)

| 詞 | 在本專案指什麼 |
|---|---|
| **客戶 / customer** | **經銷商**(B2B)，不是消費者 |
| **資料交付節奏** | **每月一份**(績效追蹤 + 月銷售各一)。prompt 中的「今日」應解讀為「本月」 |
| **推薦** | 跨產品 × 跨經銷商的 AI 配對建議 |

## Workflow Orchestration

### 1. Plan Mode Default

- 任何非瑣碎任務(3 步以上或牽涉架構決策)先進 plan mode
- 出狀況時 STOP 並 re-plan，不要硬推
- Plan 用於驗證步驟，不只是建構

### 2. Subagent Strategy

- 自由使用 subagent 把 main context 留乾淨
- Research / exploration / 平行分析交給 subagent
- 一個 subagent 一個任務，聚焦執行

### 3. Self-Improvement Loop

- 被使用者糾正後：把模式寫進 `tasks/lessons.md`(沒有就建立)
- Session 開始時複習 lessons
- 反覆迭代直到錯誤率下降

### 4. Verification Before Done

- 沒驗證過就不能宣告完成
- 跑測試、看 log、實際發 request 證明
- 自問：「資深工程師會 approve 嗎？」

### 5. Demand Elegance (Balanced)

- 非瑣碎改動先停下問：「有沒有更優雅的做法？」
- 覺得 hacky 就重來
- 簡單 fix 不要過度設計

### 6. Autonomous Bug Fixing

- 給 bug report → 直接修，不要等指示
- 指向 log / error / failing test → 解決它
- 不需要使用者切換 context

## Task Management

1. **Plan First** — 寫 plan 進 `tasks/todo.md`，項目可勾選
2. **Verify Plan** — 開工前 check-in
3. **Track Progress** — 邊做邊勾
4. **Explain Changes** — 每步給高層摘要
5. **Document Results** — 在 `tasks/todo.md` 加 review section
6. **Capture Lessons** — 被糾正後更新 `tasks/lessons.md`

## Core Principles

- **Simplicity First** — 每次改動越簡單越好。最小程式碼變動
- **No Laziness** — 找根因。不要暫時 fix。資深工程師標準
- **Minimal Impact** — 只動該動的地方，避免引入新 bug
- **ETL First, LLM Last** — Python/SQL 演算法處理資料聚合，LLM 只接已聚合表做 narrative。**不要讓 LLM 算數**
- **演算法 first, LLM fallback** — 解析 xlsx 等結構化資料預設用演算法，只在格式漂移導致失敗時 fallback 到 LLM

## Architecture

- 理解專案架構前，先讀 [`docs/architecture/architecture.md`](./docs/architecture/architecture.md)
- 規劃 / 部署相關計畫見 [`docs/plans/`](./docs/plans/)

## Rules

- **寫 / 改 code** 前先讀 [`.claude/rules/coding-rules.md`](./.claude/rules/coding-rules.md)
- **執行 LLM / DB / S3 動作** 前先讀 [`.claude/rules/safety.md`](./.claude/rules/safety.md)

## Context Window Management

Context window 接近上限會自動 compact。不要因為 token 預算焦慮提早收工。接近上限時把進度與狀態寫進 `tasks/todo.md`，永遠堅持完成任務。

## After Compaction

Context 被 compact 後，重新讀目前 task plan 與相關檔案再繼續。
