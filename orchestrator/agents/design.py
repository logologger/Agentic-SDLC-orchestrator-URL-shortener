"""Design-phase agents: speculative explorer, brownfield impact analysis, architect, planner."""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Set

from ..catalog.features import FEATURES, EditError, apply_edits, base_files
from ..models import AgentError, AgentResult, AutonomyProfile, TaskSpec
from .base import Agent, TaskContext

CORE_DECISIONS = [
    {"id": "D-CORE-1", "decision": "FastAPI + SQLite behind a repository interface",
     "rationale": "Smallest operable stack for a prototype; repository seam allows swapping in Postgres without touching the service layer",
     "alternatives": ["Flask (less typing/OpenAPI support)", "Postgres from day one (operational weight for a prototype)"]},
    {"id": "D-CORE-2", "decision": "Random 7-char base62 codes from a CSPRNG with bounded collision retry",
     "rationale": "Codes are unguessable (no enumeration of private links); 62^7 ~ 3.5e12 keeps collision probability negligible",
     "alternatives": ["sequential ids + base62 (enumerable)", "hash of URL (same URL => same code leaks existence)"]},
    {"id": "D-CORE-3", "decision": "HTTP 302 redirects",
     "rationale": "301 is cached by browsers, which would hide clicks from analytics and make links irrevocable", "alternatives": ["301"]},
    {"id": "D-CORE-4", "decision": "In-process token-bucket limiter keyed by client address",
     "rationale": "No extra infrastructure; bounded memory via LRU eviction. Replace with a shared store when scaling horizontally",
     "alternatives": ["Redis-based limiter (needs infra)", "fixed window (burst at window edges)"]},
    {"id": "D-CORE-5", "decision": "Reject non-http(s) schemes and private/loopback/link-local targets at creation time",
     "rationale": "Prevents the service being used for javascript:/file: payloads and SSRF-style probing of internal hosts",
     "alternatives": ["allow everything and rely on browsers"]},
]
CORE_RISKS = [
    {"risk": "Service abused for phishing / malware redirection", "likelihood": "high", "impact": "high",
     "mitigation": "Rate limiting now; reputation checks and abuse reporting are a documented follow-up"},
    {"risk": "DNS rebinding bypasses the private-host check (hostnames resolved later)", "likelihood": "low", "impact": "medium",
     "mitigation": "Checks are on literal hosts only; redirect service never fetches the target, so no server-side SSRF"},
    {"risk": "Single SQLite node is a SPOF and write-throughput bound", "likelihood": "medium", "impact": "medium",
     "mitigation": "Repository interface isolates the store; /healthz exposes liveness for orchestrators"},
    {"risk": "Rate limiter state lost on restart / not shared across nodes", "likelihood": "medium", "impact": "low",
     "mitigation": "Documented limitation; fail-open is acceptable for creation endpoint"},
]


def _files_for(features: List[str], present: Set[str]) -> Dict[str, str]:
    """path -> 'create' | 'modify' for the requested, not-yet-present features."""
    files: Dict[str, str] = {}
    if "core" in features:
        files.update({p: "create" for p in base_files()})
    for name in features:
        f = FEATURES.get(name)
        if not f or name in present:
            continue
        for p in f.edits:
            files.setdefault(p, "modify")
        for p in f.new_files:
            files[p] = "create"
    return files


class ExplorerAgent(Agent):
    """Speculative: drafts the file footprint from the *current* spec while humans are still answering questions.
    If the spec changes afterwards the engine invalidates and re-runs this task (dynamic re-planning)."""
    name = "explorer"
    profile = AutonomyProfile(name="analyst")

    def run(self, ctx: TaskContext) -> AgentResult:
        spec = ctx.read("spec")
        files = _files_for(spec["features"], set())
        return AgentResult({"design_draft": {"features": spec["features"], "files_estimate": sorted(files)}},
                           notes=f"draft footprint: {len(files)} file(s)")


