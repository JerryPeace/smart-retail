"""共用 Bedrock LLM builder — process 層級快取,跨 request 共用同一個 client。

為什麼放這裡 (修 review #3 + #8):
  Service 被 FastAPI 每 request 重新 new 一個 (DI 預設 scope),所以放在
  instance 上的 `self._llm` lazy cache 形同虛設 —— 每次都重建 Bedrock client
  (建 boto3 session 有成本)。改成 module-level @lru_cache,所有 instance 共用。

為什麼不需要 asyncio.Lock (TOCTOU):
  ChatBedrockConverse 的建構是「同步、無 await」的。async 函式只有遇到 await
  才會把控制權交還 event loop;這支建構函式從頭到尾沒有 await point,所以兩個
  coroutine 不可能交錯跑到一半 —— lru_cache 的填充是原子的,天然沒有 race。
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=8)
def get_bedrock_llm(
    model: str,
    region: str,
    temperature: float,
    max_tokens: int,
    guardrail_items: tuple[tuple[str, str], ...] | None = None,
):
    """回 (快取的) ChatBedrockConverse。參數全為 hashable,當 lru_cache key。

    guardrail_items: guardrailConfig 的 sorted items tuple (dict 不可 hash,故轉 tuple)。
    """
    from langchain_aws import ChatBedrockConverse

    extra: dict = {}
    if guardrail_items:
        extra["guardrailConfig"] = dict(guardrail_items)

    return ChatBedrockConverse(
        model=model,
        region_name=region,
        temperature=temperature,
        max_tokens=max_tokens,
        additional_model_request_fields=extra or None,
    )
