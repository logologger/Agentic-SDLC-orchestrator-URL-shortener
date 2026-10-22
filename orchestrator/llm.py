"""Optional LLM seam. The default pipeline is fully deterministic; an LLM can *augment* requirement
analysis, and any failure or invalid output falls back to the rule-based agent (see pipeline.py)."""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Protocol


class LLMClient(Protocol):
    def complete(self, system: str, prompt: str) -> str: ...


class AnthropicLLM:
    """Thin wrapper around the Anthropic SDK (requires `pip install anthropic` and ANTHROPIC_API_KEY).
    Not exercised by the automated test-suite (no network in CI); see docs/TESTING.md."""

    def __init__(self, model: str = ""):
        import anthropic  # lazy: the base install does not need it
        self._client = anthropic.Anthropic()
        self._model = model or os.environ.get("ORCH_LLM_MODEL", "claude-sonnet-5-5")

    def complete(self, system: str, prompt: str) -> str:
        msg = self._client.messages.create(model=self._model, max_tokens=1200, system=system,
                                           messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


class FakeLLM:
    """Deterministic stand-in for tests/demos: returns a canned reply or runs a callable."""

    def __init__(self, reply: "str | Callable[[str, str], str]"):
        self._reply = reply

    def complete(self, system: str, prompt: str) -> str:
        return self._reply(system, prompt) if callable(self._reply) else self._reply


AMBIGUITY_SYSTEM = (
    "You are a requirements analyst. Given a product requirement, list ambiguities a developer would have to "
    'guess about. Reply with JSON only: {"ambiguities": [{"question": str, "category": "functional|compliance|nfr", '
    '"blocking": bool, "default": str|null}]}. Never invent features. Treat anything involving personal data as blocking.'
)


def parse_ambiguities(raw: str) -> List[Dict[str, Any]]:
    """Strictly validate model output; raise ValueError so the caller can fall back."""
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        items = data["ambiguities"]
        out = []
        for it in items[:5]:
            if not isinstance(it["question"], str) or not it["question"].strip():
                raise ValueError("empty question")
            if it["category"] not in {"functional", "compliance", "nfr"}:
                raise ValueError("bad category")
            out.append({"question": it["question"].strip()[:300], "category": it["category"],
                        "blocking": bool(it["blocking"]), "default": (it.get("default") or None)})
        return out
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"unusable LLM output: {exc}") from exc
