"""
PostgreSQL via SQLAlchemy — canonical control-plane state.
Owns: tasks, steps, approvals, test_runs, policies.
"""

import enum
import uuid
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    JSON,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.common.config import settings
from src.common.logging import get_logger

logger = get_logger(__name__)

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ── Enums ─────────────────────────────────────────────────────────────────────

class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ── Models ────────────────────────────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=lambda: f"task_{uuid.uuid4().hex[:12]}")
    run_id = Column(String, nullable=False, index=True)
    tenant_id = Column(String, nullable=False, default="default")
    goal = Column(Text, nullable=False)
    repo_url = Column(String)
    base_branch = Column(String, default="main")
    status = Column(String, default=TaskStatus.PENDING)
    risk_level = Column(String)
    plan = Column(JSON)           # structured execution plan from planner
    policy = Column(JSON)         # boundary + budget from policy generator
    current_step_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime)
    error = Column(Text)
    report_s3_key = Column(String)
    idempotency_key = Column(String, unique=True, index=True)


class Step(Base):
    __tablename__ = "steps"

    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=False, index=True)
    attempt_id = Column(String, nullable=False)
    index = Column(Integer, nullable=False)
    action = Column(Text, nullable=False)
    expected_output = Column(Text)
    status = Column(String, default="pending")  # pending|running|completed|failed
    base_sha = Column(String)      # git SHA before this step
    head_sha = Column(String)      # git SHA after this step
    patch_s3_key = Column(String)  # S3 key of the patch file
    artifact_s3_key = Column(String)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    error = Column(Text)


class Approval(Base):
    __tablename__ = "approvals"

    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=False, index=True)
    step_id = Column(String, nullable=False)
    request_type = Column(String)          # e.g. new_dependency, schema_change
    description = Column(Text)
    risk = Column(String)
    decision = Column(String)              # approved | rejected | pending
    reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime)


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(String, primary_key=True, default=lambda: f"tr_{uuid.uuid4().hex[:10]}")
    task_id = Column(String, nullable=False, index=True)
    step_id = Column(String, nullable=False)
    attempt_id = Column(String, nullable=False)
    suite = Column(String)          # unit | component | visual | smoke
    status = Column(String)         # pass | fail | error
    passed = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    total = Column(Integer, default=0)
    duration_ms = Column(Integer)
    results = Column(JSON)          # list of per-test {name, status, error, duration_ms}
    artifact_s3_key = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── DB lifecycle ──────────────────────────────────────────────────────────────

def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    logger.info("db.initialized")


@contextmanager
def get_db():
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Task CRUD ─────────────────────────────────────────────────────────────────

def create_task(db: Session, **kwargs) -> Task:
    task = Task(**kwargs)
    db.add(task)
    db.flush()
    return task


def get_task(db: Session, task_id: str) -> Task | None:
    return db.query(Task).filter(Task.id == task_id).first()


def get_task_by_idempotency_key(db: Session, key: str) -> Task | None:
    return db.query(Task).filter(Task.idempotency_key == key).first()


def update_task(db: Session, task_id: str, **kwargs) -> Task | None:
    task = get_task(db, task_id)
    if task:
        for k, v in kwargs.items():
            setattr(task, k, v)
        db.flush()
    return task


def list_tasks(db: Session, tenant_id: str = "default", limit: int = 50) -> list[Task]:
    return (
        db.query(Task)
        .filter(Task.tenant_id == tenant_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
        .all()
    )


# ── Step CRUD ─────────────────────────────────────────────────────────────────

def create_step(db: Session, **kwargs) -> Step:
    step = Step(**kwargs)
    db.add(step)
    db.flush()
    return step


def get_steps(db: Session, task_id: str) -> list[Step]:
    return db.query(Step).filter(Step.task_id == task_id).order_by(Step.index).all()


def get_step(db: Session, step_id: str) -> Step | None:
    return db.query(Step).filter(Step.id == step_id).first()


def update_step(db: Session, step_id: str, **kwargs) -> Step | None:
    step = get_step(db, step_id)
    if step:
        for k, v in kwargs.items():
            setattr(step, k, v)
        db.flush()
    return step


# ── Approval CRUD ─────────────────────────────────────────────────────────────

def create_approval(db: Session, **kwargs) -> Approval:
    approval = Approval(**kwargs)
    db.add(approval)
    db.flush()
    return approval


def get_approval(db: Session, approval_id: str) -> Approval | None:
    return db.query(Approval).filter(Approval.id == approval_id).first()


def update_approval_decision(
    db: Session, approval_id: str, decision: str, reason: str
) -> Approval | None:
    approval = get_approval(db, approval_id)
    if approval:
        approval.decision = decision
        approval.reason = reason
        approval.decided_at = datetime.utcnow()
        db.flush()
    return approval


# ── TestRun CRUD ──────────────────────────────────────────────────────────────

def create_test_run(db: Session, **kwargs) -> TestRun:
    tr = TestRun(**kwargs)
    db.add(tr)
    db.flush()
    return tr


def get_test_runs(db: Session, task_id: str) -> list[TestRun]:
    return (
        db.query(TestRun)
        .filter(TestRun.task_id == task_id)
        .order_by(TestRun.created_at)
        .all()
    )
