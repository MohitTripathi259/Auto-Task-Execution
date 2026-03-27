"""Unit tests for common utilities — no AWS or DB required."""

from src.common.utils import (
    generate_approval_id,
    generate_attempt_id,
    generate_run_id,
    generate_step_id,
    generate_task_id,
    idempotency_key,
    s3_artifact_key,
    sha256_hex,
    utc_now,
    utc_now_iso,
)


def test_run_id_format():
    rid = generate_run_id()
    assert rid.startswith("run_")
    assert len(rid) == 16   # "run_" + 12 hex chars


def test_task_id_format():
    tid = generate_task_id()
    assert tid.startswith("task_")


def test_attempt_id_format():
    aid = generate_attempt_id()
    assert aid.startswith("att_")


def test_step_id_format():
    assert generate_step_id(0) == "s000"
    assert generate_step_id(3) == "s003"
    assert generate_step_id(12) == "s012"


def test_approval_id_format():
    aid = generate_approval_id()
    assert aid.startswith("apr_")


def test_ids_are_unique():
    ids = {generate_run_id() for _ in range(100)}
    assert len(ids) == 100


def test_sha256_hex_string():
    h = sha256_hex("hello")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_sha256_hex_bytes():
    h = sha256_hex(b"hello")
    assert len(h) == 64


def test_sha256_hex_deterministic():
    assert sha256_hex("same") == sha256_hex("same")
    assert sha256_hex("a") != sha256_hex("b")


def test_idempotency_key_deterministic():
    k1 = idempotency_key("task1", "run1", "goal")
    k2 = idempotency_key("task1", "run1", "goal")
    assert k1 == k2
    assert len(k1) == 32


def test_idempotency_key_different_inputs():
    k1 = idempotency_key("a", "b")
    k2 = idempotency_key("a", "c")
    assert k1 != k2


def test_utc_now_is_aware():
    from datetime import timezone
    now = utc_now()
    assert now.tzinfo is not None
    assert now.tzinfo == timezone.utc


def test_utc_now_iso_format():
    iso = utc_now_iso()
    assert "T" in iso
    assert "+" in iso or "Z" in iso or iso.endswith("+00:00")


def test_s3_artifact_key_structure():
    key = s3_artifact_key("tenant1", "run_abc123", "report.json")
    parts = key.split("/")
    assert parts[0] == "tenant1"
    assert parts[-1] == "report.json"
    assert "run_abc123" in key
