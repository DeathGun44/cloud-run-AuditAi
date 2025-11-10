"""
AuditAI Worker Pool - Real Multi-Agent Processing
Handles extraction, policy, anomaly, and remediation via Pub/Sub
"""

import os
import sys
import json
import time
import logging
import re
import random
import threading
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from google.cloud import pubsub_v1, firestore, storage
from google.cloud.firestore_v1 import FieldFilter
from google.cloud.pubsub_v1.types import FlowControl
from google.api_core import exceptions
from google.api_core.exceptions import InvalidArgument
import vertexai
from vertexai.generative_models import GenerativeModel, Part, GenerationConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Environment variables
PROJECT_ID = os.getenv("PROJECT_ID")
REGION = os.getenv("REGION", "us-central1")
AGENT_TYPE = os.getenv("AGENT_TYPE", "extraction")
SUBSCRIPTION = os.getenv("SUBSCRIPTION")
TOPIC_OUT = os.getenv("TOPIC_OUT")
RECEIPTS_BUCKET = os.getenv("RECEIPTS_BUCKET")
POLICY_BUCKET = os.getenv("POLICY_BUCKET")
MODEL_LOCATION = os.getenv("MODEL_LOCATION", REGION)
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "gemini-2.5-flash-preview-09-2025")
EXTRACTION_MODEL_LOCATION = os.getenv("EXTRACTION_MODEL_LOCATION", MODEL_LOCATION)
POLICY_MODEL = os.getenv("POLICY_MODEL", "gemini-2.5-flash-lite-preview-09-2025")
POLICY_MODEL_LOCATION = os.getenv("POLICY_MODEL_LOCATION", MODEL_LOCATION)
AGENT_QPS = float(os.getenv("AGENT_QPS", "2"))

# Initialize clients
vertexai.init(project=PROJECT_ID, location=MODEL_LOCATION)
subscriber = pubsub_v1.SubscriberClient()
publisher = pubsub_v1.PublisherClient()
db = firestore.Client(project=PROJECT_ID)
gcs_client = storage.Client(project=PROJECT_ID)


MAX_ITEM_COUNT = 12
MAX_STRING_LENGTH = 256
MAX_AUDIT_LOG_ENTRIES = 50
MAX_AUDIT_FIELD_LENGTH = 512
MAX_INFLIGHT_MESSAGES = int(os.getenv("MAX_INFLIGHT_MESSAGES", "4"))
MAX_INFLIGHT_BYTES = int(os.getenv("MAX_INFLIGHT_BYTES", str(4 * 1024 * 1024)))


def _clean_string(value: Optional[Any], max_len: int = MAX_STRING_LENGTH) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    value = str(value).strip()
    if len(value) > max_len:
        return value[: max_len - 3] + "..."
    return value


def _parse_amount(value: Optional[Any]) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]", "", value.replace(",", ""))
        if cleaned in {"", "-", ".", "-."}:
            return 0.0
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


def _sanitize_items(items: Any) -> list[Dict[str, Any]]:
    sanitized_items: list[Dict[str, Any]] = []
    if not isinstance(items, list):
        return sanitized_items
    for item in items:
        if len(sanitized_items) >= MAX_ITEM_COUNT:
            break
        if not isinstance(item, dict):
            continue
        name = _clean_string(item.get("name"))
        if not name:
            continue
        price = round(_parse_amount(item.get("price")), 2)
        quantity = round(_parse_amount(item.get("quantity")) or 1.0, 2)
        sanitized_items.append(
            {
                "name": name,
                "price": price,
                "quantity": quantity,
            }
        )
    return sanitized_items


