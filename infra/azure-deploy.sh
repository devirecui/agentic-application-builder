#!/usr/bin/env bash
set -euo pipefail

# ── Variables ─────────────────────────────────────────────────────────────────
SUBSCRIPTION=66962f7f-7a0e-4bf5-b392-9793c0090b74
RESOURCE_GROUP=job-apply-agent-rg
LOCATION=eastus
SUFFIX=$(printf '%04d' $((RANDOM % 10000)))
STORAGE_ACCOUNT="jobapplyagentsa${SUFFIX}"
SHARE_DATA=job-agent-data
SHARE_OUTPUT=job-agent-output
ACR_NAME="jobapplyagentacr${SUFFIX}"
IMAGE_NAME=job-apply-agent
CONTAINER_NAME=job-apply-agent

# Load .env from project root (works when run from project root or infra/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
if [ -f "$PROJECT_ROOT/.env" ]; then
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
elif [ -f ".env" ]; then
  source ".env"
else
  echo "ERROR: .env file not found. Export ANTHROPIC_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY before running."
  exit 1
fi

echo "=== Deploying Job Apply Agent ==="
echo "    Suffix:          $SUFFIX"
echo "    Storage Account: $STORAGE_ACCOUNT"
echo "    ACR:             $ACR_NAME"
echo ""

# ── a. Login ──────────────────────────────────────────────────────────────────
echo ">>> [a] Logging in to Azure..."
az login --use-device-code

# ── b. Set subscription ───────────────────────────────────────────────────────
echo ">>> [b] Setting subscription..."
az account set --subscription "$SUBSCRIPTION"

# ── c. Create resource group ──────────────────────────────────────────────────
echo ">>> [c] Creating resource group $RESOURCE_GROUP..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output table

# ── d. Create ACR ─────────────────────────────────────────────────────────────
echo ">>> [d] Creating Azure Container Registry $ACR_NAME..."
az acr create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --sku Basic \
  --admin-enabled true \
  --output table

# ── e. Build and push image to ACR ───────────────────────────────────────────
# Note: az acr build (ACR Tasks) may be blocked on some subscriptions.
# Fallback: build locally and push via docker login + docker push.
echo ">>> [e] Building and pushing image to ACR..."
cd "$PROJECT_ROOT"
docker build -t "${IMAGE_NAME}:latest" .
ACR_PASSWORD_PUSH=$(az acr credential show --name "$ACR_NAME" --query passwords[0].value -o tsv)
echo "$ACR_PASSWORD_PUSH" | docker login "${ACR_NAME}.azurecr.io" --username "$ACR_NAME" --password-stdin
docker tag "${IMAGE_NAME}:latest" "${ACR_NAME}.azurecr.io/${IMAGE_NAME}:latest"
docker push "${ACR_NAME}.azurecr.io/${IMAGE_NAME}:latest"

# ── f. Create Storage Account ─────────────────────────────────────────────────
echo ">>> [f] Creating storage account $STORAGE_ACCOUNT..."
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --output table

# ── g. Create file shares ─────────────────────────────────────────────────────
echo ">>> [g] Creating file shares..."
STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" -o tsv)

az storage share create \
  --name "$SHARE_DATA" \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --output table

az storage share create \
  --name "$SHARE_OUTPUT" \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --output table

# ── h. Get ACR credentials ────────────────────────────────────────────────────
echo ">>> [h] Fetching ACR credentials..."
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query passwords[0].value -o tsv)

# ── i. Deploy Azure Container Instance ───────────────────────────────────────
echo ">>> [i] Creating Azure Container Instance..."
cat > /tmp/aci-deploy.yaml << EOF
apiVersion: '2021-10-01'
location: ${LOCATION}
name: ${CONTAINER_NAME}
properties:
  containers:
  - name: ${CONTAINER_NAME}
    properties:
      image: ${ACR_NAME}.azurecr.io/${IMAGE_NAME}:latest
      resources:
        requests:
          cpu: 1
          memoryInGB: 1.5
      environmentVariables:
      - name: ANTHROPIC_API_KEY
        secureValue: "${ANTHROPIC_API_KEY}"
      - name: ADZUNA_APP_ID
        secureValue: "${ADZUNA_APP_ID}"
      - name: ADZUNA_APP_KEY
        secureValue: "${ADZUNA_APP_KEY}"
      volumeMounts:
      - mountPath: /app/data
        name: vol-data
      - mountPath: /app/output
        name: vol-output
  imageRegistryCredentials:
  - server: ${ACR_NAME}.azurecr.io
    username: ${ACR_USERNAME}
    password: "${ACR_PASSWORD}"
  osType: Linux
  restartPolicy: Always
  volumes:
  - name: vol-data
    azureFile:
      shareName: ${SHARE_DATA}
      storageAccountName: ${STORAGE_ACCOUNT}
      storageAccountKey: "${STORAGE_KEY}"
  - name: vol-output
    azureFile:
      shareName: ${SHARE_OUTPUT}
      storageAccountName: ${STORAGE_ACCOUNT}
      storageAccountKey: "${STORAGE_KEY}"
type: Microsoft.ContainerInstance/containerGroups
EOF

az container create \
  --resource-group "$RESOURCE_GROUP" \
  --file /tmp/aci-deploy.yaml

# ── j. Show state and log stream URL ─────────────────────────────────────────
echo ""
echo ">>> [j] Container state:"
az container show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_NAME" \
  --query "{State:instanceView.state, Image:containers[0].image, Restarts:instanceView.restartCount}" \
  --output table

echo ""
echo "Log stream URL:"
echo "  https://portal.azure.com/#resource/subscriptions/${SUBSCRIPTION}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.ContainerInstance/containerGroups/${CONTAINER_NAME}/logs"
echo ""
echo "Tail logs with:"
echo "  az container logs --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME --follow"
echo ""
echo "=== Deploy complete ==="
echo "    Resource group:  $RESOURCE_GROUP"
echo "    ACR:             ${ACR_NAME}.azurecr.io"
echo "    Storage account: $STORAGE_ACCOUNT"
echo "    Container:       $CONTAINER_NAME"
