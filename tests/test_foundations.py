"""Store (audit chain, versioning), policy rules and toolbox confinement."""
import sqlite3

import pytest

from orchestrator.models import AutonomyProfile, PolicyViolation
from orchestrator.policy import PolicyEngine
from orchestrator.store import Store
from orchestrator.toolbox import Toolbox, Workspace

POLICY = PolicyEngine()


# ---- store -------------------------------------------------------------------------------------
def test_artifacts_are_versioned_only_when_content_changes():
    s = Store()
    assert s.put_artifact("r", "spec", {"a": 1}, "t1")[:2] == (1, True)
    assert s.put_artifact("r", "spec", {"a": 1}, "t2")[:2] == (1, False)     # identical content: same version
    assert s.put_artifact("r", "spec", {"a": 2}, "t2")[:2] == (2, True)
    assert s.get_artifact("r", "spec", 1)[0] == {"a": 1} and s.latest_version("r", "spec") == 2


def test_audit_chain_detects_tampering_edits_and_deletions(tmp_path):
    path = str(tmp_path / "audit.db")
    s = Store(path)
    for i in range(5):
        s.log("r", "EVENT", f"t{i}", n=i)
    assert s.verify_chain("r") == (True, None)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE events SET payload='{\"n\":99}' WHERE seq=3")
    conn.commit()
    assert Store(path).verify_chain("r") == (False, 3)
    conn.execute("DELETE FROM events WHERE seq=2")
    conn.commit()
    assert Store(path).verify_chain("r")[0] is False


def test_chains_are_per_run():
    s = Store()
    s.log("a", "X")
    s.log("b", "X")
    s.log("a", "Y")
    assert s.verify_chain("a")[0] and s.verify_chain("b")[0]


# ---- policy ------------------------------------------------------------------------------------
@pytest.mark.parametrize("code,rule", [
    ('API_KEY = "sk-live-9f8e7d6c5b4a39281716"', "SEC-001"),
    ('password = "hunter2hunter2"', "SEC-001"),
    ("x = 'AKIAABCDEFGHIJKLMNOP'", "SEC-001"),
    ("eval(user_input)", "SEC-002"),
    ("import os\nos.system('ls')", "SEC-002"),
    ("import subprocess\nsubprocess.run('ls', shell=True)", "SEC-002"),
    ("def f(c, n):\n    c.execute(f\"SELECT * FROM t WHERE id={n}\")", "SEC-003"),
    ("def f(c, n):\n    c.execute('SELECT * FROM t WHERE id=' + n)", "SEC-003"),
    ('SQL = "CREATE TABLE visitors (ip TEXT, code TEXT);"', "CMP-001"),
    ('SQL = "ALTER TABLE links ADD COLUMN user_agent TEXT"', "CMP-001"),
    ("def broken(:\n", "CHG-006"),
])
def test_policy_detects_violations(code, rule):
    assert rule in {v.rule for v in POLICY.scan_content("shortener/x.py", code)}


def test_policy_allows_safe_code():
    ok = 'def f(c, n):\n    c.execute("SELECT * FROM t WHERE id=?", (n,))\nSQL = "CREATE TABLE v (visitor_hash TEXT);"\n'
    assert POLICY.scan_content("shortener/x.py", ok) == []


def test_required_controls_and_changeset_rules():
    assert {v.rule for v in POLICY.scan_workspace({"shortener/validation.py": "x = 1\n"})} == {"SEC-004"}
    assert [v.rule for v in POLICY.check_changeset(["shortener/service.py"])] == ["CHG-002"]
    assert POLICY.check_changeset(["shortener/service.py", "tests/test_x.py"]) == []
    assert POLICY.requires_human("release", False) and POLICY.requires_human("design", True) and not POLICY.requires_human("design", False)


# ---- toolbox -----------------------------------------------------------------------------------
def tb(tmp_path, task="t", profile=None, ws=None, finished=lambda _t: True):
    ws = ws or Workspace(tmp_path / "ws")
    prof = profile or AutonomyProfile("p", write_globs=("shortener/*.py", "tests/*.py"), max_files=2, max_changed_lines=20)
    return Toolbox(ws, task, prof, POLICY, finished), ws


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "shortener/../../x.py", ".env", ".git/config", "README.md", "other/x.py"])
def test_toolbox_refuses_paths_outside_autonomy_boundary(tmp_path, path):
    box, ws = tb(tmp_path)
    with pytest.raises(PolicyViolation):
        box.write(path, "x = 1\n")
    assert not any(ws.root.rglob("*.py"))


def test_toolbox_scans_before_writing_and_rolls_back_exactly(tmp_path):
    box, ws = tb(tmp_path)
    (ws.root / "shortener").mkdir()
    (ws.root / "shortener/a.py").write_text("orig = 1\n")
    box.write("shortener/a.py", "orig = 2\n")
    box.write("shortener/b.py", "new = 1\n")
    with pytest.raises(PolicyViolation):
        box.write("shortener/c.py", "eval('1')\n")
    assert not (ws.root / "shortener/c.py").exists()        # blocked content never touched the disk
    assert box.rollback() == 2
    assert (ws.root / "shortener/a.py").read_text() == "orig = 1\n" and not (ws.root / "shortener/b.py").exists()


def test_toolbox_enforces_budgets_and_single_writer(tmp_path):
    box, ws = tb(tmp_path)
    box.write("shortener/a.py", "a = 1\n")
    box.write("shortener/b.py", "b = 1\n")
    with pytest.raises(PolicyViolation, match="at most 2 files"):
        box.write("shortener/c.py", "c = 1\n")
    with pytest.raises(PolicyViolation, match="changed lines"):
        tb(tmp_path / "x")[0].write("shortener/big.py", "x = 1\n" * 50)
    other, _ = tb(tmp_path, task="other", ws=ws, finished=lambda _t: False)
    with pytest.raises(PolicyViolation, match="concurrently"):
        other.write("shortener/a.py", "a = 2\n")
    done, _ = tb(tmp_path, task="later", ws=ws, finished=lambda _t: True)
    done.write("shortener/a.py", "a = 3\n")                    # previous owner finished: allowed


def test_agents_without_the_capability_cannot_run_tests_or_export(tmp_path):
    box, _ = tb(tmp_path)
    with pytest.raises(PolicyViolation):
        box.run_tests()
    with pytest.raises(PolicyViolation):
        box.export_openapi()
