"""
Permission Gate — evaluates executor permission requests against the current policy.
Uses raw Anthropic Messages API (control plane).
"""

import json
from pathlib import Path

from src.common.config import settings
from src.common.logging import get_logger
from src.common.utils import generate_approval_id, utc_now_iso
from src.llm.client import invoke_text
from src.storage import dynamo

logger = get_logger(__name__)

_SYSTEM_PROMPT: str | None = None


def _load_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        prompt_path = (
            Path(__file__).parent.parent.parent / "llm" / "prompts" / "permission_system.md"
        )
        _SYSTEM_PROMPT = prompt_path.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


def evaluate(
    run_id: str,
    step_id: str,
    request_type: str,
    description: str,
    risk: str,
    policy: dict,
) -> dict:
    """
    Evaluates a permission request from the executor.

    Returns:
    {
        "approval_id": str,
        "decision": "approved|rejected",
        "reason": str,
        "alternative": str | None
    }
    """
    approval_id = generate_approval_id()

    # Fast-path: auto-approve if in policy's auto_approved list
    auto_approved = policy.get("auto_approved", [])
    if request_type in auto_approved:
        result = {
            "approval_id": approval_id,
            "decision": "approved",
            "reason": f"Auto-approved: '{request_type}' is in the policy auto-approve list",
            "alternative": None,
        }
        _log_decision(run_id, step_id, approval_id, request_type, description, result)
        return result

    # Fast-path: hard reject if action is explicitly forbidden
    forbidden = policy.get("forbidden_actions", [])
    for forbidden_action in forbidden:
        if forbidden_action.lower() in description.lower():
            result = {
                "approval_id": approval_id,
                "decision": "rejected",
                "reason": f"Action matches forbidden policy rule: '{forbidden_action}'",
                "alternative": "Find an alternative approach that does not require this action",
            }
            _log_decision(run_id, step_id, approval_id, request_type, description, result)
            return result

    # LLM evaluation for everything else
    result = _llm_evaluate(
        approval_id, run_id, step_id, request_type, description, risk, policy
    )
    _log_decision(run_id, step_id, approval_id, request_type, description, result)
    return result


def _llm_evaluate(
    approval_id: str,
    run_id: str,
    step_id: str,
    request_type: str,
    description: str,
    risk: str,
    policy: dict,
) -> dict:
    system = _load_system_prompt()
    user = f"""Permission request from executor:

REQUEST TYPE: {request_type}
DESCRIPTION: {description}
RISK ASSESSMENT: {risk}
CURRENT STEP: {step_id}

ACTIVE POLICY:
- Risk level: {policy.get("risk_level", "unknown")}
- Permission required for: {policy.get("permission_required_for", [])}
- Forbidden actions: {policy.get("forbidden_actions", [])}
- Allowed paths: {policy.get("allowed_paths", [])}
- Forbidden paths: {policy.get("forbidden_paths", [])}

Evaluate this request and return your decision as JSON."""

    raw = invoke_text(
        system=system,
        user=user,
        model=settings.CLAUDE_PLANNER_MODEL,
        max_tokens=512,
        temperature=0.0,
    )

    try:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        decision = json.loads(text)
        decision["approval_id"] = approval_id
        return decision
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("permission.parse_failed", error=str(exc))
        # Conservative fallback: reject and require human review
        return {
            "approval_id": approval_id,
            "decision": "rejected",
            "reason": "Could not evaluate request automatically — requires human review",
            "alternative": "Wait for human approval via /v1/approvals endpoint",
        }


def _log_decision(
    run_id: str,
    step_id: str,
    approval_id: str,
    request_type: str,
    description: str,
    result: dict,
) -> None:
    dynamo.put_event(
        run_id=run_id,
        event_type="permission.evaluated",
        payload={
            "approval_id": approval_id,
            "request_type": request_type,
            "description": description,
            "decision": result["decision"],
            "reason": result.get("reason"),
        },
        step_id=step_id,
        approval_id=approval_id,
    )
    logger.info(
        "permission.evaluated",
        approval_id=approval_id,
        request_type=request_type,
        decision=result["decision"],
    )
