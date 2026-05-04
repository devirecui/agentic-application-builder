#!/usr/bin/env bash
set -euo pipefail

# Upload data/resume_base.docx to the Azure File Share after initial deploy.
# Run once from the project root: bash infra/upload-resume.sh

# ── Config ────────────────────────────────────────────────────────────────────
RESOURCE_GROUP=job-apply-agent-rg
SHARE_DATA=job-agent-data
RESUME_LOCAL="data/resume_base.docx"

# ── Resolve script / project root ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

if [ ! -f "$RESUME_LOCAL" ]; then
  echo "ERROR: $RESUME_LOCAL not found. Run from project root."
  exit 1
fi

# ── Detect storage account (find the one whose name starts with jobapplyagentsa) ──
echo ">>> Looking up storage account in $RESOURCE_GROUP..."
STORAGE_ACCOUNT=$(az storage account list \
  --resource-group "$RESOURCE_GROUP" \
  --query "[?starts_with(name,'jobapplyagentsa')].name | [0]" \
  -o tsv)

if [ -z "$STORAGE_ACCOUNT" ]; then
  echo "ERROR: No storage account starting with 'jobapplyagentsa' found in $RESOURCE_GROUP."
  echo "       Run infra/azure-deploy.sh first."
  exit 1
fi

echo "    Storage account: $STORAGE_ACCOUNT"

STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" -o tsv)

# ── Upload ────────────────────────────────────────────────────────────────────
echo ">>> Uploading $RESUME_LOCAL to share $SHARE_DATA..."
az storage file upload \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --share-name "$SHARE_DATA" \
  --source "$RESUME_LOCAL" \
  --path "resume_base.docx"

echo "=== Upload complete: resume_base.docx is now at /app/data/resume_base.docx in the container ==="
