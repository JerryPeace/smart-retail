"""時間工具 — 統一產生 UTC datetime。

為什麼 (修 review #7):
  utcnow() 在 Py 3.12+ 已 deprecated,且回的是 naive datetime
  (不帶 tzinfo)。naive datetime 在跨時區比較 / 寫入 TIMESTAMPTZ 時會踩坑。
  本專案跑 Py 3.14,沿用會一直噴 DeprecationWarning。

  統一走這支 utcnow(),回 naive UTC datetime。

  注意 (bug fix — 測試期間揭露):
  DB migration 定義的欄位是 TIMESTAMP WITHOUT TIME ZONE (sa.DateTime()),
  asyncpg 不允許把 timezone-aware datetime 寫入 naive TIMESTAMP 欄位
  (「can't subtract offset-naive and offset-aware datetimes」)。
  因此維持 naive UTC:datetime.now(UTC).replace(tzinfo=None)。
  未來如需 TIMESTAMPTZ,應新增 migration 把欄位改成 DateTime(timezone=True),
  再把這裡改回 datetime.now(UTC)。
"""
from datetime import UTC, datetime


def utcnow() -> datetime:
    """現在 UTC 時間（naive,相容 TIMESTAMP WITHOUT TIME ZONE DB schema）。"""
    return datetime.now(UTC).replace(tzinfo=None)
