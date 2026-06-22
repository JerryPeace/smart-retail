"""PromptVariant — A/B 測試的 prompt 變體 (DB-managed prompt registry)"""
from datetime import datetime

from recommender.timeutil import utcnow

from sqlmodel import Field, SQLModel


class PromptVariant(SQLModel, table=True):
    __tablename__ = "prompt_variant"

    id: int | None = Field(default=None, primary_key=True)

    # Identity
    name: str = Field(index=True)  # 例 "recommendation"
    version: str  # 例 "v1.0", "v2.0"
    template: str  # 完整 prompt 文字 (含 {placeholders})

    # A/B 控制
    is_active: bool = Field(default=False, index=True)  # 同時可有多個 active
    weight: float = Field(default=1.0)  # 流量分配權重

    # Metadata
    notes: str | None = None  # 「實驗目的」(例: 加入 chain-of-thought 步驟)
    created_at: datetime = Field(default_factory=utcnow, index=True)

    class Config:
        # name + version 組合唯一
        # (Alembic autogenerate 不會自動加 unique constraint,需要手動補 migration)
        pass
