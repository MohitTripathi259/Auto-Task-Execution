"""
Reporter skill execution — generates a structured end-of-task summary.
Loads spec from skill.md, calls Claude, saves report to S3.
"""

import json

from src.common.config import settings
from src.common.logging import get_logger
from src.common.utils import s3_artifact_key, utc_now_iso
from src.llm.client import invoke_text
from src.skills.loader import build_system_prompt
from src.storage import s3

logger = get_logger(__name__)


def generate_report(
    task_id: str,
    run_id: str,
    tenant_id: str,
    goal: str,
    steps: list[dict],
    test_runs: list[dict],
    approvals: list[dict],
    started_at: str,
    ended_at: str,
    token_usage: dict | None = None,
    task_skill_content: str | None = None,
    pr_url: str | None = None,
    branch: str | None = None,
) -> dict:
    system = build_system_prompt("reporter", task_skill_content)
    user = _build_user_message(
        goal, steps, test_runs, approvals,
        started_at, ended_at, token_usage, pr_url, branch
    )

    raw = invoke_text(
        system=system,
        user=user,
        model=settings.CLAUDE_PLANNER_MODEL,
        max_tokens=1500,
        temperature=0.2,
    )

    report = _parse_report(raw, goal, steps, test_runs)
    report["task_id"] = task_id
    report["run_id"] = run_id
    report["generated_at"] = utc_now_iso()
    if pr_url:
        report.setdefault("artifacts", {})["pr_url"] = pr_url
    if branch:
        report.setdefault("artifacts", {})["branch"] = branch

    s3_key = s3_artifact_key(tenant_id, run_id, "report.json")
    s3.put_json(s3_key, report)

    logger.info("reporter.report_saved", task_id=task_id, s3_key=s3_key, status=report.get("status"))
    return {"report": report, "s3_key": s3_key}


def _build_user_message(
    goal, steps, test_runs, approvals,
    started_at, ended_at, token_usage, pr_url, branch
) -> str:
    completed = [s for s in steps if s.get("status") == "completed"]
    failed    = [s for s in steps if s.get("status") == "failed"]

    steps_detail = "\n".join(
        f"  [{s.get('status','?').upper()}] {s.get('step_id')}: {s.get('action','')[:80]}"
        for s in steps
    )
    approval_detail = "\n".join(
        f"  [{a.get('decision','?').upper()}] {a.get('request_type')}: {a.get('description','')[:60]}"
        for a in approvals
    ) or "  None"

    token_info = ""
    if token_usage:
        token_info = f"\nTOKEN USAGE:\n  Input: {token_usage.get('input_tokens',0):,}\n  Output: {token_usage.get('output_tokens',0):,}"

    pr_info = f"\nGITHUB PR: {pr_url}" if pr_url else ""
    branch_info = f"\nBRANCH: {branch}" if branch else ""

    return f"""Summarize this completed autonomous coding task:

GOAL: {goal}
TIME: {started_at} → {ended_at}
{pr_info}{branch_info}

STEPS ({len(completed)} completed, {len(failed)} failed):
{steps_detail}

TESTS:
  Passed: {sum(t.get('passed',0) for t in test_runs)}
  Failed: {sum(t.get('failed',0) for t in test_runs)}

APPROVALS:
{approval_detail}
{token_info}

Return the JSON report."""


def _parse_report(raw, goal, steps, test_runs) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("reporter.parse_failed", error=str(exc))
        return _fallback_report(goal, steps, test_runs)


def _fallback_report(goal, steps, test_runs) -> dict:
    completed = len([s for s in steps if s.get("status") == "completed"])
    failed_steps = len([s for s in steps if s.get("status") == "failed"])
    status = "completed" if failed_steps == 0 else ("partial" if completed > 0 else "failed")
    return {
        "title": f"Task {status}: {goal[:60]}",
        "status": status,
        "goal": goal,
        "summary": f"{completed}/{len(steps)} steps completed. {sum(t.get('passed',0) for t in test_runs)} tests passed.",
        "steps_completed": completed,
        "steps_failed": failed_steps,
        "steps_skipped": len(steps) - completed - failed_steps,
        "tests": {"passed": sum(t.get("passed",0) for t in test_runs), "failed": sum(t.get("failed",0) for t in test_runs), "suites_run": []},
        "approvals": {"granted": 0, "denied": 0, "key_decisions": []},
        "artifacts": {"pr_url": None, "branch": None, "commits": 0},
        "cost": {"estimated_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        "requires_attention": [],
        "next_steps": [],
        "duration_minutes": 0,
    }
