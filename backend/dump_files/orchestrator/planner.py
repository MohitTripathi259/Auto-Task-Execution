"""
Planner — breaks a goal into a structured, step-by-step execution plan.
Uses raw Anthropic Messages API (control plane).
"""

import json
from pathlib import Path

from src.common.config import settings
from src.common.logging import get_logger
from src.common.utils import generate_step_id
from src.llm.client import count_tokens, invoke_text

logger = get_logger(__name__)

_SYSTEM_PROMPT: str | None = None


def _load_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        prompt_path = Path(__file__).parent.parent.parent / "llm" / "prompts" / "planner_system.md"
        _SYSTEM_PROMPT = prompt_path.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


def _build_user_message(goal: str, repo_url: str, base_branch: str) -> str:
    return f"""Task to plan:

GOAL: {goal}
REPO: {repo_url}
BASE BRANCH: {base_branch}

Break this into concrete steps. Return only the JSON plan."""


def create_plan(goal: str, repo_url: str, base_branch: str) -> dict:
    """
    Calls Claude to decompose a goal into an execution plan.

    Returns a dict matching the planner_system.md output schema:
    {
        "goal": str,
        "risk_level": "low|medium|high",
        "estimated_cost_tier": "minimal|moderate|high",
        "steps": [{"step_id", "index", "action", "expected_output", "tools_likely_needed", "risk"}],
        "total_steps": int
    }
    """
    system = _load_system_prompt()
    user = _build_user_message(goal, repo_url, base_branch)

    # Preflight token check
    estimated_tokens = count_tokens(system, user)
    logger.info("planner.token_estimate", tokens=estimated_tokens)

    raw = invoke_text(
        system=system,
        user=user,
        model=settings.CLAUDE_PLANNER_MODEL,
        max_tokens=2048,
        temperature=0.1,  # low temp for structured output
    )

    plan = _parse_plan(raw, goal)
    plan = _normalise_step_ids(plan)

    logger.info(
        "planner.plan_created",
        goal=goal[:60],
        risk_level=plan.get("risk_level"),
        total_steps=plan.get("total_steps"),
    )
    return plan


def _parse_plan(raw: str, goal: str) -> dict:
    """Parse LLM output into a plan dict. Falls back to a safe minimal plan on error."""
    # Strip markdown fences if model added them
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
    """Ensure step_ids follow our s001, s002 format."""
    for i, step in enumerate(plan["steps"]):
        step["step_id"] = generate_step_id(i + 1)
        step["index"] = i
    plan["total_steps"] = len(plan["steps"])
    return plan


def _fallback_plan(goal: str) -> dict:
    """Minimal safe plan when parsing fails — single analysis step."""
    logger.warning("planner.using_fallback_plan")
    return {
        "goal": goal,
        "risk_level": "medium",
        "estimated_cost_tier": "minimal",
        "steps": [
            {
                "step_id": "s001",
                "index": 0,
                "action": f"Analyze codebase and implement: {goal}",
                "expected_output": "Task completed with tests passing",
                "tools_likely_needed": ["repo_read_file", "repo_apply_patch", "tests_run_unit"],
                "risk": "medium",
            }
        ],
        "total_steps": 1,
    }
