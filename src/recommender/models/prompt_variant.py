"""PromptVariant — prompt variants for A/B testing (DB-managed prompt registry)."""
from datetime import datetime

from recommender.timeutil import utcnow

from sqlmodel import Field, SQLModel


class PromptVariant(SQLModel, table=True):
    __tablename__ = "prompt_variant"

    id: int | None = Field(default=None, primary_key=True)

    # Identity
    name: str = Field(index=True)  # e.g. "recommendation"
    version: str  # e.g. "v1.0", "v2.0"
    template: str  # the full prompt text (with {placeholders})

    # A/B control
    is_active: bool = Field(default=False, index=True)  # multiple can be active at once
    weight: float = Field(default=1.0)  # traffic allocation weight

    # Metadata
    notes: str | None = None  # "experiment purpose" (e.g.: add a chain-of-thought step)
    created_at: datetime = Field(default_factory=utcnow, index=True)

    class Config:
        # name + version combination is unique
        # (Alembic autogenerate won't add the unique constraint automatically; a migration must be added manually)
        pass
