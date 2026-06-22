#!/bin/bash
# ===================================================================
# LocalStack ready.d auto-run script
# Runs after the container finishes starting; automatically creates buckets + uploads fixtures
#
# S3 layout (aligned with the local aws-s3/ directory, the source of truth):
#   s3://raw-data/marketing-recommandation/
#     ├── products/
#     │   ├── 3c/2026/{01..12}/
#     │   ├── healthy/2026/{01..12}/
#     │   ├── home-appliance/2026/{01..12}/
#     │   └── daily-necessities/2026/{01..12}/
#     ├── customers/
#     └── sales/
#         └── 2026/{01..12}/
#             └── 04/   (contains 3 xlsx + _manifest.json)
#   s3://cleaned-data/   (ETL output, not seeded here)
# ===================================================================
set -euo pipefail

RAW_BUCKET="${S3_RAW_BUCKET:-raw-data}"
CLEANED_BUCKET="${S3_CLEANED_BUCKET:-cleaned-data}"
ROOT_PREFIX="marketing-recommandation"

# Sync exclude list (Office lock files, macOS metadata, git markers)
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

# products: product master (partitioned by category/year/month)
if [ -d "/fixtures/products" ]; then
    echo "[localstack-init] Syncing products → s3://${RAW_BUCKET}/${ROOT_PREFIX}/products/"
    awslocal s3 sync /fixtures/products/ "s3://${RAW_BUCKET}/${ROOT_PREFIX}/products/" "${EXCLUDES[@]}"
fi

# customers: customer master
if [ -d "/fixtures/customers" ]; then
    echo "[localstack-init] Syncing customers → s3://${RAW_BUCKET}/${ROOT_PREFIX}/customers/"
    awslocal s3 sync /fixtures/customers/ "s3://${RAW_BUCKET}/${ROOT_PREFIX}/customers/" "${EXCLUDES[@]}"
fi

# sales: monthly sales-type data (performance tracking / monthly sales / daily sales report / sales visits)
# Written monthly by the SharePoint sync script; during the POC stage, manually seeded in the local aws-s3/
if [ -d "/fixtures/sales" ]; then
    echo "[localstack-init] Syncing sales → s3://${RAW_BUCKET}/${ROOT_PREFIX}/sales/"
    awslocal s3 sync /fixtures/sales/ "s3://${RAW_BUCKET}/${ROOT_PREFIX}/sales/" "${EXCLUDES[@]}"
fi

echo "[localstack-init] Verifying structure..."
awslocal s3 ls "s3://${RAW_BUCKET}/${ROOT_PREFIX}/" --recursive | head -60

echo "[localstack-init] ✅ Ready"
