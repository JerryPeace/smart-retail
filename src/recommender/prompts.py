"""Prompt template 載入 — 把 prompts/ 下的 .md 編譯成 LangChain ChatPromptTemplate。

對齊 CLAUDE.md「prompt 走 versioning,不要 hardcode prompt」:
  - 模板內容存檔案 (未來可平移到 prompt_variants 表做 A/B),程式只引用 version 字串
  - 統一在這裡載入,兩個 service 共用,避免各自 read_text + str.format

version 字串格式: "{module}/{version}" → prompts/{module}/{version}.md
  例: "judge/v1.0"          → prompts/judge/v1.0.md
      "recommendation/v1.0" → prompts/recommendation/v1.0.md
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate

# src/recommender/prompts.py → parent=recommender → src → repo root,再進 prompts/
PROMPTS_ROOT = Path(__file__).resolve().parent.parent.parent / "prompts"


@cache
def _load_text(version: str) -> str:
    """讀 .md 原文。lru_cache: 同一 version 只讀一次磁碟。

    review #10 注意:快取以 version 字串為 key。改了某個 .md 檔的「內容」但
    沿用同一 version,正在跑的 process 不會重讀,需重啟才生效。這是刻意的 —
    prompt 一旦發布就該是 immutable;要改內容請發新 version (例 v1.1) 並更新
    呼叫端的 *_PROMPT_VERSION 常數,別原地改檔,以免 A/B 測試時新舊混用難追。
    """
    path = PROMPTS_ROOT / f"{version}.md"
    return path.read_text(encoding="utf-8")


def load_system_prompt(version: str, human_template: str) -> ChatPromptTemplate:
    """把 .md 當 system 指令,搭一句 human 觸發,組成 ChatPromptTemplate。

    為什麼分 system / human:
      - system = 角色與評分規則 (穩定、可版本控管的 prompt 本體,放 .md)
      - human  = 本次任務的觸發語 (含 {變數},runtime 注入)
      Bedrock Converse 需要至少一則 user 訊息,單給 system 會報錯,故必帶 human。

    .md 內的 {變數} 與 human_template 內的 {變數} 都會成為這個 template 的
    input_variables —— invoke 時用 dict 一次補齊。
    """
    return ChatPromptTemplate.from_messages([
        ("system", _load_text(version)),
        ("human", human_template),
    ])