def sanitize_extracted_data(
    raw_extraction: Dict[str, Any],
    *,
    source_type: str,
    raw_text_snippet: Optional[str] = None,
) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {
        "merchant": _clean_string(raw_extraction.get("merchant") or "Unknown"),
        "total_amount": round(_parse_amount(raw_extraction.get("total_amount")), 2),
        "currency": _clean_string(raw_extraction.get("currency") or "USD", max_len=16),
        "date": _clean_string(raw_extraction.get("date"), max_len=32),
        "category": _clean_string(raw_extraction.get("category") or "other", max_len=64),
        "payment_method": _clean_string(raw_extraction.get("payment_method"), max_len=64),
        "has_alcohol": bool(raw_extraction.get("has_alcohol", False)),
        "business_purpose": _clean_string(raw_extraction.get("business_purpose"), max_len=400),
        "source_type": source_type,
        "model": EXTRACTION_MODEL,
    }

    items = _sanitize_items(raw_extraction.get("items"))
    if items:
        sanitized["items"] = items

    tax_amount = raw_extraction.get("tax") or raw_extraction.get("tax_amount")
    tax_value = _parse_amount(tax_amount)
    if tax_value:
        sanitized["tax_amount"] = round(tax_value, 2)

    subtotal = raw_extraction.get("subtotal")
    subtotal_value = _parse_amount(subtotal)
    if subtotal_value:
        sanitized["subtotal"] = round(subtotal_value, 2)

    if raw_text_snippet:
        sanitized["raw_text_snippet"] = _clean_string(raw_text_snippet, max_len=400)

    # Remove empty strings to keep Firestore doc small
    sanitized = {
        key: value
        for key, value in sanitized.items()
        if not (isinstance(value, str) and value == "")
    }
    sanitized["summary"] = f"{sanitized.get('merchant', 'Unknown')} ${sanitized.get('total_amount', 0.0):.2f}"
    return sanitized


