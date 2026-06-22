"""B2 guardrail 設定實效驗證（tasks.md 7.5）。

確認 bedrock_guardrail_id / bedrock_guardrail_version 宣告進 Settings 後,
`AgentService._guardrail_config()` 會真的讀到 —— 不再被 config.py 的
`extra="ignore"` 靜默吞掉(舊 code 用 getattr 探一個未宣告欄位,永遠 None)。
純讀 settings,不需 DB / 不打 Bedrock。
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
        "guardrailVersion": "DRAFT",  # version 未設 → fallback DRAFT
        "trace": "enabled",
    }


def test_guardrail_version_respected_when_set(monkeypatch):
    monkeypatch.setattr(settings, "bedrock_guardrail_id", "gr-test")
    monkeypatch.setattr(settings, "bedrock_guardrail_version", "2")
    assert AgentService()._guardrail_config()["guardrailVersion"] == "2"
