import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any


# ── ID generators ─────────────────────────────────────────────────────────────

def generate_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def generate_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def generate_attempt_id() -> str:
    return f"att_{uuid.uuid4().hex[:12]}"


def generate_step_id(index: int) -> str:
    return f"s{index:03d}"


def generate_tool_call_id() -> str:
    return f"tc_{uuid.uuid4().hex[:8]}"


def generate_approval_id() -> str:
    return f"apr_{uuid.uuid4().hex[:10]}"


# ── Hashing ───────────────────────────────────────────────────────────────────

def sha256_hex(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode()
    return hashlib.sha256(content).hexdigest()


def idempotency_key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return sha256_hex(raw)[:32]


# ── Time ──────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


# ── S3 key builder ────────────────────────────────────────────────────────────

def s3_artifact_key(tenant_id: str, run_id: str, filename: str) -> str:
    now = utc_now()
    return f"{tenant_id}/{now.year}/{now.month:02d}/{now.day:02d}/{run_id}/{filename}"
