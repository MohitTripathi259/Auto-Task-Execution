from fastapi import APIRouter, HTTPException

from src.api.schemas import ApprovalDecision
from src.common.logging import get_logger
from src.storage import dynamo
from src.storage.rds import get_db, get_task, update_approval_decision

logger = get_logger(__name__)
router = APIRouter()


@router.post("/{task_id}/{approval_id}")
async def decide_approval(task_id: str, approval_id: str, body: ApprovalDecision):
    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        update_approval_decision(db, approval_id, body.decision, body.reason or "")
        run_id = task.run_id

    dynamo.update_approval_status(run_id, approval_id, body.decision, body.reason or "")
    dynamo.put_event(
        run_id,
        "approval.decided",
        {"approval_id": approval_id, "decision": body.decision, "reason": body.reason},
        approval_id=approval_id,
    )

    logger.info(
        "approval.decided",
        task_id=task_id,
        approval_id=approval_id,
        decision=body.decision,
    )
    return {"approval_id": approval_id, "decision": body.decision}