class ImpactAgent(Agent):
    """Brownfield codebase reasoning: AST import graph, routes and tables -> what a change really touches."""
    name = "impact"
    profile = AutonomyProfile(name="analyst")

    def run(self, ctx: TaskContext) -> AgentResult:
        spec = ctx.read("spec")
        files = {p: ctx.toolbox.read(p) for p in ctx.toolbox.list_files(".py")}
        if not files:
            raise AgentError("no existing code to analyse")
        imports = self._import_graph(files)
        routes = [{"module": p, "route": r} for p, src in files.items() for r in re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"', src)]
        tables = {p: re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", src) for p, src in files.items()}
        tables = {p: t for p, t in tables.items() if t}
        impacted: Dict[str, List[str]] = {}
        already, requested = [], [f for f in spec["features"] if f != "core"]
        for name in requested:
            feat = FEATURES[name]
            if feat.marker in files.get(feat.marker_path, ""):
                already.append(name)
                continue
            for path in feat.edits:
                impacted.setdefault(path, []).append(f"{name}: change set edits this module")
            for path, src in files.items():
                if path.startswith("tests/"):
                    continue
                symbols = self._symbols(src)
                hit = sorted(k for k in feat.keywords if k in symbols)
                if hit and path not in feat.edits:
                    impacted.setdefault(path, []).append(f"{name}: references {', '.join(hit)}")
        direct = set(impacted)
        transitive: Dict[str, List[str]] = {}
        frontier = set(direct)
        while frontier:  # reverse-import closure: who depends on what we change?
            nxt = {m for m, deps in imports.items() if deps & frontier and m not in direct and m not in transitive}
            for m in nxt:
                transitive[m] = sorted(imports[m] & (direct | set(transitive)))
            frontier = nxt
        tests = sorted(p for p in files if p.startswith("tests/") and imports.get(p, set()) & (direct | set(transitive)))
        todo = [f for f in requested if f not in already]
        schema = any(FEATURES[f].schema_change for f in todo)
        report = {
            "requested": requested, "already_implemented": already, "nothing_to_do": not todo,
            "direct": [{"module": m, "why": w} for m, w in sorted(impacted.items())],
            "transitive": [{"module": m, "via": v} for m, v in sorted(transitive.items())],
            "routes_touched": [r for r in routes if r["module"] in direct or r["module"] in transitive],
            "tables": tables, "existing_tests_to_rerun": tests,
            "risk": "high" if schema else "medium", "risk_reason": "schema change on existing data" if schema else "code-only change",
        }
        return AgentResult({"impact": report}, notes=f"{len(direct)} direct, {len(transitive)} transitive module(s)")

    @staticmethod
    def _import_graph(files: Dict[str, str]) -> Dict[str, Set[str]]:
        graph: Dict[str, Set[str]] = {}
        for path, src in files.items():
            deps: Set[str] = set()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.ImportFrom):
                    if node.level and node.module:                               # from .service import X
                        deps.add(f"{path.rsplit('/', 1)[0]}/{node.module}.py")
                    elif node.module and node.module.startswith("shortener."):   # from shortener.service import X
                        deps.add(node.module.replace(".", "/") + ".py")
            graph[path] = {d for d in deps if d in files}
        return graph

    @staticmethod
    def _symbols(src: str) -> Set[str]:
        out: Set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                out.add(node.name)
            elif isinstance(node, ast.Name):
                out.add(node.id)
            elif isinstance(node, ast.Attribute):
                out.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "CREATE TABLE" in node.value:
                out.update(re.findall(r"\b\w+\b", node.value.lower()))
        return out


class ArchitectAgent(Agent):
    name = "architect"
    profile = AutonomyProfile(name="analyst")

    def run(self, ctx: TaskContext) -> AgentResult:
        spec = ctx.read("spec")
        impact = ctx.read("impact", required=False)
        ctx.read("design_draft", required=False)   # lineage: the speculative draft informed this design
        already = set(impact["already_implemented"]) if impact else set()
        features = spec["features"]
        todo = [f for f in features if f not in already]
        if not todo or (impact and impact["nothing_to_do"]):
            raise AgentError("every requested capability already exists; nothing to design")
        files = _files_for(features, already)
        decisions = list(CORE_DECISIONS) if "core" in features else []
        risks = list(CORE_RISKS) if "core" in features else []
        api_changes: List[str] = []
        reasons: List[str] = []
        for name in todo:
            f = FEATURES.get(name)
            if not f:
                continue
            decisions += f.decisions
            risks += f.risks
            api_changes += f.api_changes
            if f.schema_change:
                reasons.append(f"{name}: database schema change")
            if f.pii:
                reasons.append(f"{name}: processes personal data (privacy review)")
            if f.api_changes:
                reasons.append(f"{name}: public API contract change")
        if spec["privacy"]["mode"] == "hashed":
            decisions.append({"id": "D-PRIV-1", "decision": "Personal data is hashed at the edge; raw values never reach storage or logs",
                              "rationale": f"Human decision recorded in spec: {spec['privacy'].get('basis', '')[:120]}", "alternatives": ["raw storage (blocked, CMP-001/002)"]})
        design = {
            "features": todo, "files": files, "api_changes": api_changes,
            "schema_change": any(FEATURES[f].schema_change for f in todo if f in FEATURES),
            "decisions": decisions, "risks": risks,
            "high_impact": bool(reasons), "high_impact_reasons": reasons,
            "traceability": {r["id"]: r["acceptance"] for r in spec["functional"]},
        }
        return AgentResult({"design": design}, notes=f"{len(files)} file(s), high_impact={design['high_impact']}")


class PlannerAgent(Agent):
    """Task decomposition: one work item per file, ordered by the *import* dependencies of the code being produced.
    Expands the live graph with those tasks and rewires the `impl.sync` barrier to wait for them."""
    name = "planner"
    profile = AutonomyProfile(name="planner")

    def run(self, ctx: TaskContext) -> AgentResult:
        design = ctx.read("design")
        files: Dict[str, str] = design["files"]
        contents = self._projected_contents(ctx, design)
        src_files = [p for p in files if not p.startswith("tests/")]
        items, expand = [], []
        for path in sorted(files):
            item_id = f"impl:{path}"
            if path.startswith("tests/"):
                deps = [f"impl:{p}" for p in src_files if p.endswith(".py")]       # tests follow the code under test
            else:
                deps = [f"impl:{p}" for p in self._local_imports(path, contents.get(path, "")) if p in files]
            items.append({"id": item_id, "title": f"{files[path]} {path}", "file": path,
                          "kind": "test" if path.startswith("tests/") else "source", "depends_on": sorted(set(deps))})
            expand.append(TaskSpec(id=item_id, agent="implementer", fallback="implementer_safe", deps=[ctx.task.id] + sorted(set(deps)),
                                   params={"file": path}, side_effects=True, phase="implement",
                                   description=f"{files[path]} {path}", max_retries=2))
        levels: Dict[str, int] = {}
        for it in items:  # items are sorted but deps may sort later; resolve by repeated relaxation
            levels[it["id"]] = 0
        for _ in items:
            for it in items:
                levels[it["id"]] = max([levels[d] + 1 for d in it["depends_on"]], default=0)
        parallel = [sorted(i for i, l in levels.items() if l == n) for n in range(max(levels.values(), default=0) + 1)]
        plan = {"work_items": items, "parallel_levels": parallel,
                "rationale": "Source units are ordered by the import graph of the produced code; test units wait for all source units "
                             "so they are authored against the final contract. Units within a level run in parallel."}
        return AgentResult({"plan": plan}, expand=expand, add_deps={"impl.sync": [t.id for t in expand]},
                           notes=f"{len(items)} work items in {len(parallel)} levels")

    @staticmethod
    def _local_imports(path: str, src: str) -> List[str]:
        if not src.strip() or not path.endswith(".py"):
            return []
        out = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                out.append(f"{path.rsplit('/', 1)[0]}/{node.module}.py")
        return sorted(set(out))

    @staticmethod
    def _projected_contents(ctx: TaskContext, design: Dict[str, Any]) -> Dict[str, str]:
        """What each file will look like after the change (used only to compute import dependencies)."""
        base = base_files()
        out: Dict[str, str] = {}
        for path, action in design["files"].items():
            if action == "create":
                src = base.get(path, "")
                for name in design["features"]:
                    src = FEATURES[name].new_files.get(path, src) if name in FEATURES else src
                out[path] = src
            elif ctx.toolbox.exists(path):
                src = ctx.toolbox.read(path)
                for name in design["features"]:
                    if name in FEATURES and path in FEATURES[name].edits:
                        try:
                            src = apply_edits(path, src, FEATURES[name].edits[path])
                        except EditError as exc:
                            raise AgentError(f"planning failed, code drifted from the catalog: {exc}")
                out[path] = src
        return out
