"""
Backtrack engine — rewinds a task to a prior step and re-executes from there.

How it works:
  1. Find the target step's index in the plan
  2. Reset all steps from that index onwards → pending (clear SHAs, errors, timestamps)
  3. Reset task status → running
  4. Spawn a new orchestrator run that:
       - Skips steps before the target (already completed, keep their state)
       - Re-executes steps from the target onwards in a fresh workspace
  5. Log the backtrack event with reason to DynamoDB

The fresh workspace means the agent re-does the work from the target step.
For multi-step tasks this is intentional — it gives a clean slate from that point.
"""

import asyncio
from datetime import datetime

from src.common.logging import get_logger
from src.common.utils import generate_run_id
from src.storage import dynamo
from src.storage.rds import Step, Task, get_db, get_step, get_steps, get_task, update_step, update_task

logger = get_logger(__name__)


async def execute_backtrack(
    task_id: str,
    step_id: str,
    reason: str | None = None,
) -> dict:
    """
    Main entry point. Called from the API route as a background task.
    Returns immediately with status; the re-run happens in the background.
    """
    with get_db() as db:
        task = get_task(db, task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        target_step = get_step(db, step_id)
        if not target_step:
            raise ValueError(f"Step not found: {step_id}")

        if target_step.task_id != task_id:
            raise ValueError("Step does not belong to this task")

        target_index = target_step.index
        old_run_id = task.run_id

        # Collect step data before resetting
        all_steps = get_steps(db, task_id)
        steps_to_reset = [s for s in all_steps if s.index >= target_index]
        steps_to_keep  = [s for s in all_steps if s.index < target_index]

        logger.info(
            "backtrack.started",
            task_id=task_id,
            target_step=step_id,
            target_index=target_index,
            resetting=len(steps_to_reset),
            keeping=len(steps_to_keep),
        )

        # ── Reset steps from target onwards ──────────────────────────────────
        for step in steps_to_reset:
            update_step(db, step.id,
                status="pending",
                started_at=None,
                completed_at=None,
                base_sha=None,
                head_sha=None,
                error=None,
            )

        # ── Generate a new run_id for the re-execution ────────────────────────
        new_run_id = generate_run_id()

        # ── Reset task state ──────────────────────────────────────────────────
        update_task(db, task_id,
            status="running",
            run_id=new_run_id,
            current_step_id=step_id,
            completed_at=None,
            error=None,
            report_s3_key=None,
        )

        # Read values needed after session closes
        goal        = task.goal
        repo_url    = task.repo_url or ""
        base_branch = task.base_branch or "main"
        task_policy = task.policy or {}

    # ── Record the backtrack event ────────────────────────────────────────────
    dynamo.put_event(new_run_id, "backtrack.executed", {
        "original_run_id": old_run_id,
        "target_step_id": step_id,
        "target_index": target_index,
        "reason": reason or "No reason provided",
        "steps_reset": len(steps_to_reset),
    }, step_id=step_id)

    # ── Re-run orchestrator starting from target_index ────────────────────────
    asyncio.create_task(
        _rerun_from_index(
            task_id=task_id,
            run_id=new_run_id,
            start_from_index=target_index,
        )
    )

    return {
        "task_id": task_id,
        "new_run_id": new_run_id,
        "target_step_id": step_id,
        "target_index": target_index,
        "steps_reset": len(steps_to_reset),
        "status": "backtrack_started",
    }


async def _rerun_from_index(task_id: str, run_id: str, start_from_index: int) -> None:
    """Re-run the orchestrator but only execute steps from start_from_index."""
    from src.agents.orchestrator import agent as orchestrator
    try:
        await orchestrator.run(task_id, run_id, start_from_index=start_from_index)
    except Exception as exc:
        logger.exception("backtrack.rerun_failed", task_id=task_id, error=str(exc))
        with get_db() as db:
            update_task(db, task_id, status="failed", error=f"Backtrack re-run failed: {exc}")
