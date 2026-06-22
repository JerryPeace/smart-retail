#!/bin/bash
# ===================================================================
# Refresh the playground base session to 24 hours via MFA, then write the lab credentials into .env.local
#
# Why this is needed (difference from refresh-lab-creds.sh):
#   The lab profile is role chaining of "playground (temporary session) → assume <LAB_ROLE> role".
#   AWS limits a role-chained session to at most 1 hour — so the static lab credentials in .env.local
#   inherently cannot last beyond 1 hour, and no script can get around that.
#
#   The real fix is to refresh the "base playground session" to 24 hours (using the IAM user's long-term keys + MFA
#   to run get-session-token --duration 86400). While the base session is alive, the app's lab profile auto-renews
#   (see config.aws_profile="lab"), so no MFA is needed all day; the lab credentials in .env.local can also be re-exported anytime.
#
# Prerequisite (you must export your IAM user's long-term keys first; this script does not store them):
#   export AWS_MFA_ACCESS_KEY_ID="AKIA...your long-term access key"
#   export AWS_MFA_SECRET_ACCESS_KEY="...your long-term secret key"
#
# Usage: ./scripts/refresh-session-token.sh   (will prompt for the 6-digit MFA code)
# ===================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# IAM user long-term keys (read from environment variables; the script does not persist them)
LT_ACCESS_KEY="${AWS_MFA_ACCESS_KEY_ID:?請先 export AWS_MFA_ACCESS_KEY_ID（IAM user 長期 access key）}"
LT_SECRET_KEY="${AWS_MFA_SECRET_ACCESS_KEY:?請先 export AWS_MFA_SECRET_ACCESS_KEY（IAM user 長期 secret key）}"
MFA_ACCOUNT="${AWS_MFA_ACCOUNT:-<REDACTED_ACCOUNT>}"      # playground account (where the jerry_playground MFA lives)
MFA_DEVICE="${AWS_MFA_DEVICE:-jerry_playground}"
MFA_SERIAL="arn:aws:iam::${MFA_ACCOUNT}:mfa/${MFA_DEVICE}"
BASE_PROFILE="${AWS_BASE_PROFILE:-playground}"      # source_profile for lab/stg/prd
DURATION="${SESSION_DURATION:-86400}"               # 24h (IAM user get-session-token cap is 36h)

echo "==> 基底 profile: $BASE_PROFILE  ·  MFA: $MFA_DEVICE  ·  時效: $((DURATION/3600))h"
printf "請輸入 MFA 6 位數代碼: "
read -r MFA_CODE
[[ "$MFA_CODE" =~ ^[0-9]{6}$ ]] || { echo "❌ MFA 代碼必須是 6 位數字"; exit 1; }

# Get a session token using the IAM long-term keys + MFA
echo "==> 用 IAM 長期金鑰 + MFA 取 ${DURATION}s session token …"
RESP=$(AWS_ACCESS_KEY_ID="$LT_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$LT_SECRET_KEY" \
    aws sts get-session-token \
    --serial-number "$MFA_SERIAL" --token-code "$MFA_CODE" \
    --duration-seconds "$DURATION" --output json 2>&1) || {
    echo "❌ get-session-token 失敗:"; echo "$RESP"; exit 1; }

echo "$RESP" | grep -q "Credentials" || { echo "❌ 回應無 Credentials:"; echo "$RESP"; exit 1; }
NEW_AK=$(echo "$RESP" | jq -r '.Credentials.AccessKeyId')
NEW_SK=$(echo "$RESP" | jq -r '.Credentials.SecretAccessKey')
NEW_ST=$(echo "$RESP" | jq -r '.Credentials.SessionToken')
NEW_EXP=$(echo "$RESP" | jq -r '.Credentials.Expiration')

# Back up, then write into the base profile (lab/stg/prd all chain from it)
[ -f ~/.aws/credentials ] && cp ~/.aws/credentials ~/.aws/credentials.backup."$(date +%Y%m%d%H%M%S)"
aws configure set aws_access_key_id     "$NEW_AK" --profile "$BASE_PROFILE"
aws configure set aws_secret_access_key "$NEW_SK" --profile "$BASE_PROFILE"
aws configure set aws_session_token     "$NEW_ST" --profile "$BASE_PROFILE"
echo "✅ $BASE_PROFILE 基底 session 已刷新，有效到 $NEW_EXP（~$((DURATION/3600))h，期間 lab 自動續期免 MFA）"

# Export the lab credentials to .env.local (reuse the existing script; each batch lasts 1h, but while the base session is alive you can re-run anytime without MFA)
echo "==> 匯出 lab 憑證到 .env.local …"
./scripts/refresh-lab-creds.sh

echo "✅ 完成。app 的 search 走 lab profile 會自動續期 ~24h；.env.local 的 lab 靜態憑證仍 1h（AWS role chaining 上限），但基底活著時重跑 refresh-lab-creds.sh 免 MFA。"
