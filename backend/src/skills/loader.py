"""
Skill loader — reads skill.md from a skill folder and returns it as a system prompt.

Usage:
    from src.skills.loader import load_skill, build_system_prompt

    system = load_skill("planner")
    system = build_system_prompt("executor", task_skill_content="# My domain rules\n...")
"""

from functools import lru_cache
from pathlib import Path

_SKILLS_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_skill(skill_name: str) -> str:
    """Read and cache skill.md for the given skill name."""
    skill_path = _SKILLS_DIR / skill_name / "skill.md"
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_name} (looked at {skill_path})")
    return skill_path.read_text(encoding="utf-8")


def build_system_prompt(skill_name: str, task_skill_content: str | None = None) -> str:
    """
    Combine the static skill.md with an optional task-specific skill uploaded by the user.

    The task skill is appended as a clearly separated section so Claude treats it
    as domain-specific overrides/additions to the base skill spec.
    """
    base = load_skill(skill_name)
    if not task_skill_content or not task_skill_content.strip():
        return base

    return f"""{base}

---

## Task-Specific Instructions (uploaded by user)

The following instructions were provided for this specific task.
They take precedence over any conflicting general guidance above.

{task_skill_content.strip()}
"""
