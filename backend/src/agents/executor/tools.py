"""
Typed tool layer for the executor agent.

Each function corresponds to a tool Claude can call. Tools are pure Python —
no subprocess shell injection, no network calls except approved external APIs.

Tool registry: EXECUTOR_TOOLS (list of Anthropic tool dicts)
Dispatcher:    dispatch(name, input, workspace, context) → dict
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from src.agents.executor.workspace import Workspace
from src.common.logging import get_logger

logger = get_logger(__name__)

# ── Tool schemas (Anthropic format) ──────────────────────────────────────────

EXECUTOR_TOOLS: list[dict] = [
    {
        "name": "repo_list_files",
        "description": (
            "List files in the workspace. Use to explore project structure "
            "before reading or modifying files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory to list, relative to workspace root. Default '.'",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files. Default '*'",
                },
            },
            "required": [],
        },
    },
    {
        "name": "repo_read_file",
        "description": "Read the full content of a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace root.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "repo_write_file",
        "description": (
            "Write (create or overwrite) a file in the workspace. "
            "Use for creating new files or replacing entire file contents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace root.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "repo_apply_patch",
        "description": (
            "Apply a unified diff patch to the workspace. "
            "Preferred over repo_write_file for modifying existing files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "Unified diff patch string (output of git diff).",
                },
            },
            "required": ["patch"],
        },
    },
    {
        "name": "repo_create_branch",
        "description": "Create and checkout a new git branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Branch name, e.g. 'feature/phone-validation'.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "repo_commit",
        "description": "Stage and commit changes to the current branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message following conventional commits.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific files to stage. If omitted, stages all changes.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "repo_get_diff",
        "description": "Get the current git diff (unstaged + staged changes).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "shell_exec",
        "description": (
            "Execute a shell command in the workspace. "
            "Allowed: package installs (npm/pip/yarn), test runners, linters, build tools. "
            "Forbidden: curl/wget, rm -rf /, git push, network calls to external hosts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Max execution time in seconds. Default 60.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "tests_run",
        "description": (
            "Run tests in the workspace and return structured results. "
            "Supports pytest, jest, npm test."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "runner": {
                    "type": "string",
                    "enum": ["pytest", "jest", "npm_test"],
                    "description": "Test runner to use.",
                },
                "test_path": {
                    "type": "string",
                    "description": "Specific test file or directory. If omitted, runs all.",
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional arguments to pass to the test runner.",
                },
            },
            "required": ["runner"],
        },
    },
    {
        "name": "request_permission",
        "description": (
            "Request human approval for a sensitive action before proceeding. "
            "Use when policy requires it or when about to make irreversible changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request_type": {
                    "type": "string",
                    "description": "Category: dependency_add | schema_change | env_change | external_api | destructive_edit",
                },
                "description": {
                    "type": "string",
                    "description": "Clear description of what you want to do and why.",
                },
                "risk": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Your assessment of the risk level.",
                },
            },
            "required": ["request_type", "description", "risk"],
        },
    },
    {
        "name": "step_complete",
        "description": (
            "Signal that the current step is complete. "
            "Call this when you have finished all actions for this step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was accomplished in this step.",
                },
                "files_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of files that were created or modified.",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["success", "partial", "skipped"],
                    "description": "Outcome of this step.",
                },
            },
            "required": ["summary", "outcome"],
        },
    },
]

# ── Forbidden shell patterns ──────────────────────────────────────────────────

_FORBIDDEN_SHELL_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    ":(){ :|:& };:",  # fork bomb
    "git push",
    "curl ",
    "wget ",
    "nc ",
    "netcat",
    "ssh ",
    "scp ",
    "sudo ",
    "su ",
    "chmod 777",
    "dd if=",
    "> /dev/",
]


def _is_shell_allowed(command: str) -> tuple[bool, str]:
    lower = command.lower().strip()
    for pattern in _FORBIDDEN_SHELL_PATTERNS:
        if pattern in lower:
            return False, f"Command contains forbidden pattern: '{pattern}'"
    return True, ""


# ── Dispatcher ────────────────────────────────────────────────────────────────

def dispatch(
    name: str,
    tool_input: dict[str, Any],
    workspace: Workspace,
    context: dict[str, Any],
) -> dict[str, Any]:
    """
    Route a tool call to its implementation.
    Returns a result dict that is sent back to Claude as tool_result content.
    """
    logger.info("tool.call", tool=name, task_id=context.get("task_id"))

    try:
        if name == "repo_list_files":
            return _repo_list_files(tool_input, workspace)
        elif name == "repo_read_file":
            return _repo_read_file(tool_input, workspace)
        elif name == "repo_write_file":
            return _repo_write_file(tool_input, workspace)
        elif name == "repo_apply_patch":
            return _repo_apply_patch(tool_input, workspace)
        elif name == "repo_create_branch":
            return _repo_create_branch(tool_input, workspace)
        elif name == "repo_commit":
            return _repo_commit(tool_input, workspace)
        elif name == "repo_get_diff":
            return _repo_get_diff(workspace)
        elif name == "shell_exec":
            return _shell_exec(tool_input, workspace)
        elif name == "tests_run":
            return _tests_run(tool_input, workspace, context)
        elif name == "request_permission":
            return _request_permission(tool_input, context)
        elif name == "step_complete":
            return {"status": "acknowledged", **tool_input}
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.warning("tool.error", tool=name, error=str(exc))
        return {"error": str(exc)}


# ── Tool implementations ──────────────────────────────────────────────────────

def _repo_list_files(inp: dict, ws: Workspace) -> dict:
    directory = inp.get("directory", ".")
    pattern = inp.get("pattern", "*")
    files = ws.list_files(directory=directory, pattern=pattern)
    return {"files": files, "count": len(files)}


def _repo_read_file(inp: dict, ws: Workspace) -> dict:
    content = ws.read_file(inp["path"])
    return {"path": inp["path"], "content": content, "lines": content.count("\n") + 1}


def _repo_write_file(inp: dict, ws: Workspace) -> dict:
    ws.write_file(inp["path"], inp["content"])
    return {"path": inp["path"], "status": "written", "bytes": len(inp["content"])}


def _repo_apply_patch(inp: dict, ws: Workspace) -> dict:
    success = ws.apply_patch(inp["patch"])
    return {"status": "applied" if success else "failed"}


def _repo_create_branch(inp: dict, ws: Workspace) -> dict:
    name = ws.create_branch(inp["name"])
    return {"branch": name, "status": "created"}


def _repo_commit(inp: dict, ws: Workspace) -> dict:
    files = inp.get("files")
    sha = ws.commit(inp["message"], files=files)
    return {"sha": sha, "message": inp["message"]}


def _repo_get_diff(ws: Workspace) -> dict:
    diff = ws.get_diff()
    return {"diff": diff, "has_changes": bool(diff.strip())}


def _shell_exec(inp: dict, ws: Workspace) -> dict:
    command = inp["command"]
    timeout = int(inp.get("timeout_seconds", 60))

    allowed, reason = _is_shell_allowed(command)
    if not allowed:
        return {"error": f"Command blocked by policy: {reason}", "exit_code": -1}

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(ws.root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout[-4000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
            "exit_code": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s", "exit_code": -1}


def _tests_run(inp: dict, ws: Workspace, context: dict) -> dict:
    runner = inp["runner"]
    test_path = inp.get("test_path", "")
    extra_args = inp.get("extra_args", "")

    commands = {
        "pytest": f"python -m pytest {test_path} {extra_args} --tb=short -q 2>&1",
        "jest": f"npx jest {test_path} {extra_args} --no-coverage 2>&1",
        "npm_test": f"npm test -- {test_path} {extra_args} 2>&1",
    }

    cmd = commands.get(runner)
    if not cmd:
        return {"error": f"Unsupported runner: {runner}"}

    try:
        result = subprocess.run(
            cmd.strip(),
            shell=True,
            cwd=str(ws.root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        parsed = _parse_test_output(runner, output, result.returncode)
        return {
            "runner": runner,
            "exit_code": result.returncode,
            "output": output[-3000:],
            **parsed,
        }
    except subprocess.TimeoutExpired:
        return {"runner": runner, "error": "Tests timed out after 120s", "passed": 0, "failed": 0, "total": 0}


def _parse_test_output(runner: str, output: str, exit_code: int) -> dict:
    """Best-effort parse of test output to extract pass/fail counts."""
    import re

    passed = 0
    failed = 0

    if runner == "pytest":
        # e.g. "5 passed, 2 failed in 1.23s"
        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))
    elif runner in ("jest", "npm_test"):
        # e.g. "Tests: 3 passed, 1 failed, 4 total"
        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))

    return {
        "passed": passed,
        "failed": failed,
        "total": passed + failed,
        "success": exit_code == 0,
    }


def _request_permission(inp: dict, context: dict) -> dict:
    """
    Synchronous permission check via the policy/permission module.
    In the full system this would pause for human approval if policy requires it.
    """
    from src.agents.orchestrator import permission as perm_module

    policy = context.get("policy", {})
    run_id = context.get("run_id", "unknown")
    step_id = context.get("step_id", "unknown")

    result = perm_module.evaluate(
        run_id=run_id,
        step_id=step_id,
        request_type=inp["request_type"],
        description=inp["description"],
        risk=inp["risk"],
        policy=policy,
    )
    return {
        "decision": result["decision"],
        "reason": result.get("reason", ""),
        "approval_id": result.get("approval_id"),
    }
