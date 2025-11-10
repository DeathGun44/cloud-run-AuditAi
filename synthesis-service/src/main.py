import os
from fastapi import FastAPI, HTTPException
from google.cloud import firestore

app = FastAPI(title="AuditAI Synthesis Service")
fs = firestore.Client(project=os.getenv("PROJECT_ID"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/internal/synthesize/{expense_id}")
def synthesize(expense_id: str):
    ref = fs.collection("expenses").document(expense_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "Not found")
    data = snap.to_dict()
    status = data.get("final", {}).get("status") or data.get("status")
    # For now, no-op aggregation; extend with richer synthesis if needed
    return {"expenseId": expense_id, "status": status}


