"""Recommendation — Bedrock LLM agent 產出的銷售建議"""
from datetime import datetime

from recommender.timeutil import utcnow
from enum import Enum

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class HubSpotSyncStatus(str, Enum):
    pending = "pending"
    syncing = "syncing"
    synced = "synced"
    failed = "failed"


class Recommendation(SQLModel, table=True):
    __tablename__ = "recommendation"

    # === Identity ===
    id: int | None = Field(default=None, primary_key=True)
    customer_id: str = Field(index=True)

    # === Hot columns (從 payload 抽出,加 index 方便查詢) ===
    customer_segment: str | None = Field(default=None, index=True)
    confidence_score: float | None = Field(default=None, index=True)

    # === Cold JSONB (single source of truth: agent 完整輸出) ===
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))

    # === Schema versioning ===
    schema_version: str = Field(default="v1.0", index=True)

    # === LLM metadata ===
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None

    # === A/B testing — 哪個 prompt variant 產出這份結果 ===
    prompt_variant_id: int | None = Field(
        default=None, foreign_key="prompt_variant.id", index=True
    )

    # === Audit trail ===
    generated_at: datetime = Field(default_factory=utcnow, index=True)
    pipeline_job_id: int | None = Field(default=None, foreign_key="pipeline_job.id")

    # === HubSpot sync 狀態 (Phase 4 才會用) ===
    hubspot_sync_status: HubSpotSyncStatus = Field(
        default=HubSpotSyncStatus.pending, index=True
    )
    hubspot_contact_id: str | None = Field(default=None, index=True)
    hubspot_note_id: str | None = None
    hubspot_synced_at: datetime | None = None
    hubspot_sync_error: str | None = None
    hubspot_sync_retries: int = Field(default=0)
