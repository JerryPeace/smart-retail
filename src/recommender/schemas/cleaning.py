"""Cleaning report schemas — Cleaner 跑完一次的成果報告"""
from pydantic import BaseModel, Field


class RowError(BaseModel):
    """單筆驗證失敗的 row 紀錄"""

    row_index: int
    raw_data: dict
    error_message: str


class CleaningReport(BaseModel):
    """一次 cleaning 任務的成果"""

    raw_key: str
    cleaned_key: str | None = None
    brand: str
    mapping_used: str  # 用了哪個 mapper(brand_name 或 default)

    rows_input: int = 0
    rows_output: int = 0
    rows_failed: int = 0

    errors: list[RowError] = Field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.rows_input == 0:
            return 0.0
        return self.rows_output / self.rows_input
