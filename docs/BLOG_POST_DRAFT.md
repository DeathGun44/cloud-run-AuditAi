# Building AuditAI: An Autonomous Multi-Agent Expense Auditor with Google ADK and Cloud Run

*Built for the Google Cloud Run Hackathon 2025*

## The Problem: Manual Expense Audits Are Killing Productivity

Every finance team knows the pain: mountains of expense reports, receipts in various formats, policy documents scattered everywhere, and hours spent manually reviewing each submission. A typical company processes hundreds of expense claims monthly, each requiring:

- Receipt verification and data extraction
- Policy compliance checking
- Anomaly detection (duplicate claims, suspicious amounts)
- Approval workflow coordination

**The result?** Finance teams spend 10-15 hours per week on manual audits, claims take days to process, and errors slip through.

## The Solution: AuditAI — A Multi-Agent Autonomous Auditor

I built **AuditAI**, an intelligent expense audit system that uses Google's Agent Development Kit (ADK) and Cloud Run to automate the entire audit pipeline with **five specialized AI agents** working in parallel.

### Architecture Overview

```
Receipt Upload → Orchestrator → Pub/Sub → Worker Pools (4 agents) → Synthesis → Result
                                    ↓
                                Firestore
```

**The Five Agents:**

1. **Extraction Agent** — Uses Gemini 2.5 Flash Preview (global) to extract structured data from receipt images/text
2. **Policy Agent** — Uses ADK + Gemini 2.5 Flash-Lite Preview to check compliance against company policies with citations
3. **Anomaly Agent** — Detects suspicious patterns (duplicates, unusual amounts, timing anomalies)
4. **Remediation Agent** — Suggests fixes for policy violations
5. **Synthesis Agent** — Aggregates findings and produces final audit verdict with full trail

### Why ADK?

Google's Agent Development Kit (ADK) is a game-changer for building multi-agent systems. Here's what made it powerful:

- **Code-first approach**: Define agents in Python, not complex config files
- **Built-in orchestration**: Sequential and parallel agent execution patterns
- **Native Gemini integration**: Seamless access to Gemini 2.5 Flash Preview and Flash-Lite Preview
- **Tool support**: Easy to add custom tools (like policy RAG lookup)
- **Production-ready**: OpenTelemetry tracing, error handling out of the box

### Cloud Run: The Perfect Compute Layer

I used **all three Cloud Run resource types** to maximize scalability and cost-efficiency:

**1. Cloud Run Services (3 deployed)**
- **Orchestrator API**: Public HTTPS endpoint for receipt uploads, SSE streaming
- **Synthesis Service**: Private service for final aggregation
- **Frontend**: Next.js UI for demo/production use

**2. Cloud Run Worker Pools (4 deployed)**
- Each agent runs as a Worker Pool pulling from Pub/Sub
- Auto-scales from 0 to 10 instances per agent
- Only pay when processing messages
- Perfect for parallel, independent workloads

**3. Cloud Run Job (1 deployed)**
- Nightly audit report generation (CSV exports)
- Runs on schedule via Cloud Scheduler
- Processes Firestore data → uploads to GCS

## Implementation Highlights

### 1. ADK Agent Definition (Simplified)

```python
from google.adk.agents import Agent

policy_agent = Agent(
    name="policy_checker",
    model="gemini-1.5-pro",
    instruction="""You are a policy compliance expert. 
    Check expense claims against company policy and cite specific rules.""",
    tools=[policy_rag_tool],
)

# ADK handles orchestration, retries, and tracing automatically
result = policy_agent.run({"receipt_data": extracted_data})
```

### 2. Pub/Sub for Agent Coordination

Each agent publishes to the next stage:
```python
publisher.publish(
    topic_path,
    json.dumps(result).encode(),
    expense_id=expense_id,
)
```

Worker Pools automatically pull and process messages—no manual scaling!

### 3. Firestore for Persistence

All audit trails stored in Firestore:
```python
audit_ref = db.collection("audits").document(expense_id)
audit_ref.set({
    "employee_id": employee_id,
    "receipt_data": extracted,
    "policy_findings": findings,
    "status": "completed",
    "timestamp": firestore.SERVER_TIMESTAMP,
})
```

### 4. Real-Time Streaming (SSE)

Frontend receives live updates as agents process:
```python
async def stream_audit_progress(expense_id: str):
    async for update in listen_to_pubsub(expense_id):
        yield f"data: {json.dumps(update)}\n\n"
```

## Deployment Experience

Deploying to Cloud Run was incredibly smooth:

```bash
# Build and push all images
gcloud builds submit --tag us-central1-docker.pkg.dev/$PROJECT_ID/auditai/orchestrator:latest ./backend

# Deploy service
gcloud run deploy orchestrator-api \
  --image us-central1-docker.pkg.dev/$PROJECT_ID/auditai/orchestrator:latest \
  --region us-central1 \
  --allow-unauthenticated
```

**Total deployment time from code to production: ~15 minutes** for 5 images, 3 services, 4 worker pools, and 1 job!

### Infrastructure as Code (Cloud Run YAML)

```yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: orchestrator-api
spec:
  template:
    spec:
      serviceAccountName: svc-orchestrator@PROJECT_ID.iam.gserviceaccount.com
      containers:
      - image: us-central1-docker.pkg.dev/PROJECT_ID/auditai/orchestrator:latest
        env:
        - name: PROJECT_ID
          value: PROJECT_ID
        - name: REGION
          value: us-central1
```

