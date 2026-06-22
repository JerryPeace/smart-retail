#!/bin/bash
# ===================================================================
# 把 AWS lab profile 的暫時憑證寫進 .env.local
# 用法: ./scripts/refresh-lab-creds.sh
#
# 為什麼需要這個:
#   boto3 在 Python 跑時無法觸發互動式 MFA prompt,所以不能直接用 AWS_PROFILE。
#   必須先在 shell 用 aws CLI(已 cached MFA)export 暫時憑證,寫進 .env.local 給 FastAPI 用。
#
#   暫時憑證有效期通常 1-12 小時,過期就重跑這支。
# ===================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

PROFILE="${AWS_PROFILE_NAME:-lab}"
ENV_FILE=".env.local"

echo "==> Exporting credentials from profile: $PROFILE"

# 取憑證(用 eval 把 export 命令當變數讀進來)
CREDS=$(aws configure export-credentials --profile "$PROFILE" --format env 2>&1)
if [ $? -ne 0 ]; then
    echo "❌ Failed to export credentials. 確認:"
    echo "   1. ~/.aws/credentials 或 ~/.aws/config 有 [profile $PROFILE]"
    echo "   2. 該 profile 已透過 aws CLI 完成過 MFA / SSO 登入"
    exit 1
fi

# 解析每個 export
ACCESS_KEY=$(echo "$CREDS" | grep AWS_ACCESS_KEY_ID | cut -d= -f2)
SECRET_KEY=$(echo "$CREDS" | grep AWS_SECRET_ACCESS_KEY | cut -d= -f2)
SESSION_TOKEN=$(echo "$CREDS" | grep AWS_SESSION_TOKEN | cut -d= -f2)
EXPIRATION=$(echo "$CREDS" | grep AWS_CREDENTIAL_EXPIRATION | cut -d= -f2)

# 移除 .env.local 既有的 AWS_ACCESS_KEY_ID / SECRET / SESSION_TOKEN / EXPIRATION
sed -i.bak '/^AWS_ACCESS_KEY_ID=/d;/^AWS_SECRET_ACCESS_KEY=/d;/^AWS_SESSION_TOKEN=/d;/^AWS_CREDENTIAL_EXPIRATION=/d' "$ENV_FILE"

# 加進新的(在 AWS_PROFILE 行之後)
{
    echo ""
    echo "# Auto-injected by scripts/refresh-lab-creds.sh @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "AWS_ACCESS_KEY_ID=$ACCESS_KEY"
    echo "AWS_SECRET_ACCESS_KEY=$SECRET_KEY"
    echo "AWS_SESSION_TOKEN=$SESSION_TOKEN"
    echo "AWS_CREDENTIAL_EXPIRATION=$EXPIRATION"
} >> "$ENV_FILE"

rm -f "$ENV_FILE.bak"

echo "✅ Credentials written to $ENV_FILE"
echo "   Expires at: $EXPIRATION"
echo ""
echo "現在可跑: uv run uvicorn recommender.main:app --reload"
