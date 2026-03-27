"""
SQS FIFO queue — task intake buffer.
Note: Step Functions Standard will replace this as the primary orchestrator
post-demo. SQS stays for sub-work (test sharding, async notifications).
"""

import json
from typing import Any

import boto3

from src.common.config import settings
from src.common.logging import get_logger
from src.common.utils import idempotency_key

logger = get_logger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"region_name": settings.AWS_REGION}
        if settings.AWS_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
        _client = boto3.client("sqs", **kwargs)
    return _client


def send_task(task_id: str, run_id: str, payload: dict[str, Any]) -> str:
    dedup_key = idempotency_key(task_id, run_id)
    body = json.dumps({"task_id": task_id, "run_id": run_id, **payload})

    response = _get_client().send_message(
        QueueUrl=settings.SQS_TASK_QUEUE_URL,
        MessageBody=body,
        MessageDeduplicationId=dedup_key,
        MessageGroupId=task_id,   # FIFO: one group per task
    )
    message_id = response["MessageId"]
    logger.info("sqs.task.sent", task_id=task_id, run_id=run_id, message_id=message_id)
    return message_id


def receive_tasks(max_messages: int = 1, wait_seconds: int = 20) -> list[dict]:
    """Long-poll for tasks. Returns list of {receipt_handle, message_id, body}."""
    response = _get_client().receive_message(
        QueueUrl=settings.SQS_TASK_QUEUE_URL,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=wait_seconds,
        VisibilityTimeout=settings.SQS_VISIBILITY_TIMEOUT,
        AttributeNames=["All"],
    )
    result = []
    for msg in response.get("Messages", []):
        result.append({
            "receipt_handle": msg["ReceiptHandle"],
            "message_id": msg["MessageId"],
            "body": json.loads(msg["Body"]),
        })
    return result


def delete_message(receipt_handle: str) -> None:
    _get_client().delete_message(
        QueueUrl=settings.SQS_TASK_QUEUE_URL,
        ReceiptHandle=receipt_handle,
    )
    logger.debug("sqs.message.deleted")


def extend_visibility(receipt_handle: str, timeout: int = 300) -> None:
    """Heartbeat — call periodically for long-running tasks to prevent re-delivery."""
    _get_client().change_message_visibility(
        QueueUrl=settings.SQS_TASK_QUEUE_URL,
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=timeout,
    )
    logger.debug("sqs.visibility.extended", timeout=timeout)
