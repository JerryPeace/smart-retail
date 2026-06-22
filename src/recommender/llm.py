"""Shared Bedrock LLM builder — process-level cache, one client shared across requests.

Why it lives here (fixes review #3 + #8):
  FastAPI news up a Service for each request (the default DI scope), so a lazy cache on the
  instance like `self._llm` is useless — it rebuilds the Bedrock client every time
  (building a boto3 session has a cost). Changed to a module-level @lru_cache shared by all instances.

Why no asyncio.Lock is needed (TOCTOU):
  Constructing ChatBedrockConverse is "synchronous, no await". An async function only yields
  control back to the event loop when it hits an await; this constructor has no await point from start
  to finish, so two coroutines cannot interleave mid-construction — lru_cache population is atomic, naturally race-free.
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
    """Return a (cached) ChatBedrockConverse. All params are hashable, serving as the lru_cache key.

    guardrail_items: the sorted items tuple of guardrailConfig (a dict isn't hashable, so it's converted to a tuple).
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
