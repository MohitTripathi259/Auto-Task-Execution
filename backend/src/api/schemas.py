from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ── Request models ────────────────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    goal: str = Field(..., description="Plain English description of the task")
    repo_url: Optional[str] = Field(default="", description="GitHub repo URL (blank = demo workspace)")
    base_branch: str = Field(default="main", description="Branch to base work on")
    tenant_id: str = Field(default="default")
    max_cost_usd: float = Field(default=8.0, ge=0.5, le=50.0)
    max_runtime_minutes: int = Field(default=60, ge=5, le=480)
    idempotency_key: Optional[str] = None
    task_skill: Optional[str] = Field(default=None, description="Task-specific skill markdown (paste content or leave blank)")


class ApprovalDecision(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    reason: Optional[str] = None


class BacktrackRequest(BaseModel):
    reason: Optional[str] = Field(default=None, description="Why the backtrack is needed")


# ── Response models ───────────────────────────────────────────────────────────

class StepResponse(BaseModel):
    step_id: str
    index: int
    action: str
    status: str
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class TaskResponse(BaseModel):
    task_id: str
    run_id: str
    status: TaskStatus
    goal: str
    risk_level: Optional[RiskLevel] = None
    current_step_id: Optional[str] = None
    steps: list[StepResponse] = []
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    pr_url: Optional[str] = None


class TaskSubmitResponse(BaseModel):
    task_id: str
    run_id: str
    status: str
    duplicate: bool = False


class AuditEvent(BaseModel):
    event_type: str
    timestamp: str
    step_id: Optional[str] = None
    attempt_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    approval_id: Optional[str] = None
    payload: dict[str, Any]


class AuditTrailResponse(BaseModel):
    run_id: str
    events: list[AuditEvent]


class TestResult(BaseModel):
    name: str
    status: str   # pass | fail | error
    duration_ms: Optional[int] = None
    error: Optional[str] = None


class TestRunResponse(BaseModel):
    test_run_id: str
    step_id: str
    suite: str
    status: str
    passed: int
    failed: int
    total: int
    duration_ms: Optional[int] = None
    results: list[TestResult] = []
    artifact_url: Optional[str] = None
    created_at: datetime


class ApprovalResponse(BaseModel):
    approval_id: str
    task_id: str
    step_id: str
    request_type: str
    description: str
    risk: str
    decision: str
    created_at: datetime


class HealthResponse(BaseModel):
    status: str = "ok"
    env: str
    version: str = "0.1.0"
