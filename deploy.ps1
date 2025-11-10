$ErrorActionPreference = "Stop"

param(
  [string]$ProjectId = $(Read-Host "Project ID"),
  [string]$Region = "us-central1"
)

Write-Host "Configuring gcloud..." -ForegroundColor Cyan
gcloud config set project $ProjectId | Out-Null

Write-Host "Enabling APIs..." -ForegroundColor Cyan
gcloud services enable run.googleapis.com aiplatform.googleapis.com pubsub.googleapis.com firestore.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com | Out-Null

Write-Host "Creating Artifact Registry repo (auditai)..." -ForegroundColor Cyan
gcloud artifacts repositories create auditai --repository-format=docker --location=$Region --description="AuditAI images" 2>$null | Out-Null

Write-Host "Building images..." -ForegroundColor Cyan
gcloud builds submit --tag $Region-docker.pkg.dev/$ProjectId/auditai/orchestrator:latest ./backend
gcloud builds submit --tag $Region-docker.pkg.dev/$ProjectId/auditai/worker:latest ./agents/worker
gcloud builds submit --tag $Region-docker.pkg.dev/$ProjectId/auditai/synthesis:latest ./synthesis-service
gcloud builds submit --tag $Region-docker.pkg.dev/$ProjectId/auditai/frontend:latest ./frontend
gcloud builds submit --tag $Region-docker.pkg.dev/$ProjectId/auditai/job:latest ./job

Write-Host "Creating buckets and Firestore (if needed)..." -ForegroundColor Cyan
gsutil mb -l $Region gs://$ProjectId-auditai-receipts 2>$null | Out-Null
gsutil mb -l $Region gs://$ProjectId-auditai-policies 2>$null | Out-Null
gsutil mb -l $Region gs://$ProjectId-auditai-reports 2>$null | Out-Null
gcloud firestore databases create --location=$Region 2>$null | Out-Null

Write-Host "Creating Pub/Sub topics..." -ForegroundColor Cyan
foreach ($t in @("expenses.ingested","expenses.extracted","expenses.evaluated","expenses.analyzed","expenses.finalized")) {
  gcloud pubsub topics create $t 2>$null | Out-Null
}

Write-Host "Deploying Cloud Run services..." -ForegroundColor Cyan
Write-Host "Use these commands to deploy (adjust IAM and URLs):" -ForegroundColor Yellow
Write-Host "gcloud run deploy orchestrator-api --image $Region-docker.pkg.dev/$ProjectId/auditai/orchestrator:latest --region $Region --allow-unauthenticated --set-env-vars PROJECT_ID=$ProjectId,REGION=$Region,RECEIPTS_BUCKET=$ProjectId-auditai-receipts,TOPIC_INGESTED=expenses.ingested"
Write-Host "gcloud run deploy synthesis-service --image $Region-docker.pkg.dev/$ProjectId/auditai/synthesis:latest --region $Region --no-allow-unauthenticated --set-env-vars PROJECT_ID=$ProjectId"
Write-Host "gcloud run deploy frontend --image $Region-docker.pkg.dev/$ProjectId/auditai/frontend:latest --region $Region --allow-unauthenticated --set-env-vars NEXT_PUBLIC_API_BASE_URL=<ORCHESTRATOR_URL>"

Write-Host ""
Write-Host "Worker Pools (create via console or gcloud when available) pointing to:" -ForegroundColor Yellow
Write-Host "  Image: $Region-docker.pkg.dev/$ProjectId/auditai/worker:latest"
Write-Host "  Env (extraction): PROJECT_ID=$ProjectId, REGION=$Region, MODEL_LOCATION=global, EXTRACTION_MODEL_LOCATION=global, AGENT_TYPE=extraction, SUBSCRIPTION=expenses.ingested.sub, TOPIC_OUT=expenses.extracted, RECEIPTS_BUCKET=$ProjectId-auditai-receipts, EXTRACTION_MODEL=gemini-2.5-flash-preview-09-2025, AGENT_QPS=2, MAX_INFLIGHT_MESSAGES=2, MAX_INFLIGHT_BYTES=2097152"
Write-Host "  Env (policy):     PROJECT_ID=$ProjectId, REGION=$Region, MODEL_LOCATION=global, POLICY_MODEL_LOCATION=global, AGENT_TYPE=policy, SUBSCRIPTION=expenses.extracted.policy, TOPIC_OUT=expenses.evaluated, POLICY_BUCKET=$ProjectId-auditai-policies, POLICY_MODEL=gemini-2.5-flash-lite-preview-09-2025, AGENT_QPS=2, MAX_INFLIGHT_MESSAGES=2, MAX_INFLIGHT_BYTES=2097152"
Write-Host "  Env (anomaly):    PROJECT_ID=$ProjectId, REGION=$Region, MODEL_LOCATION=global, AGENT_TYPE=anomaly, SUBSCRIPTION=expenses.evaluated.anomaly, TOPIC_OUT=expenses.analyzed, AGENT_QPS=2, MAX_INFLIGHT_MESSAGES=2, MAX_INFLIGHT_BYTES=2097152"
Write-Host "  Env (remediation):PROJECT_ID=$ProjectId, REGION=$Region, MODEL_LOCATION=global, AGENT_TYPE=remediation, SUBSCRIPTION=expenses.analyzed.remediation, TOPIC_OUT=, AGENT_QPS=2, MAX_INFLIGHT_MESSAGES=2, MAX_INFLIGHT_BYTES=2097152"

Write-Host ""
Write-Host "Run job (after first data):" -ForegroundColor Yellow
Write-Host "gcloud run jobs create audit-report-job --image $Region-docker.pkg.dev/$ProjectId/auditai/job:latest --region $Region --set-env-vars PROJECT_ID=$ProjectId,REGION=$Region,REPORT_BUCKET=$ProjectId-auditai-reports"
Write-Host "gcloud run jobs execute audit-report-job --region $Region"

Write-Host ""
Write-Host "Scaffold complete." -ForegroundColor Green

