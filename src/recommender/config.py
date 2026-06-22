"""Application settings — validates all environment variables at startup via pydantic-settings."""
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # === Application ===
    port: int = 8000
    environment: Literal["dev", "staging", "production"] = "dev"
    log_level: str = "INFO"

    # === Database ===
    database_url: str = Field(
        default="postgresql+asyncpg://poc:poc@localhost:5434/marketing_cleaner"
    )

    # === Redis (reserved) ===
    redis_url: str = "redis://:redispoc@localhost:6380"

    # === AWS / LocalStack ===
    aws_endpoint_url_s3: str | None = None  # if set, use LocalStack; if empty, use real AWS
    # Default "lab": the boto3 client for search vectorization uses this profile (auto-refreshes,
    # so credentials no longer expire hourly). Only the search embedder uses this field
    # (main.py preheat / service._embed_query); it does not affect LLM/S3. Aligns with
    # BEDROCK_PROFILE="lab" in embed_products_os.py. To override: set AWS_PROFILE.
    aws_profile: str | None = "lab"
    aws_region: str = "us-east-1"
    s3_raw_bucket: str = "raw-data"
    s3_cleaned_bucket: str = "cleaned-data"
    s3_root_prefix: str = "marketing-recommandation"  # aligns with the LocalStack init script

    # === Bedrock ===
    bedrock_model_id: str = "anthropic.claude-sonnet-4-5"  # generator
    bedrock_judge_model_id: str = "us.anthropic.claude-opus-4-6-v1"  # judge; different generation avoids self-evaluation bias (US cross-region profile)
    bedrock_region: str = "us-east-1"
    aws_bearer_token_bedrock: str | None = None
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str | None = None

    # === Bedrock Embedding (query side of product semantic search) ===
    # query/doc must use the same model, same params, and same dimensions, otherwise the vector spaces are inconsistent.
    # Both doc and query sides use Cohere Embed v4 / 1536 dims / L2-normalized on ap-northeast-1.
    # The query side uses input_type=search_query, the doc side search_document (Cohere asymmetric encoding).
    bedrock_embed_model_id: str = "cohere.embed-v4:0"
    bedrock_embed_region: str = "ap-northeast-1"
    embed_dimensions: int = 1536

    # === OpenSearch (product semantic search) ===
    opensearch_host: str = "http://localhost:9200"
    opensearch_index: str = "products_v5_cohere"

    # === HubSpot (Phase 2) ===
    hubspot_api_key: str | None = None

    # === Search fusion ===
    # Weighting coefficient for the BM25 leg (w_knn is implicitly = 1.0 - search_bm25_weight).
    # Retuned after switching to Cohere Embed v4: the vector leg's Chinese semantics are clean enough that
    # symptom queries (cold hands and feet / sore neck and shoulders from prolonged sitting) are only correct
    # at w_bm25<=0.2 (at 0.7, a BM25 collision on the word "foot" drowns the correct answer -> a tripod); keyword/model-number
    # queries (air fryer / iPhone case) are stable at any w. So 0.7 (the Titan-era value) -> 0.2.
    # Evidence: a w-sweep on Cohere v4 + products_v5 — even brand queries (Samsung / AirPods) prefer low w
    # (high BM25 causes character collisions in Chinese brand names and cannot distinguish products from accessories). So the optimal weight
    # converges to low w for all query types, and type-based routing (auto_route) was removed since its premise no longer holds.
    # For a purely non-semantic SKU number where you want to rely on exact BM25 matching, use ?bm25_weight=.
    search_bm25_weight: float = 0.2

    # Candidate window multiplier: each leg (knn / bm25) takes candidate_k = search_candidate_multiplier × size.
    # Per the investigate_hybrid_fusion.py experiment: minmax_b70_pool20 (pool = 2 × 10 = 20)
    # reaches rel@10=79, pool=10 drops to 72, pool=30 gives no improvement (72); the optimal value is 2.
    # Promoted to a tunable parameter so it can be re-verified later with a new golden set, without affecting the current fusion formula.
    search_candidate_multiplier: int = 2

    # === Feature flags ===
    analyzer_mock_mode: bool = True


settings = Settings()
