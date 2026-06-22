#!/bin/bash
# ===================================================================
# 把 playground 基底 session 用 MFA 刷成 24 小時，再把 lab 憑證寫進 .env.local
#
# 為什麼需要這個（與 refresh-lab-creds.sh 的差異）:
#   lab profile 是「playground(暫時 session) → assume <LAB_ROLE> role」的 role chaining，
#   AWS 規定 role chaining 的 session 最長只有 1 小時——所以 .env.local 裡的 lab 靜態憑證
#   本質上撐不過 1 小時，沒有腳本能突破。
#
#   真正的解法是把「基底 playground session」刷成 24 小時（用 IAM user 長期金鑰 + MFA 跑
#   get-session-token --duration 86400）。基底活著時，app 的 lab profile 會自動續期
#   （見 config.aws_profile="lab"），整天不必再 MFA；.env.local 的 lab 憑證也能隨時重匯出。
#
# 前置（必須先 export 你的 IAM user 長期金鑰，這支腳本不存它們）:
#   export AWS_MFA_ACCESS_KEY_ID="AKIA...你的長期 access key"
#   export AWS_MFA_SECRET_ACCESS_KEY="...你的長期 secret key"
#
# 用法: ./scripts/refresh-session-token.sh   （會提示輸入 MFA 6 碼）
# ===================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# IAM user 長期金鑰（從環境變數讀，腳本不落地）
LT_ACCESS_KEY="${AWS_MFA_ACCESS_KEY_ID:?請先 export AWS_MFA_ACCESS_KEY_ID（IAM user 長期 access key）}"
LT_SECRET_KEY="${AWS_MFA_SECRET_ACCESS_KEY:?請先 export AWS_MFA_SECRET_ACCESS_KEY（IAM user 長期 secret key）}"
MFA_ACCOUNT="${AWS_MFA_ACCOUNT:-<REDACTED_ACCOUNT>}"      # playground 帳號（jerry_playground MFA 所在）
MFA_DEVICE="${AWS_MFA_DEVICE:-jerry_playground}"
MFA_SERIAL="arn:aws:iam::${MFA_ACCOUNT}:mfa/${MFA_DEVICE}"
BASE_PROFILE="${AWS_BASE_PROFILE:-playground}"      # lab/stg/prd 的 source_profile
DURATION="${SESSION_DURATION:-86400}"               # 24h（IAM user get-session-token 上限 36h）

echo "==> 基底 profile: $BASE_PROFILE  ·  MFA: $MFA_DEVICE  ·  時效: $((DURATION/3600))h"
printf "請輸入 MFA 6 位數代碼: "
read -r MFA_CODE
[[ "$MFA_CODE" =~ ^[0-9]{6}$ ]] || { echo "❌ MFA 代碼必須是 6 位數字"; exit 1; }

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

# 備份後寫進 base profile（lab/stg/prd 都 chain 自它）
[ -f ~/.aws/credentials ] && cp ~/.aws/credentials ~/.aws/credentials.backup."$(date +%Y%m%d%H%M%S)"
aws configure set aws_access_key_id     "$NEW_AK" --profile "$BASE_PROFILE"
aws configure set aws_secret_access_key "$NEW_SK" --profile "$BASE_PROFILE"
aws configure set aws_session_token     "$NEW_ST" --profile "$BASE_PROFILE"
echo "✅ $BASE_PROFILE 基底 session 已刷新，有效到 $NEW_EXP（~$((DURATION/3600))h，期間 lab 自動續期免 MFA）"

# 把 lab 憑證匯出到 .env.local（沿用既有腳本，1h 一份但基底活著就能隨時重跑、免 MFA）
echo "==> 匯出 lab 憑證到 .env.local …"
./scripts/refresh-lab-creds.sh

echo "✅ 完成。app 的 search 走 lab profile 會自動續期 ~24h；.env.local 的 lab 靜態憑證仍 1h（AWS role chaining 上限），但基底活著時重跑 refresh-lab-creds.sh 免 MFA。"
