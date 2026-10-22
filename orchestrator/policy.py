"""Policy guardrails: security, compliance and change-control rules evaluated by the platform.

Agents cannot opt out: the Toolbox calls `check_write` before any byte reaches the workspace, and
the reviewer/release gates call `check_changeset` / `scan_workspace` on the final state.
"""
from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, Iterable, List

from .models import Violation

_SECRET_PATTERNS = [
    re.compile(r"""(?i)\b(api[_-]?key|secret|token|passwd|password|private[_-]?key)\b\s*[:=]\s*["'][^"'\s]{8,}["']"""),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
# PII column / field names that must never be persisted in raw form (see D-UV-1)
_PII_SQL = re.compile(
    r"(CREATE\s+TABLE|ALTER\s+TABLE|INSERT[^;'\"]*?INTO)[^;'\"]*?\b(ip|ip_address|client_ip|remote_addr|user_agent|email|phone)\b",
    re.I | re.S,
)
_DANGEROUS_CALLS = {"eval", "exec", "os.system", "pickle.loads", "pickle.load", "marshal.loads"}
_REQUIRED_CONTROLS = {
    # path -> (substring, rule message)
    "shortener/validation.py": [("ALLOWED_SCHEMES", "scheme allow-list control missing"),
                                ("_is_private_host", "SSRF private-host guard missing")],
}


@dataclass(frozen=True)
class PolicyConfig:
    protected_globs: tuple = (".git/*", ".env*", "*.pem", "*.key")
    max_files_per_change: int = 12


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


class PolicyEngine:
    def __init__(self, config: PolicyConfig = PolicyConfig()):
        self.config = config

    # ---- path confinement -----------------------------------------------------------------------
    def check_path(self, path: str, allowed_globs: Iterable[str]) -> List[Violation]:
        p = PurePosixPath(path)
        if p.is_absolute() or ".." in p.parts or not path:
            return [Violation("SEC-005", "security", "high", "path escapes the workspace", path)]
        if any(fnmatch.fnmatch(path, g) for g in self.config.protected_globs):
            return [Violation("CHG-001", "change-control", "high", "protected path may not be modified by agents", path)]
        if not any(fnmatch.fnmatch(path, g) for g in allowed_globs):
            return [Violation("CHG-005", "change-control", "high", "path is outside this agent's autonomy boundary", path)]
        return []

    # ---- content scans ---------------------------------------------------------------------------
    def scan_content(self, path: str, content: str) -> List[Violation]:
        out: List[Violation] = []
        for pat in _SECRET_PATTERNS:
            if pat.search(content):
                out.append(Violation("SEC-001", "security", "high", "hard-coded secret or credential detected", path))
                break
        if path.endswith(".py"):
            out += self._scan_python(path, content)
        return out

    def _scan_python(self, path: str, content: str) -> List[Violation]:
        out: List[Violation] = []
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            return [Violation("CHG-006", "change-control", "high", f"generated code does not parse: {exc.msg}", path)]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name in _DANGEROUS_CALLS:
                    out.append(Violation("SEC-002", "security", "high", f"dangerous call `{name}`", path))
                if name.startswith("subprocess.") and any(
                    kw.arg == "shell" and getattr(kw.value, "value", False) is True for kw in node.keywords
                ):
                    out.append(Violation("SEC-002", "security", "high", "subprocess with shell=True", path))
                if name.endswith((".execute", ".executemany")) and node.args and self._is_dynamic_sql(node.args[0]):
                    out.append(Violation("SEC-003", "security", "high", "SQL built by string interpolation (injection risk)", path))
        if _PII_SQL.search(content):
            out.append(Violation("CMP-001", "compliance", "high",
                                 "raw personal data (IP / user-agent / e-mail) persisted; store only salted hashes", path))
        return out

    @staticmethod
    def _is_dynamic_sql(arg: ast.AST) -> bool:
        if isinstance(arg, ast.JoinedStr):
            return True
        if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Add, ast.Mod)):
            return True
        return isinstance(arg, ast.Call) and _dotted(arg.func).endswith(".format")

    # ---- workspace / change-set level ---------------------------------------------------------
    def scan_workspace(self, files: Dict[str, str]) -> List[Violation]:
        out: List[Violation] = []
        for path, content in sorted(files.items()):
            out += self.scan_content(path, content)
        for path, controls in _REQUIRED_CONTROLS.items():
            if path in files:
                for needle, msg in controls:
                    if needle not in files[path]:
                        out.append(Violation("SEC-004", "security", "high", f"required control: {msg}", path))
        return out

    def check_changeset(self, changed_paths: List[str]) -> List[Violation]:
        out: List[Violation] = []
        src = [p for p in changed_paths if p.startswith("shortener/")]
        tests = [p for p in changed_paths if p.startswith("tests/")]
        if src and not tests:
            out.append(Violation("CHG-002", "change-control", "high", "source changed without any test change"))
        if len(changed_paths) > self.config.max_files_per_change:
            out.append(Violation("CHG-003", "change-control", "medium",
                                 f"change touches {len(changed_paths)} files (budget {self.config.max_files_per_change})"))
        return out

    # ---- approvals ------------------------------------------------------------------------------
    @staticmethod
    def requires_human(kind: str, high_impact: bool) -> bool:
        """Change control: releases always need a person; designs only when impact is high."""
        return kind == "release" or high_impact
