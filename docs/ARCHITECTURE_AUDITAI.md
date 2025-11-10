## AuditAI Architecture (ADK + Cloud Run)

### High-Level Diagram
```
User ───▶ Frontend (Service) ───▶ Orchestrator API (Service, ADK)
                          │                   │
                          │                   ├──▶ Pub/Sub: expenses.ingested
                          │                   │
                          │                   └──▶ Firestore: expenses/{id}
                          │
                          └──▶ SSE stream from Orchestrator/Synthesis

Worker Pool (Pull-based):
  - Extraction Agent ◀── expenses.ingested ── GCS(receipts)
  - Policy Agent ◀────── expenses.extracted ─ GCS(policies)
  - Anomaly Agent ◀───── expenses.evaluated
  - Remediation Agent ◀─ expenses.analyzed

Synthesis Service ◀────── expenses.analyzed
        │
        └──▶ Firestore + emits expenses.finalized

Cloud Run Job (nightly):
  - Reads Firestore
  - Re-scores under updated policy
  - Writes GCS report + notifies
```

### Topics
- `expenses.ingested` — Payload: `{ expenseId, gcsUri, employeeId, submissionTime }`
- `expenses.extracted` — `{ expenseId, fields: { total, currency, date, merchant, lineItems[] }, confidence }`
- `expenses.evaluated` — `{ expenseId, policyFindings[], citations[], score }`
- `expenses.analyzed` — `{ expenseId, anomalies[], remediation[], riskScore }`
- `expenses.finalized` — `{ expenseId, status, resolution, auditTrail[] }`

Include per-topic dead-letter queues (DLQs) with retry policies.

### Data Model (Firestore)
- `expenses/{expenseId}`
  - `status`: enum [INGESTED, EXTRACTED, EVALUATED, REMEDIATED, FINALIZED]
  - `submitter`: { id, email, dept }
  - `extraction`: { fields, confidence, modelMeta }
  - `policy`: { findings[], citations[], score, modelMeta }
  - `anomaly`: { signals[], risk, explainer }
  - `remediation`: { action, messageDraft, approverHints }
  - `final`: { status, resolution, timestamp }
  - `auditLog[]`: append-only entries with actor (agent/human), action, timestamp, changes

### Agents with ADK
- Define each agent with:
  - Tooling: Vertex AI client (Gemini), Firestore client, GCS client
  - Input contract (topic payload), output contract (next topic payload)
  - Idempotency key: `expenseId` + `stageVersion`
  - Observability decorators for structured logs + metrics
  - Guardrails: Max token, safety settings, retry with jitter, circuit breaker on model errors

### Security
- Cloud Run services and worker pool run under dedicated service accounts.
- Use IAM roles with least privilege:
  - Vertex AI: `roles/aiplatform.user`
  - Firestore: `roles/datastore.user`
  - Pub/Sub: `roles/pubsub.subscriber`, `roles/pubsub.publisher`
  - GCS: `roles/storage.objectViewer` (receipts/policies), `roles/storage.objectCreator` (reports)
  - Secret Manager: `roles/secretmanager.secretAccessor`
- Secrets injected via:
  - Env vars (linked Secret Manager versions) for static config at deploy time
  - Mounted secrets (file) if hot-rotation needed

### Networking
- Public: `frontend`
- Private (IAM): `orchestrator-api`, `synthesis-service`, Worker Pool
- Optionally route orchestrator egress through VPC for private resources

### Observability
- Structured JSON logs with correlation ids: `expenseId`, `agentName`, `topic`
- Custom metrics: processing latency per stage, DLQ counts, approval rate, false-positive rate (with human labels)
- Error Reporting integration for agent exceptions
- Tracing: trace IDs added at orchestrator to follow event chain

### Reliability & Backpressure
- Pub/Sub flow control in worker pool to cap concurrent message processing
- DLQs with alerting; manual replay tooling
- Idempotent handlers; dedupe via Firestore document version checks
- Retry: exponential backoff with jitter; poison message routing after N attempts

### Cost & Scale
- Cloud Run min instances per service to remove cold starts on critical paths
- Use Gemini 2.5 Flash Preview (global) for extraction and Gemini 2.5 Flash-Lite Preview for policy stages
- Batch the nightly report via Job to minimize daytime load

### Demo Scenarios
1. Valid receipt — green path; auto-approve.
2. Borderline — requires manager note; remediation agent drafts a message.
3. Invalid — flagged with citations; deny with explanation.


