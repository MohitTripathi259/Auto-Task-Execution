"""
Orchestrator Agent — main coordination loop.

Responsibilities:
  1. Receive task (task_id + run_id)
  2. Call Planner → structured step plan
  3. Call Policy Generator → boundary + budget rules
  4. Persist plan + policy to PostgreSQL
  5. Spawn executor (Phase 2) — each step runs inside ECS container
  6. Handle permission requests from executor
  7. Track cost + token usage
  8. Call Reporter → generate final summary

Control plane uses raw Anthropic Messages API.
Executor (Phase 2) uses Claude Code SDK inside ECS Fargate.
"""

import asyncio
from datetime import datetime

from src.agents.orchestrator import permission, planner, policy, reporter
from src.common.logging import get_logger
from src.common.utils import generate_attempt_id, utc_now, utc_now_iso
from src.storage import dynamo, s3
from src.storage.rds import (
    Approval,
    Task,
    create_approval,
    create_step,
    get_db,
    get_steps,
    get_task,
    get_test_runs,
    update_step,
    update_task,
)

logger = get_logger(__name__)


async def run(task_id: str, run_id: str, start_from_index: int = 0) -> dict:
    """
    Main orchestration loop. Called by the API as a background task.
    start_from_index: skip steps before this index (used by backtrack engine).
    Returns the final report dict.
    """
    started_at = utc_now_iso()
    attempt_id = generate_attempt_id()
    token_usage = {"input_tokens": 0, "output_tokens": 0}

    logger.info("orchestrator.started", task_id=task_id, run_id=run_id,
                attempt_id=attempt_id, start_from_index=start_from_index)
    dynamo.put_event(run_id, "orchestrator.started", {
        "task_id": task_id, "attempt_id": attempt_id, "start_from_index": start_from_index,
    })

    try:
        # ── 1. Load task ──────────────────────────────────────────────────────
        with get_db() as db:
            task = get_task(db, task_id)
            if not task:
                raise ValueError(f"Task not found: {task_id}")
            goal = task.goal
            repo_url = task.repo_url or ""
            base_branch = task.base_branch or "main"
            task_policy = task.policy or {}

        # ── 2. Plan (skip re-planning on backtrack — reuse stored plan) ─────────
        if start_from_index > 0:
            # Backtrack: load the existing plan from the task record
            with get_db() as db:
                task = get_task(db, task_id)
                plan = task.plan
                task_policy_obj = task.policy or {}
            dynamo.put_event(run_id, "backtrack.plan_reused", {
                "start_from_index": start_from_index,
                "total_steps": plan["total_steps"],
            })
        else:
            # Normal run: generate a fresh plan
            _update_status(task_id, run_id, "planning")

            plan = planner.create_plan(goal, repo_url, base_branch)
            dynamo.put_event(run_id, "planner.completed", {
                "risk_level": plan["risk_level"],
                "total_steps": plan["total_steps"],
            })

            # ── 3. Generate policy ────────────────────────────────────────────
            task_policy_obj = policy.generate_policy(
                plan=plan,
                max_cost_usd=task_policy.get("max_cost_usd", 8.0),
                max_runtime_minutes=task_policy.get("max_runtime_minutes", 60),
            )
            dynamo.put_event(run_id, "policy.generated", {
                "risk_level": task_policy_obj["risk_level"],
                "max_cost_usd": task_policy_obj["max_cost_usd"],
                "permission_required_for": task_policy_obj.get("permission_required_for", []),
            })

            # ── 4. Persist plan + policy + steps to PostgreSQL ────────────────
            with get_db() as db:
                update_task(db, task_id,
                    status="running",
                    risk_level=plan["risk_level"],
                    plan=plan,
                    policy=task_policy_obj,
                )
                for step_def in plan["steps"]:
                    db_step_id = f"{task_id}_{step_def['step_id']}"
                    step_def["_db_step_id"] = db_step_id
                    create_step(db,
                        id=db_step_id,
                        task_id=task_id,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        index=step_def["index"],
                        action=step_def["action"],
                        expected_output=step_def.get("expected_output", ""),
                    )

            logger.info("orchestrator.plan_persisted", task_id=task_id, steps=plan["total_steps"])

        # Annotate step defs with DB IDs (needed in both normal + backtrack paths)
        for step_def in plan["steps"]:
            if "_db_step_id" not in step_def:
                step_def["_db_step_id"] = f"{task_id}_{step_def['step_id']}"

        # ── 5. Execute steps via Executor Agent ───────────────────────────────
        from src.agents.executor.runner import run_step_async
        from src.agents.executor.workspace import Workspace
        from src.storage.rds import create_test_run

        approval_records: list[dict] = []

        with Workspace(task_id=task_id, repo_url=repo_url, base_branch=base_branch) as ws:
            for step_def in plan["steps"]:
                # Skip steps before the backtrack target
                if step_def["index"] < start_from_index:
                    logger.debug("orchestrator.step.skipped_backtrack",
                                 index=step_def["index"], start_from=start_from_index)
                    continue

                # Use task-scoped DB ID (avoids PK collision across tasks)
                step_id = step_def.get("_db_step_id", f"{task_id}_{step_def['step_id']}")

                with get_db() as db:
                    update_step(db, step_id, status="running", started_at=datetime.utcnow())
                with get_db() as db:
                    update_task(db, task_id, current_step_id=step_id)

                dynamo.put_event(run_id, "step.started", {
                    "step_id": step_id,
                    "action": step_def["action"],
                }, step_id=step_id, attempt_id=attempt_id)

                dynamo.put_checkpoint(run_id, step_id, {
                    "step_id": step_id,
                    "action": step_def["action"],
                    "attempt_id": attempt_id,
                    "status": "running",
                })

                # Run the step via executor (real Claude tool-use loop)
                step_result = await run_step_async(
                    step_id=step_id,
                    action=step_def["action"],
                    expected_output=step_def.get("expected_output", ""),
                    workspace=ws,
                    policy=task_policy_obj,
                    run_id=run_id,
                    task_id=task_id,
                )

                # Accumulate token usage
                token_usage["input_tokens"] += step_result.token_usage.get("input_tokens", 0)
                token_usage["output_tokens"] += step_result.token_usage.get("output_tokens", 0)

                # Persist step outcome
                with get_db() as db:
                    update_step(db, step_id,
                        status=step_result.status,
                        completed_at=datetime.utcnow(),
                        base_sha=step_result.base_sha or None,
                        head_sha=step_result.head_sha or None,
                        error=step_result.error,
                    )

                # Persist test results if any
                for tr in step_result.test_results:
                    with get_db() as db:
                        create_test_run(db,
                            task_id=task_id,
                            step_id=step_id,
                            attempt_id=attempt_id,
                            suite=tr.get("runner", "unit"),
                            status="pass" if tr.get("success") else "fail",
                            passed=tr.get("passed", 0),
                            failed=tr.get("failed", 0),
                            total=tr.get("total", 0),
                        )

                dynamo.put_event(run_id, "step.completed", {
                    "step_id": step_id,
                    "status": step_result.status,
                    "summary": step_result.summary[:200],
                    "files_changed": step_result.files_changed,
                    "tool_calls_count": len(step_result.tool_calls),
                }, step_id=step_id, attempt_id=attempt_id)

                logger.info("orchestrator.step.done",
                    step_id=step_id, status=step_result.status,
                    tools_called=len(step_result.tool_calls))

                if step_result.status == "failed":
                    logger.warning("orchestrator.step.failed", step_id=step_id,
                        error=step_result.error)

                await asyncio.sleep(0.2)

        # ── 6. Generate report ────────────────────────────────────────────────
        with get_db() as db:
            final_steps = [
                {
                    "step_id": s.id,
                    "action": s.action,
                    "status": s.status,
                    "error": s.error,
                }
                for s in get_steps(db, task_id)
            ]
            final_test_runs = [
                {
                    "suite": t.suite,
                    "status": t.status,
                    "passed": t.passed,
                    "failed": t.failed,
                }
                for t in get_test_runs(db, task_id)
            ]
            approvals_from_db = [
                {
                    "request_type": a.request_type,
                    "description": a.description,
                    "decision": a.decision,
                }
                for a in db.query(Approval).filter(Approval.task_id == task_id).all()
            ]

        report_result = reporter.generate_report(
            task_id=task_id,
            run_id=run_id,
            tenant_id="default",
            goal=goal,
            steps=final_steps,
            test_runs=final_test_runs,
            approvals=approvals_from_db,
            started_at=started_at,
            ended_at=utc_now_iso(),
            token_usage=token_usage,
        )

        # ── 7. Finalise task ──────────────────────────────────────────────────
        failed_steps = [s for s in final_steps if s["status"] == "failed"]
        final_status = "completed" if not failed_steps else "failed"

        with get_db() as db:
            update_task(db, task_id,
                status=final_status,
                completed_at=datetime.utcnow(),
                report_s3_key=report_result["s3_key"],
            )

        dynamo.put_event(run_id, "orchestrator.completed", {
            "status": final_status,
            "report_s3_key": report_result["s3_key"],
        })

        logger.info("orchestrator.completed", task_id=task_id, status=final_status)
        return report_result["report"]

    except Exception as exc:
        logger.exception("orchestrator.failed", task_id=task_id, error=str(exc))
        dynamo.put_event(run_id, "orchestrator.failed", {"error": str(exc)})
        with get_db() as db:
            update_task(db, task_id, status="failed", error=str(exc))
        raise


def _update_status(task_id: str, run_id: str, status: str) -> None:
    with get_db() as db:
        update_task(db, task_id, status=status)
    dynamo.put_event(run_id, f"task.status.{status}", {"task_id": task_id})
