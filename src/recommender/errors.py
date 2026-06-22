"""Domain 例外 — service / repository 層拋出,由 main.py 的 exception handler
統一翻成 HTTP response。

為什麼用 domain 例外而非在 service 裡 raise HTTPException:
  service 不該知道 HTTP (它可能被 background task / CLI / 測試呼叫,那些沒有 HTTP)。
  service 拋語意明確的 domain 例外,API 邊界才負責對應到 status code (FastAPI 慣例)。
"""
from __future__ import annotations


class NotFoundError(Exception):
    """查無資源 (recommendation / job / evaluation 等)。→ HTTP 404。"""
