"""
Executor skill execution — runs a single step via Claude tool-use loop.
Loads spec from skill.md (+ optional task skill), dispatches tools, returns StepResult.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from src.agents.executor.tools import EXECUTOR_TOOLS, dispatch
from src.agents.executor.workspace import Workspace
from src.common.config import settings
from src.common.logging import get_logger
from src.llm.client import invoke_with_tools
from src.skills.loader import build_system_prompt

logger = get_logger(__name__)

MAX_TOOL_ITERATIONS = 30


@dataclass
class StepResult:
    step_id: str
    status: str
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    base_sha: str = ""
    head_sha: str = ""
    error: str | None = None
    token_usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    test_results: list[dict] = field(default_factory=list)


def run_step(
    step_id: str,
    action: str,
    expected_output: str,
    workspace: Workspace,
    policy: dict[str, Any],
    run_id: str,
    task_id: str,
    task_skill_content: str | None = None,
) -> StepResult:
    """
    Execute a single step synchronously.
    Call via run_step_async from the orchestrator.
    """
    system = build_system_prompt("executor", task_skill_content)
    context = {"task_id": task_id, "run_id": run_id, "step_id": step_id, "policy": policy}

    base_sha = workspace.current_sha()
    tool_call_log: list[dict] = []
    token_usage = {"input_tokens": 0, "output_tokens": 0}
    test_results: list[dict] = []

    user_message = _build_step_message(step_id, action, expected_output, workspace)
    messages: list[dict] = [{"role": "user", "content": user_message}]

    logger.info("executor.step.start", step_id=step_id, action=action[:80])

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = invoke_with_tools(
            system=system,
            messages=messages,
            tools=EXECUTOR_TOOLS,
            model=settings.CLAUDE_EXECUTOR_MODEL,
            max_tokens=4096,
        )

        token_usage["input_tokens"]  += response.usage.input_tokens
        token_usage["output_tokens"] += response.usage.output_tokens

        stop_reason = response.stop_reason
        content_blocks = [
            b.model_dump() if hasattr(b, "model_dump") else b
            for b in response.content
        ]
        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason == "end_turn":
            text = _extract_text(content_blocks)
            return StepResult(
                step_id=step_id, status="completed",
                summary=text[:500] if text else "Step completed.",
                tool_calls=tool_call_log, base_sha=base_sha,
                head_sha=workspace.current_sha(), token_usage=token_usage,
                test_results=test_results,
            )

        if stop_reason != "tool_use":
            break

        tool_results = []
        step_complete_result = None

        for block in content_blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue

            tool_name    = block["name"]
            tool_input   = block.get("input", {})
            tool_use_id  = block["id"]

            logger.debug("executor.tool_call", tool=tool_name, iteration=iteration)
            tool_call_log.append({"tool": tool_name, "input": tool_input, "iteration": iteration})

            result = dispatch(tool_name, tool_input, workspace, context)

            if tool_name == "tests_run" and "passed" in result:
                test_results.append({
                    "runner": tool_input.get("runner", "unknown"),
                    "passed": result.get("passed", 0),
                    "failed": result.get("failed", 0),
                    "total":  result.get("total", 0),
                    "success": result.get("success", False),
                })

            if tool_name == "step_complete":
                step_complete_result = result

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

        if step_complete_result is not None:
            outcome = step_complete_result.get("outcome", "success")
            return StepResult(
                step_id=step_id,
                status="completed" if outcome in ("success", "skipped") else "partial",
                summary=step_complete_result.get("summary", ""),
                files_changed=step_complete_result.get("files_changed", []),
                tool_calls=tool_call_log, base_sha=base_sha,
                head_sha=workspace.current_sha(), token_usage=token_usage,
                test_results=test_results,
            )

    logger.warning("executor.max_iterations_reached", step_id=step_id)
    return StepResult(
        step_id=step_id, status="partial",
        summary=f"Step reached max iterations ({MAX_TOOL_ITERATIONS}) without completing.",
        tool_calls=tool_call_log, base_sha=base_sha,
        head_sha=workspace.current_sha(), error="max_iterations_exceeded",
        token_usage=token_usage, test_results=test_results,
    )


async def run_step_async(
    step_id: str,
    action: str,
    expected_output: str,
    workspace: Workspace,
    policy: dict[str, Any],
    run_id: str,
    task_id: str,
    task_skill_content: str | None = None,
) -> StepResult:
    return await asyncio.to_thread(
        run_step,
        step_id, action, expected_output,
        workspace, policy, run_id, task_id, task_skill_content,
    )


def _build_step_message(step_id, action, expected_output, workspace) -> str:
    try:
        files = workspace.list_files(pattern="*")[:20]
        file_list = "\n".join(f"  - {f}" for f in files) if files else "  (empty workspace)"
    except Exception:
        file_list = "  (could not list files)"

    return f"""Step ID: {step_id}

Action to complete:
{action}

Expected output:
{expected_output}

Workspace overview (top files):
{file_list}

Begin by reading the relevant files, then make your changes, run tests if applicable, and call step_complete when done."""


def _extract_text(content_blocks: list) -> str:
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return ""
