import os
from typing import Optional

from google.cloud import storage


async def upload_file_to_gcs(bucket_name: str, object_name: str, file) -> str:
    client = storage.Client(project=os.getenv("PROJECT_ID"))
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    # Upload in chunks from UploadFile
    blob.upload_from_file(file.file, rewind=True, content_type=file.content_type)
    return f"gs://{bucket_name}/{object_name}"


