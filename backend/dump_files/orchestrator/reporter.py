"""
Reporter — generates a structured end-of-task summary for the owner.
Uses raw Anthropic Messages API (control plane).
"""

import json
from pathlib import Path

from src.common.config import settings
from src.common.logging import get_logger
from src.common.utils import s3_artifact_key, utc_now_iso
from src.llm.client import invoke_text
from src.storage import s3

logger = get_logger(__name__)

_SYSTEM_PROMPT: str | None = None


def _load_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        prompt_path = (
            Path(__file__).parent.parent.parent / "llm" / "prompts" / "reporter_system.md"
        )
        _SYSTEM_PROMPT = prompt_path.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


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
) -> dict:
    """
    Generates a structured summary and saves it to S3.

    Returns the report dict + the S3 key where it was saved.
    """
    system = _load_system_prompt()
    user = _build_user_message(
        goal, steps, test_runs, approvals, started_at, ended_at, token_usage
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

    # Save to S3
    s3_key = s3_artifact_key(tenant_id, run_id, "report.json")
    s3.put_json(s3_key, report)

    logger.info(
        "reporter.report_saved",
        task_id=task_id,
        run_id=run_id,
        s3_key=s3_key,
        status=report.get("status"),
    )
    return {"report": report, "s3_key": s3_key}


def _build_user_message(
    goal: str,
    steps: list[dict],
    test_runs: list[dict],
    approvals: list[dict],
    started_at: str,
    ended_at: str,
    token_usage: dict | None,
) -> str:
    completed = [s for s in steps if s.get("status") == "completed"]
    failed = [s for s in steps if s.get("status") == "failed"]
    skipped = [s for s in steps if s.get("status") == "skipped"]

    tests_passed = sum(t.get("passed", 0) for t in test_runs)
    tests_failed = sum(t.get("failed", 0) for t in test_runs)

    approvals_granted = sum(1 for a in approvals if a.get("decision") == "approved")
    approvals_denied = sum(1 for a in approvals if a.get("decision") == "rejected")

    steps_detail = "\n".join(
        f"  [{s.get('status', '?').upper()}] {s.get('step_id')}: {s.get('action', '')[:80]}"
        for s in steps
    )

    approval_detail = "\n".join(
        f"  [{a.get('decision', '?').upper()}] {a.get('request_type')}: {a.get('description', '')[:60]}"
        for a in approvals
    ) or "  None"

    token_info = ""
    if token_usage:
        token_info = f"""
TOKEN USAGE:
  Input: {token_usage.get("input_tokens", 0):,}
  Output: {token_usage.get("output_tokens", 0):,}
  Estimated cost: ${token_usage.get("estimated_usd", 0):.4f}"""

    return f"""Summarize this completed autonomous coding task:

GOAL: {goal}

TIME:
  Started: {started_at}
  Ended: {ended_at}

STEPS ({len(completed)} completed, {len(failed)} failed, {len(skipped)} skipped):
{steps_detail}

TEST RESULTS:
  Passed: {tests_passed}
  Failed: {tests_failed}
  Suites run: {[t.get("suite") for t in test_runs]}

APPROVAL DECISIONS ({approvals_granted} granted, {approvals_denied} denied):
{approval_detail}
{token_info}

Return the JSON report."""


def _parse_report(
    raw: str,
    goal: str,
    steps: list[dict],
    test_runs: list[dict],
) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("reporter.parse_failed", error=str(exc))
        return _fallback_report(goal, steps, test_runs)


def _fallback_report(goal: str, steps: list[dict], test_runs: list[dict]) -> dict:
    completed = len([s for s in steps if s.get("status") == "completed"])
    failed_steps = len([s for s in steps if s.get("status") == "failed"])
    status = "completed" if failed_steps == 0 else ("partial" if completed > 0 else "failed")

    return {
        "title": f"Task {'completed' if status == 'completed' else 'partially completed'}: {goal[:60]}",
        "status": status,
        "goal": goal,
        "summary": f"{completed}/{len(steps)} steps completed. {sum(t.get('passed', 0) for t in test_runs)} tests passed.",
        "steps_completed": completed,
        "steps_failed": failed_steps,
        "steps_skipped": len(steps) - completed - failed_steps,
        "tests": {
            "passed": sum(t.get("passed", 0) for t in test_runs),
            "failed": sum(t.get("failed", 0) for t in test_runs),
            "suites_run": list({t.get("suite") for t in test_runs if t.get("suite")}),
        },
        "approvals": {"granted": 0, "denied": 0, "key_decisions": []},
        "artifacts": {"pr_url": None, "branch": None, "commits": 0},
        "cost": {"estimated_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        "requires_attention": ["Report generation failed — review logs manually"],
        "next_steps": [],
        "duration_minutes": 0,
    }
