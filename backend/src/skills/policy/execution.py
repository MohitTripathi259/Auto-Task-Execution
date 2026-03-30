"""
Policy skill execution — generates boundary + budget rules for the executor.
Loads spec from skill.md, calls Claude, returns validated policy dict.
"""

import json

from src.common.config import settings
from src.common.logging import get_logger
from src.llm.client import invoke_text
from src.skills.loader import build_system_prompt

logger = get_logger(__name__)


def generate_policy(
    plan: dict,
    max_cost_usd: float,
    max_runtime_minutes: int,
    task_skill_content: str | None = None,
) -> dict:
    system = build_system_prompt("policy", task_skill_content)
    user = _build_user_message(plan, max_cost_usd, max_runtime_minutes)

    raw = invoke_text(
        system=system,
        user=user,
        model=settings.CLAUDE_PLANNER_MODEL,
        max_tokens=1024,
        temperature=0.0,
    )

    policy = _parse_policy(raw, plan, max_cost_usd, max_runtime_minutes)

    # Always enforce the user's budget caps — never exceed them
    policy["max_cost_usd"] = min(policy.get("max_cost_usd", max_cost_usd), max_cost_usd)
    policy["max_runtime_minutes"] = min(
        policy.get("max_runtime_minutes", max_runtime_minutes), max_runtime_minutes
    )

    logger.info("policy.generated",
                risk_level=policy.get("risk_level"),
                max_cost_usd=policy["max_cost_usd"],
                permission_required_for=policy.get("permission_required_for", []))
    return policy


def _build_user_message(plan: dict, max_cost_usd: float, max_runtime_minutes: int) -> str:
    steps_summary = "\n".join(
        f"  - Step {s['step_id']}: {s['action']} (risk: {s.get('risk', 'unknown')})"
        for s in plan.get("steps", [])
    )
    return f"""Generate policy for this execution plan:

GOAL: {plan.get("goal", "")}
RISK LEVEL: {plan.get("risk_level", "medium")}
BUDGET CAP: ${max_cost_usd:.2f}
MAX RUNTIME: {max_runtime_minutes} minutes

STEPS:
{steps_summary}

Return only the JSON policy."""


def _parse_policy(raw: str, plan: dict, max_cost_usd: float, max_runtime_minutes: int) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("policy.parse_failed", error=str(exc))
        return _default_policy(plan.get("risk_level", "medium"), max_cost_usd, max_runtime_minutes)


def _default_policy(risk_level: str, max_cost_usd: float, max_runtime_minutes: int) -> dict:
    base = {
        "allowed_tools": [
            "repo_read_file", "repo_create_branch", "repo_apply_patch",
            "repo_commit", "repo_get_diff", "tests_run",
        ],
        "forbidden_actions": [
            "push to main directly",
            "delete files without approval",
            "access secrets or env vars not in plan",
        ],
        "allowed_paths": ["/workspace/src", "/workspace/tests"],
        "forbidden_paths": ["/workspace/.env", "/workspace/config/secrets"],
        "auto_approved": ["repo_read_file", "repo_get_diff"],
        "max_runtime_minutes": max_runtime_minutes,
        "max_file_writes": 30,
        "max_tool_calls": settings.DEFAULT_MAX_TOOL_CALLS,
        "max_input_tokens": settings.DEFAULT_MAX_INPUT_TOKENS,
        "max_output_tokens": settings.DEFAULT_MAX_OUTPUT_TOKENS,
        "max_cost_usd": max_cost_usd,
        "risk_level": risk_level,
        "notes": "Default policy (LLM parse failed)",
    }
    if risk_level == "low":
        base["permission_required_for"] = ["dependency_add", "env_var_access"]
    elif risk_level == "medium":
        base["permission_required_for"] = ["dependency_add", "env_var_access", "shared_component_edit"]
        base["max_file_writes"] = 20
    else:
        base["permission_required_for"] = [
            "dependency_add", "env_var_access", "shared_component_edit",
            "schema_change", "auth_change", "config_change",
        ]
        base["max_file_writes"] = 10
        base["auto_approved"] = ["repo_read_file"]
    return base
