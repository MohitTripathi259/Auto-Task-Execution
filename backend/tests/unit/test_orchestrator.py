"""
Unit tests for orchestrator components.
Tests parsing/fallback logic without hitting Claude API or AWS.
"""

import json
import pytest


# ── Planner tests ─────────────────────────────────────────────────────────────

class TestPlannerParsing:
    def test_normalise_step_ids(self):
        from src.agents.orchestrator.planner import _normalise_step_ids
        plan = {
            "goal": "test",
            "risk_level": "low",
            "estimated_cost_tier": "minimal",
            "steps": [
                {"step_id": "whatever", "index": 99, "action": "do thing",
                 "expected_output": "done", "tools_likely_needed": [], "risk": "low"},
                {"step_id": "other", "index": 0, "action": "do other",
                 "expected_output": "done2", "tools_likely_needed": [], "risk": "low"},
            ],
            "total_steps": 2,
        }
        result = _normalise_step_ids(plan)
        assert result["steps"][0]["step_id"] == "s001"
        assert result["steps"][1]["step_id"] == "s002"
        assert result["steps"][0]["index"] == 0
        assert result["steps"][1]["index"] == 1
        assert result["total_steps"] == 2

    def test_fallback_plan_structure(self):
        from src.agents.orchestrator.planner import _fallback_plan
        plan = _fallback_plan("Add login validation")
        assert plan["goal"] == "Add login validation"
        assert plan["risk_level"] in ("low", "medium", "high")
        assert len(plan["steps"]) >= 1
        assert plan["steps"][0]["step_id"] == "s001"

    def test_parse_plan_strips_markdown_fences(self):
        from src.agents.orchestrator.planner import _parse_plan
        raw = """```json
{
  "goal": "test goal",
  "risk_level": "low",
  "estimated_cost_tier": "minimal",
  "steps": [
    {"step_id": "s001", "index": 0, "action": "read files",
     "expected_output": "files read", "tools_likely_needed": [], "risk": "low"}
  ],
  "total_steps": 1
}
```"""
        plan = _parse_plan(raw, "test goal")
        assert plan["goal"] == "test goal"
        assert len(plan["steps"]) == 1

    def test_parse_plan_falls_back_on_invalid_json(self):
        from src.agents.orchestrator.planner import _parse_plan
        plan = _parse_plan("this is not json at all", "my goal")
        assert plan["goal"] == "my goal"
        assert len(plan["steps"]) >= 1

    def test_validate_plan_schema_raises_on_missing_fields(self):
        from src.agents.orchestrator.planner import _validate_plan_schema
        with pytest.raises(ValueError, match="missing required fields"):
            _validate_plan_schema({"goal": "test"})  # missing steps, total_steps

    def test_validate_plan_schema_raises_on_empty_steps(self):
        from src.agents.orchestrator.planner import _validate_plan_schema
        with pytest.raises(ValueError, match="at least one step"):
            _validate_plan_schema({
                "goal": "test", "risk_level": "low", "steps": [], "total_steps": 0
            })


# ── Policy tests ──────────────────────────────────────────────────────────────

class TestPolicyDefaults:
    def test_default_policy_low_risk(self):
        from src.agents.orchestrator.policy import _default_policy
        p = _default_policy("low", 5.0, 30)
        assert "repo_read_file" in p["allowed_tools"]
        assert "dependency_add" in p["permission_required_for"]
        assert p["max_cost_usd"] == 5.0
        assert p["max_runtime_minutes"] == 30

    def test_default_policy_high_risk_tighter(self):
        from src.agents.orchestrator.policy import _default_policy
        low = _default_policy("low", 8.0, 60)
        high = _default_policy("high", 8.0, 60)
        assert high["max_file_writes"] < low["max_file_writes"]
        assert len(high["permission_required_for"]) > len(low["permission_required_for"])
        assert len(high["auto_approved"]) <= len(low["auto_approved"])

    def test_policy_parse_strips_markdown_fences(self):
        from src.agents.orchestrator.policy import _parse_policy
        raw = '```json\n{"allowed_tools": ["repo_read_file"], "risk_level": "low"}\n```'
        plan = {"risk_level": "low", "steps": []}
        result = _parse_policy(raw, plan, 8.0, 60)
        assert result["allowed_tools"] == ["repo_read_file"]


# ── Permission tests ──────────────────────────────────────────────────────────

class TestPermissionEvaluation:
    def _make_policy(self, auto_approved=None, forbidden_actions=None, permission_required_for=None):
        return {
            "auto_approved": auto_approved or ["repo_read_file", "repo_diff"],
            "forbidden_actions": forbidden_actions or ["push to main directly", "delete files"],
            "permission_required_for": permission_required_for or ["dependency_add"],
            "allowed_paths": ["/workspace/src"],
            "forbidden_paths": ["/workspace/.env"],
            "risk_level": "medium",
        }

    def test_auto_approved_skips_llm(self, monkeypatch):
        from src.agents.orchestrator import permission
        # Patch dynamo to avoid AWS calls
        monkeypatch.setattr("src.agents.orchestrator.permission.dynamo.put_event", lambda *a, **kw: None)

        p = self._make_policy(auto_approved=["repo_read_file"])
        result = permission.evaluate(
            run_id="run_test",
            step_id="s001",
            request_type="repo_read_file",
            description="Read a file",
            risk="low",
            policy=p,
        )
        assert result["decision"] == "approved"
        assert "auto-approved" in result["reason"].lower()

    def test_forbidden_action_fast_reject(self, monkeypatch):
        from src.agents.orchestrator import permission
        monkeypatch.setattr("src.agents.orchestrator.permission.dynamo.put_event", lambda *a, **kw: None)

        p = self._make_policy(forbidden_actions=["push to main directly"])
        result = permission.evaluate(
            run_id="run_test",
            step_id="s001",
            request_type="git_push",
            description="push to main directly without PR",
            risk="high",
            policy=p,
        )
        assert result["decision"] == "rejected"


# ── Reporter tests ────────────────────────────────────────────────────────────

class TestReporterFallback:
    def test_fallback_report_all_completed(self):
        from src.agents.orchestrator.reporter import _fallback_report
        steps = [
            {"step_id": "s001", "action": "do thing", "status": "completed", "error": None},
            {"step_id": "s002", "action": "do other", "status": "completed", "error": None},
        ]
        report = _fallback_report("My goal", steps, [])
        assert report["status"] == "completed"
        assert report["steps_completed"] == 2
        assert report["steps_failed"] == 0

    def test_fallback_report_partial_failure(self):
        from src.agents.orchestrator.reporter import _fallback_report
        steps = [
            {"step_id": "s001", "action": "ok", "status": "completed", "error": None},
            {"step_id": "s002", "action": "bad", "status": "failed", "error": "oops"},
        ]
        report = _fallback_report("My goal", steps, [])
        assert report["status"] == "partial"
        assert report["steps_completed"] == 1
        assert report["steps_failed"] == 1