## Demo Results

**Test Case: Valid Taxi Receipt**

1. **Upload**: Uber receipt for $39.53 (airport → office)
2. **Extraction** (3s): `{merchant: "Uber", amount: 39.53, category: "transportation"}`
3. **Policy Check** (2s): ✅ Within taxi limit ($75), business purpose valid
4. **Anomaly Check** (1s): ✅ No duplicates, typical route
5. **Synthesis** (1s): **APPROVED** — All checks passed

**Total processing time: 7 seconds** (vs. 10-15 minutes manual review)

**Test Case: Invalid Alcohol Receipt**

1. **Upload**: Bar receipt with $42 in alcoholic beverages
2. **Extraction**: Correctly identifies beer, wine, vodka
3. **Policy Check**: ❌ **Violation** — "Alcoholic beverages NOT reimbursable under any circumstances" (cited from policy doc)
4. **Remediation**: Suggests splitting food ($30) from alcohol ($42), resubmitting food only
5. **Synthesis**: **REJECTED** — Policy violation with remediation path

## Production Readiness

AuditAI isn't just a demo—it's production-ready:

- ✅ **Error handling**: DLQs, exponential backoff, idempotency keys
- ✅ **Observability**: Cloud Logging, Traces, Error Reporting integrated
- ✅ **Security**: IAM service accounts, least-privilege roles, Secret Manager
- ✅ **Scalability**: Auto-scales 0→10 per agent, handles 1000s of concurrent audits
- ✅ **Cost-efficient**: Pay only when processing (Worker Pools scale to zero)

## Lessons Learned

### 1. ADK Simplifies Multi-Agent Orchestration
Before ADK, I would have written complex orchestration logic. With ADK:
- Agent definitions are 10-20 lines
- Automatic retry and error handling
- Native Gemini integration (no API key management)

### 2. Worker Pools Are Perfect for Agents
Cloud Run Worker Pools are ideal for agent workloads:
- Pull-based (no HTTP overhead)
- Independent scaling per agent
- Cost-effective (scale to zero)

### 3. Pub/Sub Decouples Everything
Using Pub/Sub topics between agents:
- Agents can be updated independently
- Easy to add new agents (just subscribe to a topic)
- Built-in retry and DLQ support

## Cost Analysis

**Monthly cost for 1000 expense audits/month:**

- **Orchestrator API**: ~$5 (mostly idle, ~10s per request)
- **Worker Pools (4)**: ~$15 (5-10s processing per audit)
- **Synthesis**: ~$3 (1-2s per audit)
- **Frontend**: ~$2 (mostly static)
- **Job**: ~$1 (monthly run)
- **Gemini API**: ~$20 (Flash + Pro calls)
- **Firestore**: ~$5 (reads/writes)
- **Pub/Sub**: ~$2

**Total: ~$53/month** for 1000 audits = **$0.05 per audit**

Compare that to manual review at $50/hour (15 min/audit) = **$12.50 per audit**

**ROI: 250x cost reduction** 🎯

## Next Steps & Future Enhancements

1. **Receipt forgery detection**: Use Gemini Vision to detect image tampering
2. **Cross-employee duplicate detection**: Vector embeddings to find similar receipts
3. **Auto-learned policies**: Continuously improve from human approvals
4. **Vendor risk screening**: Check merchants against sanctions lists
5. **Carbon footprint tagging**: Track environmental impact of spend

## Try It Yourself

**Live Demo**: [frontend-URL]

**Code**: [GitHub repo]

**Deploy in 15 minutes**:
```bash
git clone [repo]
cd auditai
./deploy.sh --project YOUR_PROJECT_ID
```

## Conclusion

Building AuditAI taught me that **multi-agent systems are the future of automation**. With Google ADK and Cloud Run, you can:

- Build sophisticated AI workflows in hours (not weeks)
- Deploy with production-grade reliability
- Scale effortlessly with usage
- Keep costs low with serverless auto-scaling

If you're building AI agents, **ADK + Cloud Run** is the stack to beat.

---

**Built for**: Google Cloud Run Hackathon 2025
**Tech Stack**: Google ADK, Gemini 2.5 Flash Preview / Flash-Lite Preview, Cloud Run (Services + Worker Pools + Jobs), Pub/Sub, Firestore, Next.js
**Author**: [Your Name]
**GitHub**: [repo link]
**Demo**: [frontend URL]

*Have questions? Drop a comment or reach out on [Twitter/LinkedIn]!*

---

### Hashtags for Social Media
#CloudRunHackathon #GoogleCloud #CloudRun #AIAgents #ADK #Gemini #ServerlessAI #MultiAgentSystems #FinTech #Automation

---

## Screenshots to Include

1. **Architecture Diagram**: From `docs/ARCHITECTURE_AUDITAI.md`
2. **Frontend Upload**: Screenshot of receipt upload UI
3. **Real-Time Processing**: SSE stream showing agent progress
4. **Audit Result**: Final verdict with policy citations
5. **Cloud Run Console**: Show 3 services + 4 worker pools + 1 job

---

**Word Count**: ~1,800 words
**Reading Time**: ~8 minutes
**Target Audience**: Developers interested in AI agents, Cloud Run, multi-agent systems
**SEO Keywords**: Google Cloud Run, Agent Development Kit, ADK, Multi-agent systems, Gemini AI, Serverless AI, Expense automation

