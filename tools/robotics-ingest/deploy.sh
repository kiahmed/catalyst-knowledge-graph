#!/usr/bin/env bash
# Deploy robotics-ingest to Cloud Functions Gen 2.
#
# Prereqs:
#   - gcloud CLI authenticated: gcloud auth login
#   - Project set: gcloud config set project $GCP_PROJECT_ID
#   - tools/infra Terraform applied (creates SAs, buckets, Secret Manager entries)
#
# Usage:
#   GCP_PROJECT_ID=robotics-prod GCP_REGION=us-central1 ./deploy.sh
#
# Env vars read:
#   GCP_PROJECT_ID       (required)
#   GCP_REGION           (default: us-central1)
#   FUNCTION_NAME        (default: robotics-ingest)
#   SERVICE_ACCOUNT      (default: robotics-ingest-sa@$GCP_PROJECT_ID.iam.gserviceaccount.com)
#   ARBORYX_PROJECT      (default: $GCP_PROJECT_ID)       — Firestore source project
#   FIRESTORE_DATABASE   (default: (default))             — Arboryx Firestore DB id
#   FIRESTORE_COLLECTION (default: findings)              — source collection
#   DUCKDB_GCS_BUCKET    (default: robotics-data)         — Robotics state bucket

set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
GCP_REGION="${GCP_REGION:-us-central1}"
FUNCTION_NAME="${FUNCTION_NAME:-robotics-ingest}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-${FUNCTION_NAME}-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"
ARBORYX_PROJECT="${ARBORYX_PROJECT:-$GCP_PROJECT_ID}"
FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-(default)}"
FIRESTORE_COLLECTION="${FIRESTORE_COLLECTION:-findings}"
DUCKDB_GCS_BUCKET="${DUCKDB_GCS_BUCKET:?set DUCKDB_GCS_BUCKET}"

cd "$(dirname "$0")"

echo "Deploying ${FUNCTION_NAME} to ${GCP_PROJECT_ID} (${GCP_REGION})..."

gcloud functions deploy "${FUNCTION_NAME}" \
    --gen2 \
    --runtime=python312 \
    --region="${GCP_REGION}" \
    --source=. \
    --entry-point=run_ingest \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account="${SERVICE_ACCOUNT}" \
    --memory=2Gi \
    --cpu=1 \
    --timeout=540s \
    --max-instances=1 \
    --concurrency=1 \
    --set-env-vars="SECTOR=Robotics,GCP_PROJECT=${ARBORYX_PROJECT},FIRESTORE_DATABASE=${FIRESTORE_DATABASE},FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION},DUCKDB_GCS_BUCKET=${DUCKDB_GCS_BUCKET},LOG_LEVEL=INFO" \
    --set-secrets="GEMINI_API_KEY=GEMINI_API_KEY:latest"

echo "Deployed. Invoke:"
echo "  gcloud functions call ${FUNCTION_NAME} --region=${GCP_REGION} --data='{\"dry_run\":true}'"