def _parse_json_response(response_text: str) -> Dict[str, Any]:
    """Best-effort parsing for Gemini JSON responses with optional thinking text."""
    text = response_text.strip()
    if not text:
        raise ValueError("Model returned an empty response.")

    # Remove fenced code blocks
    if text.startswith("```"):
        segments = text.split("```")
        text = ""
        for segment in segments:
            seg = segment.strip()
            if not seg:
                continue
            if seg.lower().startswith("json"):
                newline_idx = seg.find("\n")
                seg = seg[newline_idx + 1 :] if newline_idx != -1 else ""
            text += seg + "\n"
        text = text.strip()

    # Strip hidden thinking tags (Gemini 2.5+)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()

    # Direct attempt
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Locate first JSON object by brace matching
    start = text.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(text)):
            char = text[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : idx + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    raise ValueError(f"Unable to parse JSON from model response: {response_text}")


def _resolve_model_name(model_id: str, *, location: Optional[str] = None) -> str:
    if not model_id:
        raise ValueError("Model ID must be provided.")
    if model_id.startswith("projects/"):
        return model_id
    target_location = location or MODEL_LOCATION
    if model_id.startswith("publishers/"):
        return f"projects/{PROJECT_ID}/locations/{target_location}/{model_id}"
    return f"projects/{PROJECT_ID}/locations/{target_location}/publishers/google/models/{model_id}"


class QPSTokenBucket:
    def __init__(self, qps: float):
        self.capacity = max(qps, 0.1)
        self.tokens = self.capacity
        self.rate = qps
        self.lock = threading.Lock()
        self.last = time.perf_counter()

    def take(self, cost: float = 1.0) -> None:
        while True:
            with self.lock:
                now = time.perf_counter()
                elapsed = now - self.last
                self.last = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
            time.sleep(0.02)


def call_with_retry(
    fn,
    *,
    max_attempts: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
):
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (
            exceptions.ResourceExhausted,
            exceptions.ServiceUnavailable,
            exceptions.DeadlineExceeded,
        ) as exc:
            retry_after = None
            for attr in ("retry_after", "Retry-After"):
                if hasattr(exc, attr):
                    retry_after = getattr(exc, attr)
                    break

            if retry_after:
                try:
                    sleep_seconds = float(retry_after)
                except (TypeError, ValueError):
                    sleep_seconds = delay
            else:
                jitter = random.uniform(0, delay * 0.3)
                sleep_seconds = min(delay + jitter, max_delay)

            logger.warning(
                "Transient Vertex AI error on attempt %d/%d: %s. Sleeping %.2fs",
                attempt,
                max_attempts,
                exc,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
            delay = min(delay * 2, max_delay)
            continue
        except Exception:
            raise
    # Final attempt outside loop
    return fn()


token_bucket = QPSTokenBucket(AGENT_QPS)


def _get_expense_doc_data(expense_id: str) -> Optional[Dict[str, Any]]:
    try:
        snapshot = db.collection("expenses").document(expense_id).get()
    except Exception as exc:
        logger.error("Failed to load expense %s: %s", expense_id, exc)
        return None
    if not snapshot.exists:
        return None
    return snapshot.to_dict() or {}


def _stage_already_completed(doc_data: Dict[str, Any], stage_key: str) -> bool:
    findings = doc_data.get("findings", {})
    stage_data = findings.get(stage_key)
    if isinstance(stage_data, dict):
        return bool(stage_data)
    return stage_data is not None


def _is_final_status(status: Optional[str]) -> bool:
    return status in {"APPROVED", "REJECTED", "FAILED", "COMPLETED", "NEEDS_REVIEW"}


def _sanitize_audit_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}
    for key, value in entry.items():
        if isinstance(value, str):
            sanitized[key] = _clean_string(value, max_len=MAX_AUDIT_FIELD_LENGTH)
        else:
            sanitized[key] = value
    sanitized.setdefault("ts", datetime.now(timezone.utc).isoformat())
    return sanitized


def _format_error_message(error: Exception | str) -> str:
    return _clean_string(str(error), max_len=MAX_AUDIT_FIELD_LENGTH)


def update_expense_doc(
    expense_id: str,
    updates: Dict[str, Any],
    audit_entry: Optional[Dict[str, Any]] = None,
) -> None:
    """Atomically update an expense document while maintaining a bounded audit log."""
    doc_ref = db.collection("expenses").document(expense_id)
    def _apply_update() -> None:
        snapshot = doc_ref.get()
        if not snapshot.exists:
            logger.error("Expense document %s not found while updating.", expense_id)
            return

        doc_data = snapshot.to_dict() or {}
        merged_updates = dict(updates)

        if audit_entry:
            audit_log = doc_data.get("auditLog", [])
            audit_log.append(_sanitize_audit_entry(audit_entry))
            audit_log = audit_log[-MAX_AUDIT_LOG_ENTRIES:]
            merged_updates["auditLog"] = audit_log

        doc_ref.update(merged_updates)

    try:
        _apply_update()
    except InvalidArgument as exc:
        message = str(exc)
        if "exceeds the maximum allowed size" not in message:
            raise

        logger.warning("Firestore document oversized for %s. Compacting and retrying.", expense_id)
        _compact_expense_document(doc_ref)
        _apply_update()


def _compact_expense_document(doc_ref: firestore.DocumentReference) -> None:
    snapshot = doc_ref.get()
    if not snapshot.exists:
        return

    data = snapshot.to_dict() or {}
    compact_findings = data.get("findings", {})
    compact_audit = [
        _sanitize_audit_entry(entry)
        for entry in data.get("auditLog", [])[-10:]
    ]

    compact_doc = {
        "status": data.get("status"),
        "version": data.get("version", 0),
        "submitter": data.get("submitter"),
        "gcsUri": data.get("gcsUri"),
        "createdAt": data.get("createdAt"),
        "findings": compact_findings,
        "auditLog": compact_audit,
        "error": _clean_string(data.get("error"), max_len=MAX_AUDIT_FIELD_LENGTH),
    }

    doc_ref.set(compact_doc, merge=False)


def load_policy_document() -> str:
    """Load company policy from GCS"""
    try:
        bucket = gcs_client.bucket(POLICY_BUCKET)
        blob = bucket.blob("expense-policy.txt")
        return blob.download_as_text()
    except Exception as e:
        logger.error(f"Failed to load policy: {e}")
        return ""


def extraction_agent(expense_id: str, gcs_uri: str) -> Dict[str, Any]:
    """Extract structured data from receipt using Gemini Vision"""
    logger.info(f"Extraction agent processing {expense_id}")
    
    try:
        update_expense_doc(
            expense_id,
            {
                "status": "EXTRACTING",
                "version": firestore.Increment(1),
            },
            {
                "actor": "extraction_agent",
                "action": "STARTED",
            },
        )
        
        # Download receipt content
        bucket_name = gcs_uri.replace("gs://", "").split("/")[0]
        blob_path = "/".join(gcs_uri.replace("gs://", "").split("/")[1:])
        bucket = gcs_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        
        # Determine if it's an image or text
        content_type = blob.content_type or ""
        is_image = content_type.startswith("image/") or blob_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))
        
        # Initialize Gemini model
        model = GenerativeModel(_resolve_model_name(EXTRACTION_MODEL, location=EXTRACTION_MODEL_LOCATION))
        
        raw_text_snippet: Optional[str] = None
        source_type = "image" if is_image else "text"

        if is_image:
            # Use Vision for image receipts
            image_part = Part.from_uri(gcs_uri, mime_type=content_type or "image/jpeg")
            prompt = """
            Analyze this receipt image and extract the following information as JSON:
            {
              "merchant": "merchant name",
              "total_amount": 123.45,
              "date": "YYYY-MM-DD",
              "category": "meals|transportation|lodging|office_supplies|other",
              "items": [{"name": "item", "price": 12.34}],
              "payment_method": "card type",
              "has_alcohol": true/false,
              "business_purpose": "extracted from receipt if present"
            }
            
            Be precise. If alcohol beverages are listed (beer, wine, vodka, etc.), set has_alcohol=true.
            Return ONLY valid JSON, no markdown formatting.
            """
            
            token_bucket.take()
            response = call_with_retry(
                lambda: model.generate_content(
                    [prompt, image_part],
                    generation_config=GenerationConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                        max_output_tokens=512,
                    ),
                )
            )
        else:
            # Use text processing for text receipts
            receipt_text = blob.download_as_text()
            raw_text_snippet = receipt_text[:400]
            prompt = f"""
            Analyze this receipt text and extract the following information as JSON:
            {{
              "merchant": "merchant name",
              "total_amount": 123.45,
              "date": "YYYY-MM-DD",
              "category": "meals|transportation|lodging|office_supplies|other",
              "items": [{{"name": "item", "price": 12.34}}],
              "payment_method": "card type",
              "has_alcohol": true/false,
              "business_purpose": "extracted from receipt if present"
            }}
            
            Receipt Text:
            {receipt_text}
            
            Be precise. If alcohol beverages are mentioned, set has_alcohol=true.
            Return ONLY valid JSON, no markdown formatting.
            """
            
            token_bucket.take()
            response = call_with_retry(
                lambda: model.generate_content(
                    prompt,
                    generation_config=GenerationConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                        max_output_tokens=512,
                    ),
                )
            )
        
        # Parse Gemini response
        response_text = response.text or ""
        extracted_data = _parse_json_response(response_text)
        sanitized_extraction = sanitize_extracted_data(
            extracted_data,
            source_type=source_type,
            raw_text_snippet=raw_text_snippet,
        )
        
        # Update Firestore with extracted data
        update_expense_doc(
            expense_id,
            {
                "status": "EXTRACTED",
                "version": firestore.Increment(1),
                "findings": {
                    "extraction": sanitized_extraction
                },
            },
            {
                "actor": "extraction_agent",
                "action": "COMPLETED",
            },
        )
        
        logger.info(
            "Extraction complete: merchant=%s total=%.2f source=%s",
            sanitized_extraction.get("merchant"),
            sanitized_extraction.get("total_amount", 0.0),
            source_type,
        )
        return sanitized_extraction
        
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        error_message = _format_error_message(e)
        update_expense_doc(
            expense_id,
            {
                "status": "EXTRACTION_FAILED",
                "error": error_message,
            },
            {
                "actor": "extraction_agent",
                "action": "FAILED",
                "error": error_message,
            },
        )
        raise


