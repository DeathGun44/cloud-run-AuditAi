#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Set PROJECT_ID env var"; exit 1
fi

gcloud config set project "${PROJECT_ID}"
gcloud services enable run.googleapis.com aiplatform.googleapis.com pubsub.googleapis.com firestore.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

gcloud artifacts repositories create auditai --repository-format=docker --location="${REGION}" --description="AuditAI images" || true

gcloud builds submit --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/orchestrator:latest" ./backend
gcloud builds submit --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/worker:latest" ./agents/worker
gcloud builds submit --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/synthesis:latest" ./synthesis-service
gcloud builds submit --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/frontend:latest" ./frontend
gcloud builds submit --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/job:latest" ./job

gsutil mb -l "${REGION}" "gs://${PROJECT_ID}-auditai-receipts" || true
gsutil mb -l "${REGION}" "gs://${PROJECT_ID}-auditai-policies" || true
gsutil mb -l "${REGION}" "gs://${PROJECT_ID}-auditai-reports" || true
gcloud firestore databases create --location="${REGION}" || true

for t in expenses.ingested expenses.extracted expenses.evaluated expenses.analyzed expenses.finalized; do
  gcloud pubsub topics create "$t" || true
done

echo "Deploy Cloud Run services:"
echo "gcloud run deploy orchestrator-api --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/orchestrator:latest --region ${REGION} --allow-unauthenticated --set-env-vars PROJECT_ID=${PROJECT_ID},REGION=${REGION},RECEIPTS_BUCKET=${PROJECT_ID}-auditai-receipts,TOPIC_INGESTED=expenses.ingested"
echo "gcloud run deploy synthesis-service --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/synthesis:latest --region ${REGION} --no-allow-unauthenticated --set-env-vars PROJECT_ID=${PROJECT_ID}"
echo "gcloud run deploy frontend --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/frontend:latest --region ${REGION} --allow-unauthenticated --set-env-vars NEXT_PUBLIC_API_BASE_URL=<ORCHESTRATOR_URL>"

echo "Create Worker Pools via console or gcloud and set env as documented in README_AUDITAI.md"
echo "Create Job: gcloud run jobs create audit-report-job --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/auditai/job:latest --region ${REGION} --set-env-vars PROJECT_ID=${PROJECT_ID},REGION=${REGION},REPORT_BUCKET=${PROJECT_ID}-auditai-reports"
echo "Execute Job: gcloud run jobs execute audit-report-job --region ${REGION}"

echo "Done."

