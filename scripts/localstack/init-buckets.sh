#!/bin/bash
# ===================================================================
# LocalStack ready.d 自動執行 script
# Container 啟動完成後執行,自動建 bucket + 上傳 fixture
#
# S3 結構 (對齊本地 aws-s3/ 目錄, source of truth):
#   s3://raw-data/marketing-recommandation/
#     ├── products/
#     │   ├── 3c/2026/{01..12}/
#     │   ├── healthy/2026/{01..12}/
#     │   ├── home-appliance/2026/{01..12}/
#     │   └── daily-necessities/2026/{01..12}/
#     ├── customers/
#     └── sales/
#         └── 2026/{01..12}/
#             └── 04/   (含 3 xlsx + _manifest.json)
#   s3://cleaned-data/   (ETL 產出, 不在此 seed)
# ===================================================================
set -euo pipefail

RAW_BUCKET="${S3_RAW_BUCKET:-raw-data}"
CLEANED_BUCKET="${S3_CLEANED_BUCKET:-cleaned-data}"
ROOT_PREFIX="marketing-recommandation"

# Sync 排除清單 (Office lock files, macOS metadata, git markers)
EXCLUDES=(
    --exclude "*.gitkeep"
    --exclude ".DS_Store"
    --exclude "**/.DS_Store"
    --exclude "~\$*"           # Microsoft Office lock files (~$xxx.xlsx)
    --exclude "**/~\$*"
)

echo "[localstack-init] Creating buckets..."
awslocal s3 mb "s3://${RAW_BUCKET}" || echo "  ${RAW_BUCKET} already exists"
awslocal s3 mb "s3://${CLEANED_BUCKET}" || echo "  ${CLEANED_BUCKET} already exists"

# products: 商品 master (按 category/year/month 分區)
if [ -d "/fixtures/products" ]; then
    echo "[localstack-init] Syncing products → s3://${RAW_BUCKET}/${ROOT_PREFIX}/products/"
    awslocal s3 sync /fixtures/products/ "s3://${RAW_BUCKET}/${ROOT_PREFIX}/products/" "${EXCLUDES[@]}"
fi

# customers: 客戶 master
if [ -d "/fixtures/customers" ]; then
    echo "[localstack-init] Syncing customers → s3://${RAW_BUCKET}/${ROOT_PREFIX}/customers/"
    awslocal s3 sync /fixtures/customers/ "s3://${RAW_BUCKET}/${ROOT_PREFIX}/customers/" "${EXCLUDES[@]}"
fi

# sales: 月度銷售類資料 (績效追蹤 / 月銷售 / 業績日報 / 業務拜訪)
# 由 SharePoint sync 腳本每月寫入, POC 階段手動 seed 在本地 aws-s3/
if [ -d "/fixtures/sales" ]; then
    echo "[localstack-init] Syncing sales → s3://${RAW_BUCKET}/${ROOT_PREFIX}/sales/"
    awslocal s3 sync /fixtures/sales/ "s3://${RAW_BUCKET}/${ROOT_PREFIX}/sales/" "${EXCLUDES[@]}"
fi

echo "[localstack-init] Verifying structure..."
awslocal s3 ls "s3://${RAW_BUCKET}/${ROOT_PREFIX}/" --recursive | head -60

echo "[localstack-init] ✅ Ready"
