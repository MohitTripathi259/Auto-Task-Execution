"""Unit tests for API request/response schema validation."""

import pytest
from pydantic import ValidationError

from src.api.schemas import ApprovalDecision, BacktrackRequest, CreateTaskRequest


def test_create_task_request_valid():
    req = CreateTaskRequest(
        goal="Add input validation to login form",
        repo_url="https://github.com/org/repo",
    )
    assert req.base_branch == "main"
    assert req.tenant_id == "default"
    assert req.max_cost_usd == 8.0


def test_create_task_request_custom_budget():
    req = CreateTaskRequest(
        goal="Big task",
        repo_url="https://github.com/org/repo",
        max_cost_usd=20.0,
        max_runtime_minutes=120,
    )
    assert req.max_cost_usd == 20.0
    assert req.max_runtime_minutes == 120


def test_create_task_request_budget_limits():
    with pytest.raises(ValidationError):
        CreateTaskRequest(
            goal="test",
            repo_url="https://github.com/org/repo",
            max_cost_usd=0.1,  # below minimum
        )
    with pytest.raises(ValidationError):
        CreateTaskRequest(
            goal="test",
            repo_url="https://github.com/org/repo",
            max_cost_usd=100.0,  # above maximum
        )


def test_approval_decision_valid():
    d = ApprovalDecision(decision="approved", reason="Low risk dependency")
    assert d.decision == "approved"


def test_approval_decision_invalid():
    with pytest.raises(ValidationError):
        ApprovalDecision(decision="maybe")  # only approved|rejected allowed


def test_backtrack_request_optional_reason():
    r = BacktrackRequest()
    assert r.reason is None

    r2 = BacktrackRequest(reason="Tests failed after step 2")
    assert r2.reason == "Tests failed after step 2"
