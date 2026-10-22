"""The only door through which agents touch the workspace.

Enforces autonomy boundaries (path globs, file/line budgets, single-writer ownership), runs policy
scans *before* bytes are written, and keeps an undo journal so any task can be rolled back exactly.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .models import AutonomyProfile, PolicyViolation, TransientError, Violation
from .policy import PolicyEngine

_OPENAPI_SNIPPET = (
    "import json; from shortener.api import create_app; "
    "print(json.dumps(create_app(':memory:').openapi(), sort_keys=True, indent=2))"
)


class Workspace:
    """Shared by all toolboxes of a run: serialises writes and tracks which task owns which file."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.owners: Dict[str, str] = {}

    def snapshot(self) -> Dict[str, str]:
        files = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts and p.suffix in {".py", ".md", ".txt", ".json", ".ini", ".toml"}:
                files[p.relative_to(self.root).as_posix()] = p.read_text()
        return files


class Toolbox:
    def __init__(self, ws: Workspace, task_id: str, profile: AutonomyProfile, policy: PolicyEngine,
                 task_finished: Callable[[str], bool] = lambda _t: True):
        self.ws, self.task_id, self.profile, self.policy = ws, task_id, profile, policy
        self._task_finished = task_finished
        self._journal: List[Tuple[str, Optional[str]]] = []     # (path, previous content or None)
        self.added: Dict[str, int] = {}
        self.removed: Dict[str, int] = {}

    # ---- reading -----------------------------------------------------------------------------
    def exists(self, path: str) -> bool:
        return (self.ws.root / path).is_file()

    def read(self, path: str) -> str:
        return (self.ws.root / path).read_text()

    def list_files(self, suffix: str = ".py") -> List[str]:
        return [p for p in self.ws.snapshot() if p.endswith(suffix)]

    def snapshot(self) -> Dict[str, str]:
        return self.ws.snapshot()

    # ---- writing -----------------------------------------------------------------------------
    def write(self, path: str, content: str) -> None:
        violations = self.policy.check_path(path, self.profile.write_globs)
        if violations:
            raise PolicyViolation(violations)
        with self.ws.lock:
            owner = self.ws.owners.get(path)
            if owner not in (None, self.task_id) and not self._task_finished(owner):
                raise PolicyViolation([Violation("CHG-004", "change-control", "high",
                                                 f"file is being modified concurrently by task {owner}", path)])
            if path not in {p for p, _ in self._journal} and len(self.added) >= self.profile.max_files:
                raise PolicyViolation([Violation("CHG-003", "change-control", "medium",
                                                 f"autonomy budget: at most {self.profile.max_files} files per task", path)])
            violations = self.policy.scan_content(path, content)
            if violations:
                raise PolicyViolation(violations)
            target = self.ws.root / path
            previous = target.read_text() if target.is_file() else None
            diff = list(difflib.unified_diff((previous or "").splitlines(), content.splitlines(), lineterm="", n=0))
            plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            if sum(self.added.values()) + sum(self.removed.values()) + plus + minus > self.profile.max_changed_lines:
                raise PolicyViolation([Violation("CHG-003", "change-control", "medium",
                                                 f"autonomy budget: more than {self.profile.max_changed_lines} changed lines", path)])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            self._journal.append((path, previous))
            self.ws.owners[path] = self.task_id
            self.added[path] = self.added.get(path, 0) + plus
            self.removed[path] = self.removed.get(path, 0) + minus

    @property
    def changed_paths(self) -> List[str]:
        return sorted(set(p for p, _ in self._journal))

    def rollback(self) -> int:
        """Undo every write of this task (newest first). Returns number of files restored."""
        with self.ws.lock:
            n = 0
            for path, previous in reversed(self._journal):
                target = self.ws.root / path
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_text(previous)
                n += 1
            for path in {p for p, _ in self._journal}:
                if self.ws.owners.get(path) == self.task_id:
                    del self.ws.owners[path]
            self._journal.clear()
            self.added.clear()
            self.removed.clear()
            return n

    # ---- constrained execution -----------------------------------------------------------------
    def _run(self, argv: List[str], timeout: float) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}
        try:
            return subprocess.run(argv, cwd=self.ws.root, capture_output=True, text=True, timeout=timeout, env=env, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"timeout after {timeout}s running {argv[2:4]}") from exc

    def run_tests(self, timeout: float = 120.0) -> Dict[str, object]:
        if not self.profile.can_run_tests:
            raise PolicyViolation([Violation("CHG-005", "change-control", "high", "agent may not execute tests")])
        proc = self._run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore"], timeout)
        out = proc.stdout + proc.stderr
        count = lambda word: int(m.group(1)) if (m := re.search(rf"(\d+) {word}", out)) else 0  # noqa: E731
        return {"returncode": proc.returncode, "passed": count("passed"), "failed": count("failed"),
                "errors": count("error"), "tail": "\n".join(out.strip().splitlines()[-12:])}

    def export_openapi(self, timeout: float = 60.0) -> dict:
        if not self.profile.can_export_schema:
            raise PolicyViolation([Violation("CHG-005", "change-control", "high", "agent may not export schemas")])
        proc = self._run([sys.executable, "-W", "ignore", "-c", _OPENAPI_SNIPPET], timeout)
        if proc.returncode != 0:
            raise TransientError("openapi export failed: " + proc.stderr.strip()[-300:])
        return json.loads(proc.stdout)
