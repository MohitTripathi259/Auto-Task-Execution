"""
Executor runner — executes a single step using Claude tool-use.

Flow per step:
  1. Build system prompt + step context
  2. Run Claude Messages API tool-use loop (max N turns)
  3. Each tool call → dispatch() → tool result back to Claude
  4. Loop ends on step_complete tool call, or max_iterations, or error
  5. Return StepResult

This module is intentionally synchronous; the orchestrator awaits it in a thread pool.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.agents.executor.tools import EXECUTOR_TOOLS, dispatch
from src.agents.executor.workspace import Workspace
from src.common.config import settings
from src.common.logging import get_logger
from src.llm.client import invoke_with_tools

logger = get_logger(__name__)

MAX_TOOL_ITERATIONS = 30  # hard cap per step

EXECUTOR_SYSTEM_PROMPT = """\
You are an autonomous software engineering agent executing a single well-defined step.

You have access to tools to read, modify, and test code in your workspace.
Work methodically:
1. Read relevant files to understand context before making changes.
2. Make targeted, minimal changes.
3. Verify changes with tests when possible.
4. Commit your work with a clear message.
5. Call step_complete when done.

Rules:
- Only modify files relevant to the step. Do not refactor unrelated code.
- Prefer repo_apply_patch for modifying existing files.
- Always check for existing patterns/libraries before adding new dependencies.
- If you need permission for a sensitive action (adding dependencies, env changes),
  call request_permission first and wait for the decision.
- If a tool call fails, reason about the error and try an alternative approach.
- Do not loop forever. If after 3 attempts you cannot complete the step, call
  step_complete with outcome="partial" and explain what was done and what was blocked.
"""


@dataclass
class StepResult:
    step_id: str
    status: str          # completed | failed | partial
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
) -> StepResult:
    """
    Execute a single step. Synchronous — call via asyncio.to_thread from the orchestrator.
    """
    context = {
        "task_id": task_id,
        "run_id": run_id,
        "step_id": step_id,
        "policy": policy,
    }

    base_sha = workspace.current_sha()
    tool_call_log: list[dict] = []
    token_usage = {"input_tokens": 0, "output_tokens": 0}
    test_results: list[dict] = []

    # Build initial user message
    user_message = _build_step_message(step_id, action, expected_output, workspace)

    messages: list[dict] = [{"role": "user", "content": user_message}]

    logger.info("executor.step.start", step_id=step_id, action=action[:80])

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = invoke_with_tools(
            system=EXECUTOR_SYSTEM_PROMPT,
            messages=messages,
            tools=EXECUTOR_TOOLS,
            model=settings.CLAUDE_EXECUTOR_MODEL,
            max_tokens=4096,
        )

        # Track token usage
        token_usage["input_tokens"] += response.usage.input_tokens
        token_usage["output_tokens"] += response.usage.output_tokens

        stop_reason = response.stop_reason
        # Convert content blocks to dicts for consistent handling
        content_blocks = [
            block.model_dump() if hasattr(block, "model_dump") else block
            for block in response.content
        ]

        # Add assistant response to conversation
        messages.append({"role": "assistant", "content": content_blocks})

        if stop_reason == "end_turn":
            # Claude finished without calling a tool — treat as completion
            text = _extract_text(content_blocks)
            return StepResult(
                step_id=step_id,
                status="completed",
                summary=text[:500] if text else "Step completed.",
                files_changed=[],
                tool_calls=tool_call_log,
                base_sha=base_sha,
                head_sha=workspace.current_sha(),
                token_usage=token_usage,
                test_results=test_results,
            )

        if stop_reason != "tool_use":
            logger.warning("executor.unexpected_stop", stop_reason=stop_reason, iteration=iteration)
            break

        # Process all tool calls in this response
        tool_results = []
        step_complete_result = None

        for block in content_blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue

            tool_name = block["name"]
            tool_input = block.get("input", {})
            tool_use_id = block["id"]

            logger.debug("executor.tool_call", tool=tool_name, iteration=iteration)
            tool_call_log.append({
                "tool": tool_name,
                "input": tool_input,
                "iteration": iteration,
            })

            result = dispatch(tool_name, tool_input, workspace, context)

            # Capture test results for tracking
            if tool_name == "tests_run" and "passed" in result:
                test_results.append({
                    "runner": tool_input.get("runner", "unknown"),
                    "passed": result.get("passed", 0),
                    "failed": result.get("failed", 0),
                    "total": result.get("total", 0),
                    "success": result.get("success", False),
                })

            if tool_name == "step_complete":
                step_complete_result = result

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": _serialize_result(result),
            })

        # Append tool results as user turn
        messages.append({"role": "user", "content": tool_results})

        # If step_complete was called, we're done
        if step_complete_result is not None:
            outcome = step_complete_result.get("outcome", "success")
            return StepResult(
                step_id=step_id,
                status="completed" if outcome in ("success", "skipped") else "partial",
                summary=step_complete_result.get("summary", ""),
                files_changed=step_complete_result.get("files_changed", []),
                tool_calls=tool_call_log,
                base_sha=base_sha,
                head_sha=workspace.current_sha(),
                token_usage=token_usage,
                test_results=test_results,
            )

    # Exceeded max iterations
    logger.warning("executor.max_iterations_reached", step_id=step_id)
    return StepResult(
        step_id=step_id,
        status="partial",
        summary=f"Step reached max iterations ({MAX_TOOL_ITERATIONS}) without completing.",
        tool_calls=tool_call_log,
        base_sha=base_sha,
        head_sha=workspace.current_sha(),
        error="max_iterations_exceeded",
        token_usage=token_usage,
        test_results=test_results,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_step_message(
    step_id: str,
    action: str,
    expected_output: str,
    workspace: Workspace,
) -> str:
    # List top-level files to give Claude initial context
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


def _serialize_result(result: dict) -> str:
    """Convert tool result dict to string for Claude's tool_result content."""
    import json
    try:
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


async def run_step_async(
    step_id: str,
    action: str,
    expected_output: str,
    workspace: Workspace,
    policy: dict[str, Any],
    run_id: str,
    task_id: str,
) -> StepResult:
    """Async wrapper — runs the synchronous executor in a thread pool."""
    return await asyncio.to_thread(
        run_step,
        step_id, action, expected_output,
        workspace, policy, run_id, task_id,
    )
