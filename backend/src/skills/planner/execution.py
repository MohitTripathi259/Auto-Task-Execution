"""
Planner skill execution — decomposes a goal into a structured step plan.
Loads spec from skill.md, calls Claude, returns validated plan dict.
"""

import json

from src.common.config import settings
from src.common.logging import get_logger
from src.common.utils import generate_step_id
from src.llm.client import count_tokens, invoke_text
from src.skills.loader import build_system_prompt

logger = get_logger(__name__)


def create_plan(
    goal: str,
    repo_url: str,
    base_branch: str,
    task_skill_content: str | None = None,
) -> dict:
    """
    Decompose a goal into an execution plan.

    task_skill_content: optional user-uploaded skill markdown that overrides/extends
                        the base planner skill for domain-specific instructions.
    """
    system = build_system_prompt("planner", task_skill_content)
    user = _build_user_message(goal, repo_url, base_branch)

    estimated_tokens = count_tokens(system, user)
    logger.info("planner.token_estimate", tokens=estimated_tokens)

    raw = invoke_text(
        system=system,
        user=user,
        model=settings.CLAUDE_PLANNER_MODEL,
        max_tokens=2048,
        temperature=0.1,
    )

    plan = _parse_plan(raw, goal)
    plan = _normalise_step_ids(plan)

    logger.info("planner.plan_created",
                goal=goal[:60],
                risk_level=plan.get("risk_level"),
                total_steps=plan.get("total_steps"))
    return plan


def _build_user_message(goal: str, repo_url: str, base_branch: str) -> str:
    return f"""Task to plan:

GOAL: {goal}
REPO: {repo_url or "(demo workspace — no real repo)"}
BASE BRANCH: {base_branch}

Break this into concrete steps. Return only the JSON plan."""


def _parse_plan(raw: str, goal: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    try:
        plan = json.loads(text)
        _validate_plan_schema(plan)
        return plan
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("planner.parse_failed", error=str(exc), raw_preview=raw[:200])
        return _fallback_plan(goal)


def _validate_plan_schema(plan: dict) -> None:
    required = {"goal", "risk_level", "steps", "total_steps"}
    missing = required - plan.keys()
    if missing:
        raise ValueError(f"Plan missing required fields: {missing}")
    if not isinstance(plan["steps"], list) or len(plan["steps"]) == 0:
        raise ValueError("Plan must have at least one step")
    for step in plan["steps"]:
        for field in ("step_id", "index", "action", "expected_output"):
            if field not in step:
                raise ValueError(f"Step missing field: {field}")


def _normalise_step_ids(plan: dict) -> dict:
    for i, step in enumerate(plan["steps"]):
        step["step_id"] = generate_step_id(i + 1)
        step["index"] = i
    plan["total_steps"] = len(plan["steps"])
    return plan


def _fallback_plan(goal: str) -> dict:
    logger.warning("planner.using_fallback_plan")
    return {
        "goal": goal,
        "risk_level": "medium",
        "estimated_cost_tier": "minimal",
        "steps": [{
            "step_id": "s001",
            "index": 0,
            "action": f"Analyze codebase and implement: {goal}",
            "expected_output": "Task completed with tests passing",
            "tools_likely_needed": ["repo_read_file", "repo_apply_patch", "tests_run"],
            "risk": "medium",
        }],
        "total_steps": 1,
    }
