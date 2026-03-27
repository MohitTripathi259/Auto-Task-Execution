from fastapi import APIRouter, HTTPException

from src.common.logging import get_logger
from src.storage import dynamo
from src.storage.rds import get_db, get_task

logger = get_logger(__name__)
router = APIRouter()


@router.get("/{run_id}/events")
async def get_run_events(run_id: str, limit: int = 200):
    events = dynamo.get_events(run_id, limit=limit)
    return {"run_id": run_id, "count": len(events), "events": events}


@router.get("/{run_id}/checkpoints")
async def get_checkpoints(run_id: str):
    latest = dynamo.get_latest_checkpoint(run_id)
    return {"run_id": run_id, "latest_checkpoint": latest}
