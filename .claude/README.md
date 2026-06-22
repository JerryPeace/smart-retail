# Claude Code Workflow — Marketing Cleaner POC

## 三段式開發流程

```
┌─────────────────────────────────────────────────────┐
│ Session 1: Planning                                 │
├─────────────────────────────────────────────────────┤
│ 1. 探索領域知識                                       │
│    → 讀 docs/architecture/architecture.md            │
│    → 讀目標 module(api/services/repositories)         │
│                                                     │
│ 2. 釐清需求(AskUserQuestion)                         │
│    → 經銷商 vs 消費者?                                │
│    → 月度 vs 即時?                                   │
│    → 算數放 ETL 還是 LLM?                            │
│                                                     │
│ 3. 寫計畫到 tasks/todo.md                            │
│    → 可勾選的步驟                                     │
│    → 預期影響範圍                                     │
│                                                     │
│ 4. 與使用者對齊計畫                                   │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ Session 2: Implementation                           │
├─────────────────────────────────────────────────────┤
│ 1. 讀 .claude/rules/coding-rules.md                 │
│ 2. 三層架構順序:                                      │
│    repositories → services → api                    │
│ 3. 邊做邊更新 tasks/todo.md                          │
│ 4. 真資料前先 mock(ANALYZER_MOCK_MODE=true)          │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│ Session 3: Verification                             │
├─────────────────────────────────────────────────────┤
│ 1. 讀 .claude/rules/safety.md                       │
│ 2. 跑 alembic upgrade head 驗 migration              │
│ 3. 起 docker-compose.dev → curl 真實 endpoint        │
│ 4. 看 log,證明行為符合預期                            │
│ 5. 用 senior-reviewer-py subagent 做 code review     │
└─────────────────────────────────────────────────────┘
```

## 子目錄

| 路徑 | 用途 |
|------|------|
| `rules/coding-rules.md` | FastAPI / SQLModel / 三層架構規範 |
| `rules/safety.md` | Bedrock / DB / S3 / AWS 憑證的安全護欄 |

## Slash Commands(全域)

本專案目前不自帶 slash commands，使用使用者全域提供的：

| Command | Purpose |
|---------|---------|
| `/init` | 重新生成 / 更新 CLAUDE.md |
| `/loop` | 排程重複任務(例如監控 pipeline) |
| `/handoff` | session 太長時寫交接文件 |
| `/cid` | commit + push + deploy 一條龍 |

## 推薦的 Subagent

| Agent | 何時用 |
|-------|--------|
| `Explore` | 跨檔案搜尋、找 symbol、確認某段邏輯在哪 |
| `architect` / `architecture-explorer` | 進入不熟的模組、需要先掌握全貌 |
| `feature-dev:code-explorer` | 深度分析既有 feature 的執行路徑 |
| `feature-dev:code-architect` | 設計新 feature 的實作藍圖 |
| `senior-reviewer` | 寫完 code 後做 review(會自動觸發) |
