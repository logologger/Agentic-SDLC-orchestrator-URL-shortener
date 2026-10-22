"""Delivery-phase agents: implement, test, scan, document, review."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..catalog.features import FEATURES, EditError, apply_edits, base_files
from ..models import AgentError, AgentResult, AutonomyProfile
from ..store import digest
from .base import Agent, TaskContext

POISON_LINE = '\nAPI_KEY = "sk-live-9f8e7d6c5b4a39281716"\n'   # what a careless/compromised agent might emit (chaos demo)


class ImplementerAgent(Agent):
    """One work unit = one file. Starts from the baseline (greenfield) or the live file (brownfield) and applies
    catalog change sets. Every byte goes through the Toolbox, so policy and autonomy limits cannot be bypassed."""
    name = "implementer"
    honor_poison = True
    profile = AutonomyProfile(name="implementer", write_globs=("shortener/*.py", "tests/*.py", "conftest.py", "requirements.txt"),
                              max_files=2, max_changed_lines=700)

    def run(self, ctx: TaskContext) -> AgentResult:
        path: str = ctx.params["file"]
        design = ctx.read("design")
        if path not in design["files"]:
            raise AgentError(f"{path} is not part of the approved design")
        features = design["features"]
        content = ctx.toolbox.read(path) if ctx.toolbox.exists(path) else None
        if "core" in features and path in base_files():
            content = base_files()[path]
        for name in features:
            if name in FEATURES and path in FEATURES[name].new_files:
                content = FEATURES[name].new_files[path]
        for name in features:
            if name in FEATURES and path in FEATURES[name].edits:
                if content is None:
                    raise AgentError(f"cannot modify {path}: file does not exist")
                try:
                    content = apply_edits(path, content, FEATURES[name].edits[path])
                except EditError as exc:
                    raise AgentError(str(exc))
        if content is None:
            raise AgentError(f"no template or change set produces {path}")
        if self.honor_poison and ctx.params.get("__poison"):
            content += POISON_LINE
        ctx.toolbox.write(path, content)
        info = {"path": path, "action": design["files"][path], "added": ctx.toolbox.added.get(path, 0),
                "removed": ctx.toolbox.removed.get(path, 0), "sha256": digest(content)[:16]}
        return AgentResult({f"change:{path}": info}, notes=f"{info['action']} {path} (+{info['added']}/-{info['removed']})")


class SafeImplementer(ImplementerAgent):
    """Fallback variant: identical output, but never emits injected content (models a conservative template-only path)."""
    name = "implementer_safe"
    honor_poison = False


class TesterAgent(Agent):
    name = "tester"
    profile = AutonomyProfile(name="tester", can_run_tests=True)

    def run(self, ctx: TaskContext) -> AgentResult:
        rep = ctx.toolbox.run_tests()
        rep["test_files"] = [p for p in ctx.toolbox.list_files(".py") if p.startswith("tests/")]
        return AgentResult({"test_report": rep}, notes=f"{rep['passed']} passed, {rep['failed']} failed, {rep['errors']} errors")


class ScannerAgent(Agent):
    name = "scanner"
    profile = AutonomyProfile(name="scanner")

    def run(self, ctx: TaskContext) -> AgentResult:
        files = ctx.toolbox.snapshot()
        violations = ctx.toolbox.policy.scan_workspace(files)
        rep = {"files_scanned": len(files), "violations": [v.__dict__ for v in violations],
               "rules": ["SEC-001 secrets", "SEC-002 dangerous calls", "SEC-003 SQL injection", "SEC-004 required controls",
                         "CMP-001 raw PII at rest"]}
        return AgentResult({"scan_report": rep}, notes=f"{len(files)} files scanned, {len(violations)} violation(s)")


class DocsAgent(Agent):
    name = "docs"
    profile = AutonomyProfile(name="docs", write_globs=("README.md", "CHANGELOG.md", "docs/*"), max_files=4, max_changed_lines=900,
                              can_export_schema=True)
    BEGIN, END = "<!-- BEGIN GENERATED -->", "<!-- END GENERATED -->"

    def run(self, ctx: TaskContext) -> AgentResult:
        spec, design = ctx.read("spec"), ctx.read("design")
        openapi = ctx.toolbox.export_openapi()
        endpoints = [{"method": m.upper(), "path": p, "summary": op.get("summary", "")}
                     for p, ops in sorted(openapi["paths"].items()) for m, op in ops.items()]
        fields = {n: sorted(s.get("properties", {})) for n, s in openapi.get("components", {}).get("schemas", {}).items()
                  if n == "CreateRequest"}
        ctx.toolbox.write("docs/openapi.json", __import__("json").dumps(openapi, indent=2, sort_keys=True) + "\n")
        self._readme(ctx, spec, design, endpoints, fields)
        self._changelog(ctx, spec, design)
        self._design_doc(ctx, spec, design)
        rep = {"files": ["README.md", "CHANGELOG.md", "docs/DESIGN.md", "docs/openapi.json"], "endpoints": endpoints,
               "request_fields": fields}
        return AgentResult({"docs_report": rep}, notes=f"{len(endpoints)} endpoints documented")

    def _readme(self, ctx: TaskContext, spec, design, endpoints, fields) -> None:
        rows = "\n".join(f"| {e['method']} | `{e['path']}` | {e['summary']} |" for e in endpoints)
        feats = "\n".join(f"- **{f}**" for f in spec["features"])
        block = (f"{self.BEGIN}\n## Run\n```bash\npip install -r requirements.txt\nuvicorn --factory shortener.api:create_app\n```\n\n"
                 f"## Endpoints\n| Method | Path | Summary |\n|---|---|---|\n{rows}\n\n"
                 f"`POST /api/v1/urls` body fields: {', '.join(f'`{x}`' for x in fields.get('CreateRequest', []))}\n\n"
                 "## Configuration\n| Variable | Purpose |\n|---|---|\n| `SHORTENER_DB` | SQLite path (default `shortener.db`) |\n"
                 "| `SHORTENER_BASE_URL` | Public origin used to build `short_url` |\n"
                 + ("| `VISITOR_SALT` | Secret for visitor hashing (set from your secret store) |\n" if "unique_visitors" in
                    set(spec["features"]) | self._present(ctx) else "")
                 + f"\n## Capabilities in this build\n{feats}\n\n## Tests\n```bash\npython -m pytest\n```\n{self.END}\n")
        path = "README.md"
        if ctx.toolbox.exists(path):
            text = ctx.toolbox.read(path)
            if self.BEGIN in text and self.END in text:
                text = re.sub(re.escape(self.BEGIN) + r".*?" + re.escape(self.END) + r"\n?", lambda _m: block, text, flags=re.S)
            else:
                text = text.rstrip() + "\n\n" + block
        else:
            text = "# URL Shortener\n\n" + block
        ctx.toolbox.write(path, text)

    @staticmethod
    def _present(ctx: TaskContext) -> set:
        return {n for n, f in FEATURES.items() if ctx.toolbox.exists(f.marker_path) and f.marker in ctx.toolbox.read(f.marker_path)}

    def _changelog(self, ctx: TaskContext, spec, design) -> None:
        entry = f"## Run {ctx.run_id}\n" + "".join(f"- {FEATURES[f].summary if f in FEATURES else 'Initial URL shortener service'}\n"
                                                    for f in design["features"])
        entry += "".join(f"- API: {c}\n" for c in design["api_changes"]) + ("- Schema: additive migration\n" if design["schema_change"] else "")
        text = ctx.toolbox.read("CHANGELOG.md") if ctx.toolbox.exists("CHANGELOG.md") else "# Changelog\n"
        if f"## Run {ctx.run_id}" not in text:
            head, _, rest = text.partition("\n")
            text = f"{head}\n\n{entry}{rest}"
        ctx.toolbox.write("CHANGELOG.md", text)

    def _design_doc(self, ctx: TaskContext, spec, design) -> None:
        sec = [f"## Change {ctx.run_id}: {', '.join(design['features'])}", "", f"_Requirement:_ {spec['requirement']}", "",
               "### Decisions", ""]
        sec += [f"- **{d['id']}** {d['decision']} - {d['rationale']}" for d in design["decisions"]]
        sec += ["", "### Risks", ""] + [f"- ({r['likelihood']}/{r['impact']}) {r['risk']} -> {r['mitigation']}" for r in design["risks"]]
        text = ctx.toolbox.read("docs/DESIGN.md") if ctx.toolbox.exists("docs/DESIGN.md") else "# Design log\n"
        if f"## Change {ctx.run_id}" not in text:
            text = text.rstrip() + "\n\n" + "\n".join(sec) + "\n"
        ctx.toolbox.write("docs/DESIGN.md", text)


class ReviewerAgent(Agent):
    """Independent reviewer: aggregates evidence and applies change-control rules. Cannot write anything."""
    name = "reviewer"
    profile = AutonomyProfile(name="reviewer")

    def run(self, ctx: TaskContext) -> AgentResult:
        spec, design = ctx.read("spec"), ctx.read("design")
        tests, scan, docs = ctx.read("test_report"), ctx.read("scan_report"), ctx.read("docs_report")
        changes: Dict[str, Any] = ctx.read_prefix("change:")
        approval = ctx.read("approval.design", required=False)
        manifest = sorted(({"path": c["path"], "action": c["action"], "added": c["added"], "removed": c["removed"]}
                           for c in changes.values()), key=lambda c: c["path"])
        blockers: List[str] = []
        warnings: List[str] = []
        if tests["failed"] or tests["errors"] or not tests["passed"]:
            blockers.append("tests are not green")
        blockers += [f"{v['rule']}: {v['message']} ({v['path']})" for v in scan["violations"] if v["severity"] == "high"]
        warnings += [f"{v['rule']}: {v['message']}" for v in scan["violations"] if v["severity"] != "high"]
        blockers += [str(v) for v in ctx.toolbox.policy.check_changeset([m["path"] for m in manifest])]
        if design["high_impact"] and not (approval and approval["approved"]):
            blockers.append("high-impact design has no recorded human approval")
        if "unique_visitors" in design["features"] and spec["privacy"]["mode"] != "hashed":
            blockers.append("personal-data feature without a recorded privacy decision")
        if not docs["endpoints"]:
            blockers.append("API documentation is empty")
        assumed = [a for a in spec["ambiguities"] if (a.get("resolution") or {}).get("source") == "assumed-default"]
        if assumed:
            warnings.append(f"{len(assumed)} ambiguity(ies) resolved by assumption, not by a human: {[a['id'] for a in assumed]}")
        review = {"blockers": blockers, "warnings": warnings, "manifest": manifest,
                  "totals": {"files": len(manifest), "added": sum(m["added"] for m in manifest),
                             "removed": sum(m["removed"] for m in manifest)},
                  "tests": {"passed": tests["passed"], "test_files": tests["test_files"]},
                  "requirements_total": len(spec["functional"])}
        return AgentResult({"review": review}, notes=f"{len(blockers)} blocker(s), {len(warnings)} warning(s)")
