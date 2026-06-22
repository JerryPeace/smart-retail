"""Prompt template loading — compiles the .md files under prompts/ into LangChain ChatPromptTemplates.

Aligns with CLAUDE.md "prompts use versioning, don't hardcode prompts":
  - Template content lives in files (can later be migrated to the prompt_variants table for A/B); the code only references the version string
  - Loaded in one place here, shared by both services, to avoid each doing its own read_text + str.format

version string format: "{module}/{version}" → prompts/{module}/{version}.md
  e.g.: "judge/v1.0"          → prompts/judge/v1.0.md
        "recommendation/v1.0" → prompts/recommendation/v1.0.md
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate

# src/recommender/prompts.py → parent=recommender → src → repo root, then into prompts/
PROMPTS_ROOT = Path(__file__).resolve().parent.parent.parent / "prompts"


@cache
def _load_text(version: str) -> str:
    """Read the raw .md text. lru_cache: each version reads from disk only once.

    review #10 note: the cache is keyed by the version string. If you change the *content* of a .md file but
    keep the same version, a running process will not re-read it; a restart is required to take effect. This is intentional —
    once published, a prompt should be immutable; to change content, publish a new version (e.g. v1.1) and update
    the caller's *_PROMPT_VERSION constant, rather than editing the file in place, to avoid mixing old and new in A/B tests in a way that's hard to trace.
    """
    path = PROMPTS_ROOT / f"{version}.md"
    return path.read_text(encoding="utf-8")


def load_system_prompt(version: str, human_template: str) -> ChatPromptTemplate:
    """Use the .md as the system instruction, paired with a human trigger line, to form a ChatPromptTemplate.

    Why split system / human:
      - system = role and scoring rules (the stable, version-controlled body of the prompt, in .md)
      - human  = the trigger phrase for this task (contains {variables}, injected at runtime)
      Bedrock Converse requires at least one user message; providing only system errors out, so a human message is always included.

    The {variables} inside the .md and inside human_template all become this template's
    input_variables — filled in at once with a dict at invoke time.
    """
    return ChatPromptTemplate.from_messages([
        ("system", _load_text(version)),
        ("human", human_template),
    ])
