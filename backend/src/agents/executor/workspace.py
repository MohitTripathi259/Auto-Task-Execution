"""
Git workspace lifecycle manager.

Creates an isolated working directory per task, optionally clones the repo,
and exposes basic git operations used by executor tools.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.common.logging import get_logger

logger = get_logger(__name__)


class Workspace:
    """Manages an isolated git workspace for a single task execution."""

    def __init__(self, task_id: str, repo_url: str | None = None, base_branch: str = "main"):
        self.task_id = task_id
        self.repo_url = repo_url
        self.base_branch = base_branch
        self._root: Path | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def setup(self) -> Path:
        """Create workspace directory, optionally clone repo."""
        self._root = Path(tempfile.mkdtemp(prefix=f"agent_{self.task_id}_"))
        logger.info("workspace.created", task_id=self.task_id, path=str(self._root))

        if self.repo_url and self.repo_url.startswith(("http://", "https://", "git@")):
            self._clone()
        else:
            # Demo mode: init an empty git repo so all git ops work
            self._init_empty_repo()

        return self._root

    def teardown(self) -> None:
        """Remove workspace directory."""
        if self._root and self._root.exists():
            shutil.rmtree(self._root, ignore_errors=True)
            logger.info("workspace.removed", task_id=self.task_id)
        self._root = None

    def __enter__(self) -> "Workspace":
        self.setup()
        return self

    def __exit__(self, *_) -> None:
        self.teardown()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        if not self._root:
            raise RuntimeError("Workspace not set up. Call setup() first.")
        return self._root

    # ── Git helpers ───────────────────────────────────────────────────────────

    def current_sha(self) -> str:
        result = self._git("rev-parse", "HEAD")
        return result.strip() if result else "0000000"

    def current_branch(self) -> str:
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return result.strip() if result else self.base_branch

    def create_branch(self, name: str) -> str:
        self._git("checkout", "-b", name)
        logger.info("workspace.branch_created", branch=name)
        return name

    def commit(self, message: str, files: list[str] | None = None) -> str:
        if files:
            self._git("add", "--", *files)
        else:
            self._git("add", "-A")
        result = self._git("commit", "-m", message, "--allow-empty")
        sha = self.current_sha()
        logger.info("workspace.committed", sha=sha[:8], message=message[:60])
        return sha

    def get_diff(self) -> str:
        return self._git("diff", "HEAD") or ""

    def get_status(self) -> str:
        return self._git("status", "--short") or ""

    def list_files(self, directory: str = ".", pattern: str = "*") -> list[str]:
        """Return paths of files matching pattern relative to workspace root."""
        base = self.root / directory
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.root))
            for p in base.rglob(pattern)
            if p.is_file()
        ]

    def read_file(self, path: str) -> str:
        full = self.root / path
        if not full.exists():
            raise FileNotFoundError(f"File not found in workspace: {path}")
        return full.read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> None:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        logger.debug("workspace.file_written", path=path)

    def apply_patch(self, patch: str) -> bool:
        """Apply a unified diff patch. Returns True on success."""
        patch_file = self.root / ".agent_patch.diff"
        patch_file.write_text(patch, encoding="utf-8")
        try:
            self._git("apply", "--whitespace=fix", str(patch_file))
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning("workspace.patch_failed", stderr=exc.stderr or "")
            return False
        finally:
            patch_file.unlink(missing_ok=True)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, ["git", *args],
                output=result.stdout, stderr=result.stderr
            )
        return result.stdout

    def _clone(self) -> None:
        logger.info("workspace.cloning", repo=self.repo_url)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", self.base_branch,
             self.repo_url, str(self._root)],
            check=True, capture_output=True, text=True,
        )
        logger.info("workspace.cloned")

    def _init_empty_repo(self) -> None:
        """Bootstrap a minimal git repo for demo/test mode."""
        self._git("init")
        self._git("config", "user.email", "agent@autonomous.local")
        self._git("config", "user.name", "Autonomous Agent")
        # Create a minimal README so HEAD exists
        (self._root / "README.md").write_text("# Agent workspace\n")
        self._git("add", ".")
        self._git("commit", "-m", "chore: init workspace")
        logger.info("workspace.demo_repo_initialized")
