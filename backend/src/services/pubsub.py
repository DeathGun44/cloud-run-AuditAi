import os
import json
from typing import Dict

from google.cloud import pubsub_v1


_publisher = None


def _get_publisher() -> pubsub_v1.PublisherClient:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def publish_event(topic: str, message: Dict) -> None:
    """Publish event to Pub/Sub topic (synchronous)"""
    project_id = os.getenv("PROJECT_ID")
    topic_path = _get_publisher().topic_path(project_id, topic)
    data = json.dumps(message).encode("utf-8")
    future = _get_publisher().publish(topic_path, data)
    future.result(timeout=30)  # Block until published


