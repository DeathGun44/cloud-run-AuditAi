import os
import time
import uuid
import json
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from starlette.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .services.gcs import upload_file_to_gcs
from .services.firestore import (
    init_expense_doc,
    get_expense_doc,
    update_expense_status,
)
from .services.pubsub import publish_event


PROJECT_ID = os.getenv("PROJECT_ID")
REGION = os.getenv("REGION", "us-central1")
RECEIPTS_BUCKET = os.getenv("RECEIPTS_BUCKET", f"{PROJECT_ID}-auditai-receipts")
TOPIC_INGESTED = os.getenv("TOPIC_INGESTED", "expenses.ingested")


class SubmitResponse(BaseModel):
    expenseId: str
    gcsUri: str
    status: str


app = FastAPI(title="AuditAI Orchestrator API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"message": "AuditAI Orchestrator API", "version": "0.1.0", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "orchestrator", "project": PROJECT_ID}


@app.post("/api/expenses", response_model=SubmitResponse)
async def submit_expense(
    file: UploadFile = File(...),
    employeeId: Optional[str] = Form(default=None),
    department: Optional[str] = Form(default=None),
):
    if not PROJECT_ID:
        raise HTTPException(status_code=500, detail="PROJECT_ID not configured")
    # 1) Upload to GCS
    expense_id = str(uuid.uuid4())
    object_name = f"{expense_id}/{file.filename}"
    gcs_uri = await upload_file_to_gcs(RECEIPTS_BUCKET, object_name, file)

    # 2) Create Firestore doc
    init_expense_doc(
        expense_id=expense_id,
        submitter={"id": employeeId, "department": department},
        gcs_uri=gcs_uri,
    )

    # 3) Publish Pub/Sub event to start pipeline
    publish_event(
        topic=TOPIC_INGESTED,
        message={
            "expenseId": expense_id,
            "gcsUri": gcs_uri,
            "employeeId": employeeId,
            "submissionTime": int(time.time()),
        },
    )

    return SubmitResponse(expenseId=expense_id, gcsUri=gcs_uri, status="INGESTED")


@app.get("/api/expenses/{expense_id}")
async def get_expense(expense_id: str):
    doc = get_expense_doc(expense_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(jsonable_encoder(doc))


@app.get("/api/expenses/{expense_id}/stream")
async def stream_expense(expense_id: str):
    """Stream expense updates via Server-Sent Events"""
    import asyncio
    
    async def event_generator() -> AsyncGenerator[str, None]:
        last_version: int = 0
        max_iterations = 120  # Max 2 minutes
        iteration = 0
        completed = False
        
        while iteration < max_iterations:
            iteration += 1
            doc = get_expense_doc(expense_id)
            
            if not doc:
                yield f"data: {json.dumps({'error': 'NOT_FOUND'})}\n\n"
                completed = True
                break
            
            current_version = doc.get('version', 0)
            if current_version > last_version:
                last_version = current_version
                # Send the full document update
                yield f"data: {json.dumps(doc, default=str)}\n\n"
            
            # Check if processing is complete
            status = doc.get('status', '')
            if status in ['APPROVED', 'REJECTED', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW']:
                yield f"data: {json.dumps({'status': 'DONE', 'finalStatus': status})}\n\n"
                completed = True
                break
            
            # Wait before next poll
            sleep_seconds = 1
            if status and status not in ['INGESTED', 'EXTRACTING', 'EXTRACTED']:
                sleep_seconds = 2
            await asyncio.sleep(sleep_seconds)
        
        if not completed:
            yield f"data: {json.dumps({'status': 'TIMEOUT'})}\n\n"

    return EventSourceResponse(event_generator())


