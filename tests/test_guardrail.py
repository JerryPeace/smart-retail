"""B2 guardrail config effectiveness verification (tasks.md 7.5).

Confirms that once bedrock_guardrail_id / bedrock_guardrail_version are declared
in Settings, `AgentService._guardrail_config()` actually reads them — no longer
silently swallowed by config.py's `extra="ignore"` (old code used getattr to
probe an undeclared field, which was always None). Reads settings only; no DB,
no Bedrock calls.
"""
from recommender.config import settings
from recommender.services.agent_service import AgentService


def test_guardrail_config_none_when_id_unset(monkeypatch):
    monkeypatch.setattr(settings, "bedrock_guardrail_id", None)
    assert AgentService()._guardrail_config() is None


def test_guardrail_config_built_when_id_set(monkeypatch):
    monkeypatch.setattr(settings, "bedrock_guardrail_id", "gr-test")
    monkeypatch.setattr(settings, "bedrock_guardrail_version", None)
    assert AgentService()._guardrail_config() == {
        "guardrailIdentifier": "gr-test",
        "guardrailVersion": "DRAFT",  # version unset → fallback to DRAFT
        "trace": "enabled",
    }


def test_guardrail_version_respected_when_set(monkeypatch):
    monkeypatch.setattr(settings, "bedrock_guardrail_id", "gr-test")
    monkeypatch.setattr(settings, "bedrock_guardrail_version", "2")
    assert AgentService()._guardrail_config()["guardrailVersion"] == "2"
