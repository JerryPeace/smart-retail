"""Cleaning report schemas — the result report for one Cleaner run."""
from pydantic import BaseModel, Field


class RowError(BaseModel):
    """Record for a single row that failed validation."""

    row_index: int
    raw_data: dict
    error_message: str


class CleaningReport(BaseModel):
    """The result of one cleaning task."""

    raw_key: str
    cleaned_key: str | None = None
    brand: str
    mapping_used: str  # which mapper was used (brand_name or default)

    rows_input: int = 0
    rows_output: int = 0
    rows_failed: int = 0

    errors: list[RowError] = Field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.rows_input == 0:
            return 0.0
        return self.rows_output / self.rows_input
