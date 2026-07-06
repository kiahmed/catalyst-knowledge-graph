#!/usr/bin/env bash
# Deploy robotics-social as a Cloud Run Job.
#
# Usage:
#   GCP_PROJECT_ID=robotics-prod ./deploy.sh

set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
GCP_REGION="${GCP_REGION:-us-central1}"
JOB_NAME="${JOB_NAME:-robotics-social}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-${JOB_NAME}-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
ARTIFACT_REPO="${ARTIFACT_REPO:-${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/robotics}"
IMAGE="${ARTIFACT_REPO}/${JOB_NAME}:${IMAGE_TAG}"

cd "$(dirname "$0")"

echo "Building ${IMAGE}..."
gcloud builds submit --tag "${IMAGE}" .

echo "Deploying Cloud Run Job ${JOB_NAME}..."
gcloud run jobs deploy "${JOB_NAME}" \
    --image="${IMAGE}" \
    --region="${GCP_REGION}" \
    --service-account="${SERVICE_ACCOUNT}" \
    --command=python \
    --args=main.py,--once \
    --memory=1Gi \
    --cpu=1 \
    --task-timeout=600 \
    --max-retries=1 \
    --set-env-vars="DUCKDB_PATH=/data/robotics.duckdb,CARD_IMAGES_DIR=/data/exports/card_images,DAILY_POST_CAP=3,MIN_CONFIDENCE=0.75,POSTIZ_CHANNELS=twitter,linkedin,LOG_LEVEL=INFO" \
    --set-secrets="POSTIZ_API_KEY=POSTIZ_API_KEY:latest,POSTIZ_BASE_URL=POSTIZ_BASE_URL:latest"

echo "Triggering a test run..."
gcloud run jobs execute "${JOB_NAME}" --region="${GCP_REGION}" --wait || true

echo "Done."
