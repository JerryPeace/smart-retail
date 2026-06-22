---
description: "Bedrock / DB / S3 / AWS 憑證的安全規則。執行任何外部副作用前先讀。"
---

# Safety Rules

## 危險動作 — 必須事先和使用者確認

這些動作會產生「真錢、真副作用、真資料變動」，絕對不能默默執行：

### 1. Bedrock LLM 呼叫(會花錢)

- **真實 LLM 呼叫**(`ANALYZER_MOCK_MODE=false` 時)會打 AWS Bedrock，按 token 計費
- 每次跑完整 pipeline 之前，先確認：
  - 環境變數是否在 mock mode?
  - 如果不是 mock，是否真的需要打真 LLM?(通常開發階段都該用 mock)
- **本機批次跑 100+ 個 prompt 之前必須先告知使用者**，因為一晚可能燒掉幾十美金

### 2. 資料庫變動

| 動作 | 風險 |
|------|------|
| `alembic downgrade` | 倒回 migration 會掉資料表 |
| `alembic upgrade head` 在 prod | 沒驗證過的 migration 上 prod 會壞線上 |
| 直接執行 `DROP TABLE` / `TRUNCATE` | 不可逆 |
| `docker-compose down -v` | 連 volume 一起清,會掉 dev DB 全部資料 |

開發時資料是 seed 出來的可以重來，但 **`-v` flag 永遠先警告使用者**。

### 3. S3 與 LocalStack

- **LocalStack** (`localhost:4566`) 是 mock 環境，不會真的影響 AWS — 在這裡 delete / overwrite 是安全的
- **真 AWS S3 bucket** 的 delete / overwrite 必須先和使用者確認
- 不要把真 production 資料同步到 LocalStack 跑測試而沒打 mask(可能含 PII)

### 4. AWS Lab 憑證

- 專案有 `scripts/refresh-lab-creds.sh` 用來刷新 AWS lab 環境的臨時憑證
- **不要把 `.env.local` 提交到 git**(已在 `.gitignore`)
- 不要在 commit message / log / response 裡印完整 AWS access key
- 憑證過期跑 `scripts/refresh-lab-creds.sh` 重新拿,不要手動編輯 `.env.local` 裡的 key

## 安全動作(不需確認)

- 唯讀查詢(SELECT、`alembic current`、`alembic history`)
- LocalStack 上的 S3 動作
- `ANALYZER_MOCK_MODE=true` 下的 pipeline 執行
- Pytest 跑單元測試(只要不是 e2e 打真 AWS)
- 看 log、看 docker container 狀態

## 為什麼要這樣做

**Bedrock 花費風險**：Sonnet 4.5 input ~$3/M token、output ~$15/M token。一次跑「全經銷商 × 全產品」分析動輒幾百萬 token，沒注意 mock flag 一晚燒幾十鎂。預防勝於追討。

**DB Migration 風險**：本專案有 prompt versioning + evaluation 的表結構，downgrade 會掉歷史評估資料(無法重建,因為每次 LLM 回應不一樣)。一律 forward-only migration。

**AWS 憑證風險**：這是 本公司 公司的 AWS lab 帳號,憑證外洩會牽連到組織層級資安。

## 自我檢查清單(交付前問自己)

跑任何外部動作前先問：
1. 這次呼叫會花錢嗎?如果會,使用者知道嗎?
2. 這次資料變動可逆嗎?如果不行,使用者授權了嗎?
3. 我用的是 mock / LocalStack 還是真 AWS?有沒有寫錯環境?
4. 我的 log 會不會印出敏感資訊(API key、PII、客戶名稱)?
