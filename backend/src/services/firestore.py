import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore


_client: Optional[firestore.Client] = None


def _get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=os.getenv("PROJECT_ID"))
    return _client


def init_expense_doc(expense_id: str, submitter: Dict[str, Any], gcs_uri: str) -> None:
    """Initialize expense document in Firestore (synchronous)"""
    doc_ref = _get_client().collection("expenses").document(expense_id)
    doc_ref.set(
        {
            "status": "INGESTED",
            "version": 1,
            "submitter": submitter,
            "gcsUri": gcs_uri,
            "createdAt": datetime.now(timezone.utc),
            "findings": {},
            "auditLog": [
                {"actor": "orchestrator", "action": "INGESTED", "ts": datetime.now(timezone.utc).isoformat()}
            ],
        }
    )


def get_expense_doc(expense_id: str) -> Optional[Dict[str, Any]]:
    """Get expense document from Firestore (synchronous)"""
    doc_ref = _get_client().collection("expenses").document(expense_id)
    snap = doc_ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    data["id"] = expense_id
    return data


def update_expense_status(expense_id: str, status: str) -> None:
    """Update expense status in Firestore (synchronous)"""
    doc_ref = _get_client().collection("expenses").document(expense_id)
    doc_ref.update(
        {
            "status": status,
            "version": firestore.Increment(1),
            "updatedAt": datetime.now(timezone.utc),
            "auditLog": firestore.ArrayUnion(
                [{"actor": "orchestrator", "action": f"STATUS:{status}", "ts": datetime.now(timezone.utc).isoformat()}]
            ),
        }
    )


