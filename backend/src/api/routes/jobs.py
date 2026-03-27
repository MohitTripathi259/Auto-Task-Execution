from fastapi import APIRouter

from src.common.logging import get_logger
from src.storage.rds import get_db, list_tasks

logger = get_logger(__name__)
router = APIRouter()


@router.get("")
async def list_jobs(tenant_id: str = "default", limit: int = 50):
    with get_db() as db:
        tasks = list_tasks(db, tenant_id=tenant_id, limit=limit)
        jobs = [
            {
                "task_id": t.id,
                "run_id": t.run_id,
                "goal": t.goal[:120],
                "status": t.status,
                "risk_level": t.risk_level,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in tasks
        ]
    return {"jobs": jobs}
