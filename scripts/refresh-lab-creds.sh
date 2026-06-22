#!/bin/bash
# ===================================================================
# Write the AWS lab profile's temporary credentials into .env.local
# Usage: ./scripts/refresh-lab-creds.sh
#
# Why this is needed:
#   When running in Python, boto3 cannot trigger an interactive MFA prompt, so AWS_PROFILE can't be used directly.
#   You must first export the temporary credentials in the shell via the aws CLI (with MFA already cached), writing them into .env.local for FastAPI to use.
#
#   Temporary credentials are usually valid for 1-12 hours; re-run this once they expire.
# ===================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

PROFILE="${AWS_PROFILE_NAME:-lab}"
ENV_FILE=".env.local"

echo "==> Exporting credentials from profile: $PROFILE"

# Fetch credentials (use eval to read the export commands in as variables)
CREDS=$(aws configure export-credentials --profile "$PROFILE" --format env 2>&1)
if [ $? -ne 0 ]; then
    echo "❌ Failed to export credentials. 確認:"
    echo "   1. ~/.aws/credentials 或 ~/.aws/config 有 [profile $PROFILE]"
    echo "   2. 該 profile 已透過 aws CLI 完成過 MFA / SSO 登入"
    exit 1
fi

# Parse each export
ACCESS_KEY=$(echo "$CREDS" | grep AWS_ACCESS_KEY_ID | cut -d= -f2)
SECRET_KEY=$(echo "$CREDS" | grep AWS_SECRET_ACCESS_KEY | cut -d= -f2)
SESSION_TOKEN=$(echo "$CREDS" | grep AWS_SESSION_TOKEN | cut -d= -f2)
EXPIRATION=$(echo "$CREDS" | grep AWS_CREDENTIAL_EXPIRATION | cut -d= -f2)

# Remove the existing AWS_ACCESS_KEY_ID / SECRET / SESSION_TOKEN / EXPIRATION from .env.local
sed -i.bak '/^AWS_ACCESS_KEY_ID=/d;/^AWS_SECRET_ACCESS_KEY=/d;/^AWS_SESSION_TOKEN=/d;/^AWS_CREDENTIAL_EXPIRATION=/d' "$ENV_FILE"

# Append the new ones (after the AWS_PROFILE line)
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
