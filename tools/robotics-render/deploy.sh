#!/usr/bin/env bash
# Deploy robotics-render to Cloud Run (service).
#
# Usage:
#   GCP_PROJECT_ID=robotics-prod ./deploy.sh

set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
GCP_REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-robotics-render}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-${SERVICE_NAME}-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
ARTIFACT_REPO="${ARTIFACT_REPO:-${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/robotics}"
IMAGE="${ARTIFACT_REPO}/${SERVICE_NAME}:${IMAGE_TAG}"

cd "$(dirname "$0")"

echo "Building ${IMAGE}..."
gcloud builds submit --tag "${IMAGE}" .

echo "Deploying ${SERVICE_NAME} to ${GCP_PROJECT_ID} (${GCP_REGION})..."
gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE}" \
    --region="${GCP_REGION}" \
    --service-account="${SERVICE_ACCOUNT}" \
    --no-allow-unauthenticated \
    --memory=2Gi \
    --cpu=2 \
    --timeout=120 \
    --min-instances=0 \
    --max-instances=5 \
    --concurrency=1 \
    --set-env-vars="DUCKDB_PATH=/data/robotics.duckdb,CARD_IMAGES_DIR=/data/exports/card_images,TEMPLATES_DIR=/templates,LOG_LEVEL=INFO"

echo "Done. URL: $(gcloud run services describe ${SERVICE_NAME} --region=${GCP_REGION} --format='value(status.url)')"
