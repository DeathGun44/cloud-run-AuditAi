import csv
import os
from datetime import datetime, timezone

from google.cloud import firestore, storage

PROJECT_ID = os.getenv("PROJECT_ID")
REGION = os.getenv("REGION", "us-central1")
REPORT_BUCKET = os.getenv("REPORT_BUCKET", f"{PROJECT_ID}-auditai-reports")


def run():
    fs = firestore.Client(project=PROJECT_ID)
    st = storage.Client(project=PROJECT_ID)

    docs = fs.collection("expenses").stream()
    rows = [["expenseId", "status", "decision", "total", "merchant", "score"]]
    for d in docs:
        v = d.to_dict()
        rows.append(
            [
                d.id,
                v.get("status"),
                (v.get("final") or {}).get("status"),
                ((v.get("extraction") or {}).get("fields") or {}).get("total"),
                ((v.get("extraction") or {}).get("fields") or {}).get("merchant"),
                (v.get("policy") or {}).get("score"),
            ]
        )

    now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"reports/audit-{now}.csv"
    bucket = st.bucket(REPORT_BUCKET)
    blob = bucket.blob(filename)

    with blob.open("w") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"Wrote gs://{REPORT_BUCKET}/{filename}")


if __name__ == "__main__":
    run()

