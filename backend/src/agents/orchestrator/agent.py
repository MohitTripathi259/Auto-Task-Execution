"""
Orchestrator Agent — main coordination loop.

Responsibilities:
  1. Receive task (task_id + run_id)
  2. Call Planner skill → structured step plan
  3. Call Policy skill  → boundary + budget rules
  4. Persist plan + policy to PostgreSQL
  5. Spawn Executor skill — Claude tool-use loop per step
  6. Optionally push to GitHub and open PR (github_pr skill)
  7. Call Reporter skill → generate final summary

Control plane uses raw Anthropic Messages API.
Executor (Phase 2) uses Claude tool-use loop inside the executor skill.
"""

import asyncio
from datetime import datetime

from src.common.logging import get_logger
from src.common.utils import generate_attempt_id, utc_now_iso
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
            goal               = task.goal
            repo_url           = task.repo_url or ""
            base_branch        = task.base_branch or "main"
            task_policy        = task.policy or {}
            task_skill_content = task.task_skill_content  # may be None

        # ── 2. Plan (skip re-planning on backtrack — reuse stored plan) ───────
        if start_from_index > 0:
            with get_db() as db:
                task = get_task(db, task_id)
                plan             = task.plan
                task_policy_obj  = task.policy or {}
            dynamo.put_event(run_id, "backtrack.plan_reused", {
                "start_from_index": start_from_index,
                "total_steps": plan["total_steps"],
            })
        else:
            _update_status(task_id, run_id, "planning")

            # Use planner skill
            from src.skills.planner.execution import create_plan
            plan = create_plan(goal, repo_url, base_branch, task_skill_content)
            dynamo.put_event(run_id, "planner.completed", {
                "risk_level": plan["risk_level"],
                "total_steps": plan["total_steps"],
            })

            # Use policy skill
            from src.skills.policy.execution import generate_policy
            task_policy_obj = generate_policy(
                plan=plan,
                max_cost_usd=task_policy.get("max_cost_usd", 8.0),
                max_runtime_minutes=task_policy.get("max_runtime_minutes", 60),
                task_skill_content=task_skill_content,
            )
            dynamo.put_event(run_id, "policy.generated", {
                "risk_level": task_policy_obj["risk_level"],
                "max_cost_usd": task_policy_obj["max_cost_usd"],
                "permission_required_for": task_policy_obj.get("permission_required_for", []),
            })

            # Persist plan + policy + steps to PostgreSQL
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

        # ── 3. Execute steps via Executor skill ───────────────────────────────
        from src.skills.executor.execution import run_step_async
        from src.agents.executor.workspace import Workspace
        from src.storage.rds import create_test_run

        feature_branch: str | None = None

        with Workspace(task_id=task_id, repo_url=repo_url, base_branch=base_branch) as ws:
            for step_def in plan["steps"]:
                if step_def["index"] < start_from_index:
                    logger.debug("orchestrator.step.skipped_backtrack",
                                 index=step_def["index"], start_from=start_from_index)
                    continue

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

                step_result = await run_step_async(
                    step_id=step_id,
                    action=step_def["action"],
                    expected_output=step_def.get("expected_output", ""),
                    workspace=ws,
                    policy=task_policy_obj,
                    run_id=run_id,
                    task_id=task_id,
                    task_skill_content=task_skill_content,
                )

                token_usage["input_tokens"]  += step_result.token_usage.get("input_tokens", 0)
                token_usage["output_tokens"] += step_result.token_usage.get("output_tokens", 0)

                with get_db() as db:
                    update_step(db, step_id,
                        status=step_result.status,
                        completed_at=datetime.utcnow(),
                        base_sha=step_result.base_sha or None,
                        head_sha=step_result.head_sha or None,
                        error=step_result.error,
                    )

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

            # Capture the feature branch name from the workspace
            try:
                feature_branch = ws.current_branch()
            except Exception:
                feature_branch = None

        # ── 4. GitHub PR (optional — only if token + repo configured) ─────────
        pr_url: str | None = None
        if repo_url and feature_branch and feature_branch not in (base_branch, "main", "master"):
            from src.skills.github_pr.execution import create_pull_request
            with get_db() as db:
                final_steps_for_pr = [
                    {"action": s.action}
                    for s in get_steps(db, task_id)
                ]
                risk_level = get_task(db, task_id).risk_level

            pr_url = create_pull_request(
                repo_url=repo_url,
                feature_branch=feature_branch,
                base_branch=base_branch,
                task_id=task_id,
                goal=goal,
                steps=final_steps_for_pr,
                risk_level=risk_level,
            )
            if pr_url:
                with get_db() as db:
                    update_task(db, task_id, pr_url=pr_url)
                dynamo.put_event(run_id, "github_pr.created", {"pr_url": pr_url})

        # ── 5. Generate report ────────────────────────────────────────────────
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

        from src.skills.reporter.execution import generate_report
        report_result = generate_report(
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
            task_skill_content=task_skill_content,
            pr_url=pr_url,
            branch=feature_branch,
        )

        # ── 6. Finalise task ──────────────────────────────────────────────────
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
            "pr_url": pr_url,
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
