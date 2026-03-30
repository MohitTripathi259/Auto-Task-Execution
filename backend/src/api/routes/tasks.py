from fastapi import APIRouter, BackgroundTasks, HTTPException

from src.api.schemas import (
    BacktrackRequest,
    CreateTaskRequest,
    TaskSubmitResponse,
)
from src.common.logging import get_logger
from src.common.utils import generate_run_id, idempotency_key
from src.storage import dynamo, s3
from src.storage.rds import (
    create_task,
    get_db,
    get_steps,
    get_task,
    get_task_by_idempotency_key,
    get_test_runs,
)

logger = get_logger(__name__)
router = APIRouter()


@router.post("", response_model=TaskSubmitResponse)
async def submit_task(request: CreateTaskRequest, background_tasks: BackgroundTasks):
    run_id = generate_run_id()
    idem_key = request.idempotency_key or idempotency_key(
        request.goal, request.repo_url, request.tenant_id
    )

    with get_db() as db:
        existing = get_task_by_idempotency_key(db, idem_key)
        if existing:
            logger.info("task.duplicate", task_id=existing.id)
            return TaskSubmitResponse(
                task_id=existing.id,
                run_id=existing.run_id,
                status=existing.status,
                duplicate=True,
            )

        task = create_task(
            db,
            run_id=run_id,
            tenant_id=request.tenant_id,
            goal=request.goal,
            repo_url=request.repo_url,
            base_branch=request.base_branch,
            idempotency_key=idem_key,
            task_skill_content=request.task_skill or None,
            policy={
                "max_cost_usd": request.max_cost_usd,
                "max_runtime_minutes": request.max_runtime_minutes,
            },
        )
        task_id = task.id

    dynamo.put_event(run_id, "task.submitted", {"task_id": task_id, "goal": request.goal})
    logger.info("task.submitted", task_id=task_id, run_id=run_id)

    # Run orchestration in background
    background_tasks.add_task(_run_orchestration, task_id, run_id)

    return TaskSubmitResponse(task_id=task_id, run_id=run_id, status="pending")


async def _run_orchestration(task_id: str, run_id: str) -> None:
    from src.agents.orchestrator import agent as orchestrator
    try:
        await orchestrator.run(task_id, run_id)
    except Exception as exc:
        logger.exception("orchestration.background_error", task_id=task_id, error=str(exc))


@router.get("/{task_id}")
async def get_task_status(task_id: str):
    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        steps = get_steps(db, task_id)

        # Convert to plain dicts inside session to avoid DetachedInstanceError
        task_data = {
            "task_id": task.id,
            "run_id": task.run_id,
            "status": task.status,
            "goal": task.goal,
            "risk_level": task.risk_level,
            "current_step_id": task.current_step_id,
            "steps": [
                {
                    "step_id": s.id,
                    "index": s.index,
                    "action": s.action,
                    "status": s.status,
                    "base_sha": s.base_sha,
                    "head_sha": s.head_sha,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    "error": s.error,
                }
                for s in steps
            ],
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "error": task.error,
            "pr_url": task.pr_url,
        }
    return task_data


@router.get("/{task_id}/audit")
async def get_audit_trail(task_id: str):
    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        run_id = task.run_id  # read inside session

    events = dynamo.get_events(run_id)
    return {"run_id": run_id, "task_id": task_id, "events": events}


@router.get("/{task_id}/tests")
async def get_test_results(task_id: str):
    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        runs = get_test_runs(db, task_id)

        # Convert inside session
        test_data = [
            {
                "test_run_id": r.id,
                "step_id": r.step_id,
                "suite": r.suite,
                "status": r.status,
                "passed": r.passed,
                "failed": r.failed,
                "total": r.total,
                "duration_ms": r.duration_ms,
                "results": r.results or [],
                "artifact_url": s3.presigned_url(r.artifact_s3_key) if r.artifact_s3_key else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in runs
        ]
    return {"task_id": task_id, "test_runs": test_data}


@router.post("/{task_id}/backtrack/{step_id}")
async def backtrack_to_step(task_id: str, step_id: str, request: BacktrackRequest):
    from src.agents.orchestrator.backtrack import execute_backtrack

    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in ("completed", "failed", "running", "cancelled"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot backtrack a task with status '{task.status}'"
            )

    try:
        result = await execute_backtrack(
            task_id=task_id,
            step_id=step_id,
            reason=request.reason,
        )
        logger.info("task.backtrack.started", task_id=task_id, step_id=step_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        from src.storage.rds import update_task
        update_task(db, task_id, status="cancelled")
        dynamo.put_event(task.run_id, "task.cancelled", {"task_id": task_id})

    return {"task_id": task_id, "status": "cancelled"}


@router.get("/{task_id}/report")
async def get_report(task_id: str):
    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.report_s3_key:
            raise HTTPException(status_code=404, detail="Report not yet generated")

    url = s3.presigned_url(task.report_s3_key)
    return {"task_id": task_id, "report_url": url}
