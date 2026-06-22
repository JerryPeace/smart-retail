"""DatasetService — transform S3 raw → an AI-ready dataset

Responsibilities:
  1. Read raw data from the S3 raw bucket (products + customer)
  2. Apply transformation / mapping / validation (TODO: fill in once real data arrives)
  3. Consolidate into a unified dataset (pandas DataFrame operations)
  4. Write to the S3 cleaned bucket for AgentService to read

This is a stub during the POC phase; the concrete ETL logic will be filled in once
real raw data arrives.
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
        """Transform raw → AI dataset, returning (cleaned S3 key, cleaning report).

        TODO: implement this once the user uploads real data to S3:
            1. Use self.s3.list_objects() to find the raw CSV for this brand/month
            2. Read it with pandas.read_csv (handle inconsistent columns, missing values, types)
            3. Apply a brand-specific mapper to convert to the canonical schema
            4. JOIN customer (from customers/customers.csv)
            5. Filter candidate products (e.g. those suited to this customer's segment)
            6. Write a unified-format CSV to S3 cleaned
            7. Return cleaned key + CleaningReport (rows_in/out, errors, etc.)
        """
        # === Stub: return fixed values until real data arrives ===
        timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
        cleaned_key = f"analyses/{customer_id}/{timestamp}/dataset.csv"

        # Example skeleton: how to use pandas (left for future implementation)
        # raw_csv = await self.s3.get_text(settings.s3_raw_bucket, raw_key)
        # df = pd.read_csv(StringIO(raw_csv))
        # df_clean = df.dropna(subset=["sku", "price"])  # simple cleaning example
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
