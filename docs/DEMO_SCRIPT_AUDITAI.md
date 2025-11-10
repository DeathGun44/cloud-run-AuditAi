## Demo Script (7–9 minutes)

### Goal
Demonstrate an autonomous, multi-agent expense audit workflow with Cloud Run services, worker pool, and a nightly job — plus Cloud Run production features (preview tags, rollback, logs, metrics).

### Setup (before demo)
- Deploy `frontend`, `orchestrator-api`, worker pool agents, `synthesis-service`, and `audit-report-job`.
- Prepare three receipts: valid, borderline (coffee + taxi), invalid (alcohol).
- Upload 1–2 policy PDFs to `gs://<PROJECT_ID>-auditai-policies`.

### Flow
1) Open `frontend` URL and briefly show the architecture diagram link.
2) Upload “valid” receipt.
   - Point to Cloud Run metrics (requests) and logs tab.
   - Show SSE progress: Ingested → Extracted (Gemini 2.5 Flash Preview) → Evaluated → Finalized.
   - Result: approved. Show fields + confidence.
3) Upload “borderline” receipt.
   - Show policy citations and anomaly flags; remediation agent drafts a message.
   - Approve with note.
4) Upload “invalid” receipt.
   - Show deny with rationale and citations.
5) Run the nightly job:
   - Trigger `audit-report-job` from UI/CLI; show GCS report output.
6) Preview links & rollback:
   - Deploy a “blue” revision (cosmetic change).
   - Use revision tag URL to preview, then flip traffic to blue.
   - If time allows, show quick rollback.

### Close
- Recap: multi-agent ADK, Gemini 2.5 Flash Preview / Flash-Lite Preview, multi-service Cloud Run, worker pool, job, IAM, Secret Manager, observability.
- Mention blog post + video link + `#CloudRunHackathon`.