def policy_agent(expense_id: str, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    """Check policy compliance using Gemini Pro + ADK"""
    logger.info(f"Policy agent processing {expense_id}")
    
    try:
        # Update status
        update_expense_doc(
            expense_id,
            {
                "status": "CHECKING_POLICY",
                "version": firestore.Increment(1),
            },
            {
                "actor": "policy_agent",
                "action": "STARTED",
            },
        )
        
        # Load company policy
        policy_text = load_policy_document()
        
        # Use Gemini Pro for policy reasoning
        model = GenerativeModel(_resolve_model_name(POLICY_MODEL, location=POLICY_MODEL_LOCATION))
        
        merchant = extracted_data.get("merchant", "Unknown")
        amount = extracted_data.get("total_amount", 0)
        category = extracted_data.get("category", "other")
        has_alcohol = extracted_data.get("has_alcohol", False)
        items = extracted_data.get("items", [])
        
        prompt = f"""
        You are a corporate expense policy compliance expert.
        
        COMPANY POLICY:
        {policy_text}
        
        EXPENSE TO EVALUATE:
        - Merchant: {merchant}
        - Amount: ${amount}
        - Category: {category}
        - Has Alcohol: {has_alcohol}
        - Items: {json.dumps(items)}
        
        TASK:
        1. Determine if this expense is compliant with the policy
        2. Cite specific policy sections (e.g., "§2.1", "Policy Section 3")
        3. Explain reasoning
        4. If non-compliant, specify the violation
        
        Return as JSON:
        {{
          "compliant": true/false,
          "verdict": "APPROVED|REJECTED|NEEDS_REVIEW",
          "violations": ["list of violations"],
          "citations": ["§2.1: Meals limit", ...],
          "reasoning": "detailed explanation",
          "recommended_action": "what to do next"
        }}
        
        Return ONLY valid JSON.
        """
        
        token_bucket.take()
        response = call_with_retry(
            lambda: model.generate_content(
                prompt,
                generation_config=GenerationConfig(
                    temperature=0.2,
                    top_p=0.95,
                    max_output_tokens=512,
                )
            )
        )
        
        # Parse response
        response_text = response.text or ""
        policy_result = _parse_json_response(response_text)
        
        # Update Firestore
        update_expense_doc(
            expense_id,
            {
                "status": "POLICY_CHECKED",
                "version": firestore.Increment(1),
                "findings.policy": policy_result,
            },
            {
                "actor": "policy_agent",
                "action": "COMPLETED",
                "verdict": policy_result.get("verdict"),
            },
        )
        
        logger.info(f"Policy check complete: {policy_result.get('verdict')}")
        return policy_result
        
    except Exception as e:
        logger.error(f"Policy check failed: {e}")
        error_message = _format_error_message(e)
        update_expense_doc(
            expense_id,
            {
                "status": "POLICY_CHECK_FAILED",
                "error": error_message,
            },
            {
                "actor": "policy_agent",
                "action": "FAILED",
                "error": error_message,
            },
        )
        raise


def anomaly_agent(expense_id: str, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    """Detect anomalies and fraud patterns"""
    logger.info(f"Anomaly agent processing {expense_id}")
    
    try:
        update_expense_doc(
            expense_id,
            {
                "status": "CHECKING_ANOMALIES",
                "version": firestore.Increment(1),
            },
            {
                "actor": "anomaly_agent",
                "action": "STARTED",
            },
        )
        
        anomalies = []
        risk_score = 0
        
        amount = extracted_data.get("total_amount", 0)
        merchant = extracted_data.get("merchant", "")
        category = extracted_data.get("category", "")
        
        # Check for duplicate submissions (same merchant, similar amount, recent)
        recent_cutoff = datetime.now(timezone.utc).timestamp() - (7 * 24 * 3600)  # 7 days
        similar_expenses = db.collection("expenses").where(
            filter=FieldFilter("createdAt", ">=", datetime.fromtimestamp(recent_cutoff, timezone.utc))
        ).stream()
        
        for expense in similar_expenses:
            exp_data = expense.to_dict()
            exp_findings = exp_data.get("findings", {})
            exp_extraction = exp_findings.get("extraction", {})
            
            if expense.id != expense_id:
                if (exp_extraction.get("merchant") == merchant and 
                    abs(exp_extraction.get("total_amount", 0) - amount) < 5):
                    anomalies.append(f"Potential duplicate: Similar expense to {merchant} for ${amount}")
                    risk_score += 30
        
        # Check for unusually high amounts
        category_limits = {
            "meals": 100,
            "transportation": 200,
            "office_supplies": 500,
            "lodging": 300
        }
        
        limit = category_limits.get(category, 200)
        if amount > limit:
            anomalies.append(f"High amount for {category}: ${amount} exceeds typical limit ${limit}")
            risk_score += 20
        
        # Check for round numbers (fraud indicator)
        if amount % 10 == 0 and amount > 50:
            anomalies.append("Round number amount may indicate estimate vs actual receipt")
            risk_score += 10
        
        result = {
            "anomalies_detected": len(anomalies) > 0,
            "anomalies": anomalies,
            "risk_score": min(risk_score, 100),
            "risk_level": "high" if risk_score > 50 else "medium" if risk_score > 20 else "low"
        }
        
        update_expense_doc(
            expense_id,
            {
                "status": "ANOMALY_CHECKED",
                "version": firestore.Increment(1),
                "findings.anomaly": result,
            },
            {
                "actor": "anomaly_agent",
                "action": "COMPLETED",
                "risk_score": risk_score,
            },
        )
        
        logger.info(f"Anomaly check complete: {len(anomalies)} anomalies, risk={risk_score}")
        return result
        
    except Exception as e:
        logger.error(f"Anomaly check failed: {e}")
        error_message = _format_error_message(e)
        update_expense_doc(
            expense_id,
            {
                "status": "ANOMALY_CHECK_FAILED",
                "error": error_message,
            },
            {
                "actor": "anomaly_agent",
                "action": "FAILED",
                "error": error_message,
            },
        )
        raise


def remediation_agent(expense_id: str, findings: Dict[str, Any]) -> Dict[str, Any]:
    """Generate remediation recommendations"""
    logger.info(f"Remediation agent processing {expense_id}")
    
    try:
        update_expense_doc(
            expense_id,
            {
                "status": "GENERATING_REMEDIATION",
                "version": firestore.Increment(1),
            },
            {
                "actor": "remediation_agent",
                "action": "STARTED",
            },
        )
        
        policy_result = findings.get("policy", {})
        anomaly_result = findings.get("anomaly", {})
        extraction = findings.get("extraction", {})
        
        recommendations = []
        
        # Generate remediation for policy violations
        if not policy_result.get("compliant", True):
            violations = policy_result.get("violations", [])
            for violation in violations:
                if "alcohol" in violation.lower():
                    recommendations.append({
                        "issue": "Alcohol not reimbursable",
                        "action": "Separate food items from alcohol and resubmit food portion only",
                        "priority": "high"
                    })
                elif "limit" in violation.lower() or "exceeds" in violation.lower():
                    recommendations.append({
                        "issue": "Amount exceeds policy limit",
                        "action": "Obtain manager pre-approval or split into multiple receipts if multi-day",
                        "priority": "medium"
                    })
                else:
                    recommendations.append({
                        "issue": violation,
                        "action": "Review policy documentation and resubmit with required information",
                        "priority": "medium"
                    })
        
        # Handle anomalies
        if anomaly_result.get("anomalies_detected"):
            for anomaly in anomaly_result.get("anomalies", []):
                if "duplicate" in anomaly.lower():
                    recommendations.append({
                        "issue": "Possible duplicate submission",
                        "action": "Verify this is not a duplicate. If legitimate, add note explaining why.",
                        "priority": "high"
                    })
                elif "round number" in anomaly.lower():
                    recommendations.append({
                        "issue": "Receipt shows round number",
                        "action": "Ensure you have the itemized receipt, not just an estimate",
                        "priority": "low"
                    })
        
        result = {
            "needs_remediation": len(recommendations) > 0,
            "recommendations": recommendations,
            "auto_approvable": len(recommendations) == 0 and policy_result.get("compliant", False)
        }
        
        update_expense_doc(
            expense_id,
            {
                "status": "REMEDIATION_COMPLETE",
                "version": firestore.Increment(1),
                "findings.remediation": result,
            },
            {
                "actor": "remediation_agent",
                "action": "COMPLETED",
                "recommendations_count": len(recommendations),
            },
        )
        
        logger.info(f"Remediation complete: {len(recommendations)} recommendations")
        return result
        
    except Exception as e:
        logger.error(f"Remediation failed: {e}")
        error_message = _format_error_message(e)
        update_expense_doc(
            expense_id,
            {
                "status": "REMEDIATION_FAILED",
                "error": error_message,
            },
            {
                "actor": "remediation_agent",
                "action": "FAILED",
                "error": error_message,
            },
        )
        raise


def synthesis_agent(expense_id: str) -> Dict[str, Any]:
    """Synthesize all findings into final verdict"""
    logger.info(f"Synthesis agent processing {expense_id}")
    
    try:
        # Get all findings
        doc = db.collection("expenses").document(expense_id).get()
        if not doc.exists:
            raise ValueError("Expense document not found")
        
        data = doc.to_dict()
        findings = data.get("findings", {})
        
        extraction = findings.get("extraction", {})
        policy = findings.get("policy", {})
        anomaly = findings.get("anomaly", {})
        remediation = findings.get("remediation", {})
        
        # Determine final verdict
        if not policy.get("compliant", False):
            final_verdict = "REJECTED"
            confidence = 95
            summary = f"Policy violation: {', '.join(policy.get('violations', ['Unknown violation']))}"
        elif anomaly.get("risk_level") == "high":
            final_verdict = "NEEDS_REVIEW"
            confidence = 70
            summary = f"High risk detected: {', '.join(anomaly.get('anomalies', []))}"
        elif remediation.get("needs_remediation"):
            final_verdict = "NEEDS_REVIEW"
            confidence = 80
            summary = f"{len(remediation.get('recommendations', []))} issues require attention"
        else:
            final_verdict = "APPROVED"
            confidence = 98
            summary = f"All checks passed for {extraction.get('merchant')} - ${extraction.get('total_amount')}"
        
        result = {
            "verdict": final_verdict,
            "confidence": confidence,
            "summary": summary,
            "amount": extraction.get("total_amount", 0),
            "merchant": extraction.get("merchant", "Unknown"),
            "completedAt": datetime.now(timezone.utc).isoformat()
        }
        
        # Final update
        update_expense_doc(
            expense_id,
            {
                "status": final_verdict,
                "version": firestore.Increment(1),
                "findings.synthesis": result,
                "completedAt": datetime.now(timezone.utc),
            },
            {
                "actor": "synthesis_agent",
                "action": "COMPLETED",
                "verdict": final_verdict,
            },
        )
        
        logger.info(f"Synthesis complete: {final_verdict} with {confidence}% confidence")
        return result
        
    except Exception as e:
        logger.error(f"Synthesis failed: {e}")
        error_message = _format_error_message(e)
        update_expense_doc(
            expense_id,
            {
                "status": "FAILED",
                "error": error_message,
            },
            {
                "actor": "synthesis_agent",
                "action": "FAILED",
                "error": error_message,
            },
        )
        raise


def process_message(message: pubsub_v1.subscriber.message.Message):
    """Process a single Pub/Sub message"""
    try:
        data = json.loads(message.data.decode('utf-8'))
        expense_id = data.get("expenseId")
        gcs_uri = data.get("gcsUri")
        
        logger.info(f"Processing message for expense {expense_id}, agent type: {AGENT_TYPE}")

        doc_data_for_skip = _get_expense_doc_data(expense_id)
        if doc_data_for_skip and _is_final_status(doc_data_for_skip.get("status")):
            logger.info(
                "Expense %s already finalized with status %s. Skipping %s worker.",
                expense_id,
                doc_data_for_skip.get("status"),
                AGENT_TYPE,
            )
            message.ack()
            return
        
        if AGENT_TYPE == "extraction":
            if doc_data_for_skip and _stage_already_completed(doc_data_for_skip, "extraction"):
                logger.info("Skipping extraction for %s; stage already marked complete.", expense_id)
                message.ack()
                return
            # Extract data and publish to next topic
            extracted_data = extraction_agent(expense_id, gcs_uri)
            if TOPIC_OUT:
                topic_path = publisher.topic_path(PROJECT_ID, TOPIC_OUT)
                publisher.publish(topic_path, json.dumps({
                    "expenseId": expense_id,
                    "gcsUri": gcs_uri,
                    "extracted": extracted_data
                }).encode('utf-8'))
                logger.info(f"Published to {TOPIC_OUT}")
        
        elif AGENT_TYPE == "policy":
            if doc_data_for_skip and _stage_already_completed(doc_data_for_skip, "policy"):
                logger.info("Skipping policy check for %s; stage already complete.", expense_id)
                message.ack()
                return
            doc_dict = doc_data_for_skip or _get_expense_doc_data(expense_id)
            if not doc_dict:
                logger.warning("Policy worker could not load expense document %s. Nacking for retry.", expense_id)
                message.nack()
                return
            # Get extracted data from Firestore
            findings = doc_dict.get("findings", {})
            extracted_data = findings.get("extraction", {})
            if not extracted_data:
                logger.warning("Policy worker missing extraction findings for %s. Nacking.", expense_id)
                message.nack()
                return
            
            # Check policy
            policy_result = policy_agent(expense_id, extracted_data)
            
            if TOPIC_OUT:
                topic_path = publisher.topic_path(PROJECT_ID, TOPIC_OUT)
                publisher.publish(topic_path, json.dumps({
                    "expenseId": expense_id,
                    "policy": policy_result
                }).encode('utf-8'))
        
        elif AGENT_TYPE == "anomaly":
            if doc_data_for_skip and _stage_already_completed(doc_data_for_skip, "anomaly"):
                logger.info("Skipping anomaly check for %s; stage already complete.", expense_id)
                message.ack()
                return
            doc_dict = doc_data_for_skip or _get_expense_doc_data(expense_id)
            if not doc_dict:
                logger.warning("Anomaly worker could not load expense document %s. Nacking for retry.", expense_id)
                message.nack()
                return
            # Get extracted data
            findings = doc_dict.get("findings", {})
            extracted_data = findings.get("extraction", {})
            if not extracted_data:
                logger.warning("Anomaly worker missing extraction findings for %s. Nacking.", expense_id)
                message.nack()
                return
            
            anomaly_result = anomaly_agent(expense_id, extracted_data)
            
            if TOPIC_OUT:
                topic_path = publisher.topic_path(PROJECT_ID, TOPIC_OUT)
                publisher.publish(topic_path, json.dumps({
                    "expenseId": expense_id,
                    "anomaly": anomaly_result
                }).encode('utf-8'))
        
        elif AGENT_TYPE == "remediation":
            if doc_data_for_skip and _stage_already_completed(doc_data_for_skip, "remediation"):
                logger.info("Skipping remediation for %s; stage already complete.", expense_id)
                message.ack()
                return
            # Get all findings
            doc_dict = doc_data_for_skip or _get_expense_doc_data(expense_id)
            if not doc_dict:
                logger.warning("Remediation worker could not load expense document %s. Nacking for retry.", expense_id)
                message.nack()
                return
            findings = doc_dict.get("findings", {})
            if not findings:
                logger.warning("Remediation worker missing findings for %s. Nacking.", expense_id)
                message.nack()
                return
            
            remediation_result = remediation_agent(expense_id, findings)
            
            # Trigger synthesis after remediation
            # Call synthesis directly since it's the final step
            synthesis_result = synthesis_agent(expense_id)
        
        # Acknowledge message
        message.ack()
        logger.info(f"Message processed and acknowledged for {expense_id}")
        
    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        # Nack the message so it can be retried
        message.nack()


def main():
    """Main worker loop - pull messages from Pub/Sub"""
    if not SUBSCRIPTION:
        logger.error("SUBSCRIPTION environment variable not set")
        sys.exit(1)
    
    subscription_path = subscriber.subscription_path(PROJECT_ID, SUBSCRIPTION)
    
    logger.info(f"Starting {AGENT_TYPE} worker")
    logger.info(f"Subscription: {subscription_path}")
    logger.info(f"Output topic: {TOPIC_OUT}")
    
    # Pull messages
    flow_control = FlowControl(
        max_messages=max(1, MAX_INFLIGHT_MESSAGES),
        max_bytes=max(1, MAX_INFLIGHT_BYTES),
    )
    streaming_pull_future = subscriber.subscribe(
        subscription_path,
        callback=process_message,
        flow_control=flow_control,
    )
    
    logger.info(f"Listening for messages on {subscription_path}...")
    
    try:
        # Keep the worker running
        streaming_pull_future.result()
    except KeyboardInterrupt:
        streaming_pull_future.cancel()
        logger.info("Worker stopped by user")
    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        streaming_pull_future.cancel()
        raise


if __name__ == "__main__":
    main()
