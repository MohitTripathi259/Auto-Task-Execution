"""
DynamoDB — append-only event journal + checkpoints + approval requests.
High-write, schema-flexible. Not the source of truth for relational state.
"""

import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from src.common.config import settings
from src.common.logging import get_logger
from src.common.utils import utc_now_iso

logger = get_logger(__name__)

_resource = None


def _to_dynamo(obj: Any) -> Any:
    """Recursively convert floats to Decimal (DynamoDB requirement)."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo(v) for v in obj]
    return obj


def _get_resource():
    global _resource
    if _resource is None:
        kwargs: dict[str, Any] = {"region_name": settings.AWS_REGION}
        if settings.AWS_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
        _resource = boto3.resource("dynamodb", **kwargs)
    return _resource


def _table(name: str):
    return _get_resource().Table(name)


# ── Events ────────────────────────────────────────────────────────────────────

def put_event(
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    step_id: str | None = None,
    attempt_id: str | None = None,
    tool_call_id: str | None = None,
    approval_id: str | None = None,
    ttl_hours: int = 168,  # 7-day default retention
) -> None:
    timestamp = utc_now_iso()
    item: dict[str, Any] = {
        "pk": run_id,
        "sk": f"EVENT#{timestamp}#{event_type}",
        "run_id": run_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "payload": _to_dynamo(payload),
        "ttl": int(time.time()) + (ttl_hours * 3600),
    }
    if step_id:
        item["step_id"] = step_id
    if attempt_id:
        item["attempt_id"] = attempt_id
    if tool_call_id:
        item["tool_call_id"] = tool_call_id
    if approval_id:
        item["approval_id"] = approval_id

    _table(settings.DYNAMODB_TABLE_EVENTS).put_item(Item=item)
    logger.debug("dynamo.event.put", run_id=run_id, event_type=event_type)


def get_events(run_id: str, limit: int = 500) -> list[dict]:
    response = _table(settings.DYNAMODB_TABLE_EVENTS).query(
        KeyConditionExpression=Key("pk").eq(run_id) & Key("sk").begins_with("EVENT#"),
        ScanIndexForward=True,
        Limit=limit,
    )
    return response.get("Items", [])


# ── Checkpoints ───────────────────────────────────────────────────────────────

def put_checkpoint(run_id: str, step_id: str, state: dict[str, Any]) -> None:
    """Store a recoverable checkpoint after each successful step."""
    _table(settings.DYNAMODB_TABLE_EVENTS).put_item(Item={
        "pk": run_id,
        "sk": f"CHECKPOINT#{step_id}",
        "run_id": run_id,
        "step_id": step_id,
        "state": _to_dynamo(state),
        "timestamp": utc_now_iso(),
    })
    logger.debug("dynamo.checkpoint.put", run_id=run_id, step_id=step_id)


def get_checkpoint(run_id: str, step_id: str) -> dict | None:
    response = _table(settings.DYNAMODB_TABLE_EVENTS).get_item(
        Key={"pk": run_id, "sk": f"CHECKPOINT#{step_id}"}
    )
    return response.get("Item")


def get_latest_checkpoint(run_id: str) -> dict | None:
    """Return the most recent checkpoint for a run (used for resume)."""
    response = _table(settings.DYNAMODB_TABLE_EVENTS).query(
        KeyConditionExpression=Key("pk").eq(run_id) & Key("sk").begins_with("CHECKPOINT#"),
        ScanIndexForward=False,  # descending
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


# ── Approval requests ─────────────────────────────────────────────────────────

def put_approval_request(
    run_id: str,
    approval_id: str,
    request: dict[str, Any],
) -> None:
    _table(settings.DYNAMODB_TABLE_EVENTS).put_item(Item={
        "pk": run_id,
        "sk": f"APPROVAL#{approval_id}",
        "run_id": run_id,
        "approval_id": approval_id,
        "status": "pending",
        "request": _to_dynamo(request),
        "created_at": utc_now_iso(),
    })
    logger.info("dynamo.approval.created", run_id=run_id, approval_id=approval_id)


def update_approval_status(
    run_id: str,
    approval_id: str,
    decision: str,
    reason: str,
) -> None:
    _table(settings.DYNAMODB_TABLE_EVENTS).update_item(
        Key={"pk": run_id, "sk": f"APPROVAL#{approval_id}"},
        UpdateExpression="SET #s = :s, #r = :r, decided_at = :d",
        ExpressionAttributeNames={"#s": "status", "#r": "reason"},
        ExpressionAttributeValues={
            ":s": decision,
            ":r": reason,
            ":d": utc_now_iso(),
        },
    )
    logger.info("dynamo.approval.updated", approval_id=approval_id, decision=decision)
