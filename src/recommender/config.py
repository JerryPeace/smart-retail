"""應用設定 — 啟動時透過 pydantic-settings 驗證所有環境變數"""
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

    # === Redis (預留) ===
    redis_url: str = "redis://:redispoc@localhost:6380"

    # === AWS / LocalStack ===
    aws_endpoint_url_s3: str | None = None  # 設了就走 LocalStack;留空走真 AWS
    # 預設 "lab"：search 向量化的 boto3 client 走此 profile（會 auto-refresh，憑證不再
    # 每小時過期）。只有 search embedder 用此欄（main.py 預熱 / service._embed_query），
    # 不影響 LLM/S3。對齊 embed_products_os.py 的 BEDROCK_PROFILE="lab"。覆寫：設 AWS_PROFILE。
    aws_profile: str | None = "lab"
    aws_region: str = "us-east-1"
    s3_raw_bucket: str = "raw-data"
    s3_cleaned_bucket: str = "cleaned-data"
    s3_root_prefix: str = "marketing-recommandation"  # 對齊 LocalStack init script

    # === Bedrock ===
    bedrock_model_id: str = "anthropic.claude-sonnet-4-5"  # generator
    bedrock_judge_model_id: str = "us.anthropic.claude-opus-4-6-v1"  # judge,跨世代避自評偏誤(US cross-region profile)
    bedrock_region: str = "us-east-1"
    aws_bearer_token_bedrock: str | None = None
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str | None = None

    # === Bedrock Embedding (商品語意搜尋 query 端) ===
    # query/doc 必須同模型同參數同維度,否則向量空間不一致。
    # doc/query 兩端皆 ap-northeast-1 的 Cohere Embed v4 / 1536 維 / L2 正規化。
    # query 端用 input_type=search_query、doc 端 search_document（Cohere 不對稱編碼）。
    bedrock_embed_model_id: str = "cohere.embed-v4:0"
    bedrock_embed_region: str = "ap-northeast-1"
    embed_dimensions: int = 1536

    # === OpenSearch (商品語意搜尋) ===
    opensearch_host: str = "http://localhost:9200"
    opensearch_index: str = "products_v5_cohere"

    # === HubSpot (Phase 2) ===
    hubspot_api_key: str | None = None

    # === Search fusion ===
    # BM25 路加權係數（w_knn 隱式 = 1.0 - search_bm25_weight）。
    # 換 Cohere Embed v4 後重調：向量腿中文語意夠乾淨，症狀 query（手腳冰冷／久坐肩頸痠痛）
    # 在 w_bm25≤0.2 才正確（0.7 時 BM25「腳」碰撞淹掉正解→三腳架）；關鍵字/型號 query
    # （氣炸鍋／iPhone 殼）在任何 w 皆穩。故 0.7（Titan 時代值）→ 0.2。
    # 證據：Cohere v4 + products_v5 的 w-sweep——含品牌 query（三星/AirPods）也是低 w 較好
    # （高 BM25 中文品牌名中字碰撞、且分不清商品與配件）。故所有 query 類型最佳權重收斂到低 w，
    # 判型路由（auto_route）因前提消失而移除。純無語意料號 SKU 想倚賴 BM25 精確匹配用 ?bm25_weight=。
    search_bm25_weight: float = 0.2

    # 候選窗倍數：每邊（knn / bm25）各取 candidate_k = search_candidate_multiplier × size。
    # 依 investigate_hybrid_fusion.py 實驗：minmax_b70_pool20（pool = 2 × 10 = 20）
    # 達 rel@10=79，pool=10 降到 72，pool=30 無改善（72）；最佳值為 2。
    # 提升為可調參數以便日後用新 golden set 重驗，不影響現有融合公式。
    search_candidate_multiplier: int = 2

    # === Feature flags ===
    analyzer_mock_mode: bool = True


settings = Settings()
