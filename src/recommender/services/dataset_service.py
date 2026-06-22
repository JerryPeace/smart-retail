"""DatasetService — 整理 S3 raw → AI 可用的 dataset

職責:
  1. 從 S3 raw bucket 讀進原始資料 (products + customer)
  2. 套上 transformation / mapping / 驗證 (TODO: 等真實資料來填)
  3. 整理成統一的 dataset (pandas DataFrame 操作)
  4. 寫到 S3 cleaned bucket,供 AgentService 讀取

POC 階段是 stub,等實際 raw data 進來再填具體 ETL 邏輯。
"""
from recommender.timeutil import utcnow

import pandas as pd

from recommender.config import settings
from recommender.schemas.cleaning import CleaningReport
from recommender.services.s3_service import S3Service


class DatasetService:
    def __init__(self, s3: S3Service) -> None:
        self.s3 = s3

    async def prepare(
        self,
        customer_id: str,
        brand: str,
        month: str,
    ) -> tuple[str, CleaningReport]:
        """整理 raw → AI dataset,回傳 (cleaned S3 key, cleaning report).

        TODO: 這支等使用者把真實資料放上 S3 後再實作:
            1. 用 self.s3.list_objects() 找出該品牌/月份的 raw CSV
            2. 用 pandas.read_csv 讀進來 (處理欄位不一致、缺值、型別)
            3. 套 brand-specific mapper 轉成 canonical schema
            4. JOIN customer (從 customers/customers.csv)
            5. 篩選 candidate products (例: 該客戶 segment 適合的)
            6. 寫成統一格式 CSV 到 S3 cleaned
            7. 回傳 cleaned key + CleaningReport (rows_in/out, errors 等)
        """
        # === Stub:回固定值,等真實資料來再實作 ===
        timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
        cleaned_key = f"analyses/{customer_id}/{timestamp}/dataset.csv"

        # 範例骨架: pandas 怎麼用 (留給未來填)
        # raw_csv = await self.s3.get_text(settings.s3_raw_bucket, raw_key)
        # df = pd.read_csv(StringIO(raw_csv))
        # df_clean = df.dropna(subset=["sku", "price"])  # 簡單 cleaning 範例
        # csv_out = df_clean.to_csv(index=False)
        # await self.s3.put_text(settings.s3_cleaned_bucket, cleaned_key, csv_out)

        report = CleaningReport(
            raw_key=f"{settings.s3_root_prefix}/products/{brand}/2026/{month.split('-')[1]}/products.csv",
            cleaned_key=cleaned_key,
            brand=brand,
            mapping_used="default",
            rows_input=0,
            rows_output=0,
            rows_failed=0,
        )
        return cleaned_key, report
