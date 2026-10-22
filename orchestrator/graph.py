"""Explicit task dependency graph: validation, traversal and rendering helpers."""
from __future__ import annotations

from typing import Dict, List, Set

from .models import OrchestratorError, TaskSpec


class GraphError(OrchestratorError):
    pass


def validate(tasks: Dict[str, TaskSpec]) -> None:
    """Reject unknown dependencies and cycles (Kahn's algorithm)."""
    for t in tasks.values():
        for d in t.deps:
            if d not in tasks:
                raise GraphError(f"task {t.id!r} depends on unknown task {d!r}")
            if d == t.id:
                raise GraphError(f"task {t.id!r} depends on itself")
    indeg = {i: len(set(t.deps)) for i, t in tasks.items()}
    children = children_map(tasks)
    queue = [i for i, n in indeg.items() if n == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for c in children[node]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if seen != len(tasks):
        cyc = sorted(i for i, n in indeg.items() if n > 0)
        raise GraphError(f"dependency cycle among: {', '.join(cyc)}")


def children_map(tasks: Dict[str, TaskSpec]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {i: [] for i in tasks}
    for t in tasks.values():
        for d in set(t.deps):
            out.setdefault(d, []).append(t.id)
    return out


def descendants(tasks: Dict[str, TaskSpec], root: str) -> Set[str]:
    children, seen, stack = children_map(tasks), set(), [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def topological_levels(tasks: Dict[str, TaskSpec]) -> List[List[str]]:
    """Groups of tasks that could run in parallel (useful for plans and docs)."""
    validate(tasks)
    level: Dict[str, int] = {}

    def depth(i: str) -> int:
        if i not in level:
            level[i] = 1 + max((depth(d) for d in tasks[i].deps), default=-1)
        return level[i]

    for i in tasks:
        depth(i)
    out: List[List[str]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
    for i, n in level.items():
        out[n].append(i)
    return [sorted(x) for x in out]


def to_mermaid(tasks: Dict[str, TaskSpec], states: Dict[str, str]) -> str:
    style = {"SUCCEEDED": "fill:#d4f4dd", "FAILED": "fill:#f8d0d0", "ROLLED_BACK": "fill:#ffe9b3",
             "ABORTED": "fill:#e0e0e0", "PENDING": "fill:#ffffff", "RUNNING": "fill:#cfe2ff"}
    ident = lambda s: s.replace(".", "_").replace("/", "_").replace("-", "_").replace(":", "_")  # noqa: E731
    lines = ["flowchart LR"]
    for t in tasks.values():
        shape = f'{{{{"{t.id}"}}}}' if t.human else f'["{t.id}"]'
        lines.append(f"  {ident(t.id)}{shape}")
    for t in tasks.values():
        for d in t.deps:
            lines.append(f"  {ident(d)} --> {ident(t.id)}")
    for i, s in states.items():
        if i in tasks:
            lines.append(f"  style {ident(i)} {style.get(s, '')}")
    return "\n".join(lines)
