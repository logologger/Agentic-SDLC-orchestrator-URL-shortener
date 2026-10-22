"""Feature catalog: what the implementer agents know how to build or change.

`core` is the greenfield baseline (files under ./base). Other features are *brownfield change sets*:
structured, anchor-based edits against the existing code. An edit whose anchor is missing or ambiguous
fails loudly instead of guessing, which is what triggers rollback in the orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

BASE_DIR = Path(__file__).parent / "base"


@dataclass(frozen=True)
class Edit:
    old: str
    new: str


@dataclass
class Feature:
    name: str
    summary: str
    keywords: List[str]                      # symbols/words used by impact analysis to locate affected code
    edits: Dict[str, List[Edit]] = field(default_factory=dict)      # path -> ordered edits
    new_files: Dict[str, str] = field(default_factory=dict)         # path -> content
    api_changes: List[str] = field(default_factory=list)
    schema_change: bool = False
    pii: bool = False
    decisions: List[dict] = field(default_factory=list)
    risks: List[dict] = field(default_factory=list)
    marker: str = ""                         # (path, substring) proving the feature is already present
    marker_path: str = ""


def base_files() -> Dict[str, str]:
    out = {}
    for p in sorted(BASE_DIR.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py"}:
            out[p.relative_to(BASE_DIR).as_posix()] = p.read_text()
    out["requirements.txt"] = "fastapi>=0.110\nuvicorn[standard]>=0.29\nhttpx>=0.27\npytest>=8.0\n"
    return out


# ---------------------------------------------------------------------------------------------------
# expiry: time-limited links
# ---------------------------------------------------------------------------------------------------
EXPIRY = Feature(
    name="expiry",
    summary="Links may carry a TTL; expired links answer 410 Gone and stop counting clicks.",
    keywords=["expire", "ttl", "created_at", "links", "resolve", "create"],
    marker="expires_at", marker_path="shortener/repository.py",
    schema_change=True,
    api_changes=[
        "POST /api/v1/urls: optional `ttl_seconds` (60..31536000); response gains `expires_at` (nullable)",
        "GET /{code}: new 410 Gone response for expired links",
    ],
    decisions=[
        {"id": "D-EXP-1", "decision": "Store absolute `expires_at` (REAL, nullable) on `links`",
         "rationale": "Absolute timestamps make reads a single comparison and survive TTL policy changes",
         "alternatives": ["store ttl + created_at (needs arithmetic on every read)", "background reaper deleting rows (loses analytics, needs scheduler)"]},
        {"id": "D-EXP-2", "decision": "Lazy expiry check at resolve time; rows are kept",
         "rationale": "No scheduler to operate; analytics history preserved; 410 is more informative than 404",
         "alternatives": ["periodic purge job"]},
        {"id": "D-EXP-3", "decision": "Additive, idempotent migration (`ALTER TABLE ... ADD COLUMN`) executed on startup",
         "rationale": "Backward compatible: existing rows get NULL (= never expires); old binaries ignore the column",
         "alternatives": ["offline migration tool", "new table + backfill"]},
    ],
    risks=[
        {"risk": "Schema change on a live database", "likelihood": "low", "impact": "high",
         "mitigation": "Additive nullable column, idempotent migration, covered by a legacy-database test; rollback = ignore column"},
        {"risk": "Clock skew between nodes makes links expire early/late", "likelihood": "medium", "impact": "low",
         "mitigation": "Single clock source per deployment; document NTP requirement"},
    ],
    edits={
        "shortener/validation.py": [
            Edit("MAX_URL_LENGTH = 2048\n",
                 "MAX_URL_LENGTH = 2048\nMIN_TTL_SECONDS = 60\nMAX_TTL_SECONDS = 365 * 24 * 3600\n"),
            Edit("def validate_alias(alias: str) -> str:\n",
                 'def validate_ttl(ttl_seconds: int) -> int:\n'
                 '    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):\n'
                 '        raise ValidationError("ttl_seconds must be an integer")\n'
                 '    if not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:\n'
                 '        raise ValidationError(f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}")\n'
                 '    return ttl_seconds\n\n\n'
                 "def validate_alias(alias: str) -> str:\n"),
        ],
        "shortener/repository.py": [
            Edit("    created_at: float\n\n\nclass LinkRepository:",
                 "    created_at: float\n    expires_at: Optional[float] = None\n\n\nclass LinkRepository:"),
            Edit("    created_at REAL NOT NULL\n);\nCREATE TABLE IF NOT EXISTS clicks (",
                 "    created_at REAL NOT NULL,\n    expires_at REAL\n);\nCREATE TABLE IF NOT EXISTS clicks ("),
            Edit('        """Forward-only, idempotent schema upgrades for databases created by older releases."""\n',
                 '        """Forward-only, idempotent schema upgrades for databases created by older releases."""\n'
                 '        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(links)")}\n'
                 '        if "expires_at" not in columns:\n'
                 '            self._conn.execute("ALTER TABLE links ADD COLUMN expires_at REAL")\n'),
            Edit('                    "INSERT INTO links (code, long_url, created_at) VALUES (?, ?, ?)",\n'
                 '                    (link.code, link.long_url, link.created_at),\n',
                 '                    "INSERT INTO links (code, long_url, created_at, expires_at) VALUES (?, ?, ?, ?)",\n'
                 '                    (link.code, link.long_url, link.created_at, link.expires_at),\n'),
            Edit('"SELECT code, long_url, created_at FROM links WHERE code = ?"',
                 '"SELECT code, long_url, created_at, expires_at FROM links WHERE code = ?"'),
        ],
        "shortener/service.py": [
            Edit("from .validation import validate_alias, validate_url",
                 "from .validation import validate_alias, validate_ttl, validate_url"),
            Edit("class NotFound(Exception):",
                 'class LinkExpired(Exception):\n    """The link existed but its TTL has elapsed."""\n\n\nclass NotFound(Exception):'),
            Edit("    def create(self, long_url: str, alias: Optional[str] = None) -> Link:\n        url = validate_url(long_url)\n",
                 "    def create(self, long_url: str, alias: Optional[str] = None, ttl_seconds: Optional[int] = None) -> Link:\n"
                 "        url = validate_url(long_url)\n"
                 "        now = self._clock()\n"
                 "        expires_at = None if ttl_seconds is None else now + validate_ttl(ttl_seconds)\n"),
            Edit("Link(validate_alias(alias), url, self._clock())", "Link(validate_alias(alias), url, now, expires_at)"),
            Edit("Link(self._code_factory(), url, self._clock())", "Link(self._code_factory(), url, now, expires_at)"),
            Edit("            raise NotFound(code)\n        self._repo.record_click(",
                 "            raise NotFound(code)\n"
                 "        if link.expires_at is not None and link.expires_at <= self._clock():\n"
                 "            raise LinkExpired(code)\n"
                 "        self._repo.record_click("),
        ],
        "shortener/api.py": [
            Edit("    NotFound,\n    ShortenerService,\n", "    LinkExpired,\n    NotFound,\n    ShortenerService,\n"),
            Edit("    alias: Optional[str] = None\n", "    alias: Optional[str] = None\n    ttl_seconds: Optional[int] = None\n"),
            Edit("service.create(body.url, body.alias)", "service.create(body.url, body.alias, body.ttl_seconds)"),
            Edit('            "created_at": link.created_at,\n',
                 '            "created_at": link.created_at,\n            "expires_at": link.expires_at,\n'),
            Edit('            raise HTTPException(404, "unknown short code")\n',
                 '            raise HTTPException(404, "unknown short code")\n'
                 '        except LinkExpired:\n'
                 '            raise HTTPException(410, "link has expired")\n'),
        ],
    },
    new_files={
        "tests/test_expiry.py": '''import sqlite3
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.repository import LinkRepository
from shortener.service import LinkExpired, ShortenerService
from shortener.validation import ValidationError


def make_service(now):
    return ShortenerService(LinkRepository(":memory:"), clock=lambda: now[0])


def test_link_expires_after_ttl_and_stops_counting():
    now = [1000.0]
    svc = make_service(now)
    link = svc.create("https://example.com", ttl_seconds=60)
    assert link.expires_at == 1060.0
    assert svc.resolve(link.code) == "https://example.com"
    now[0] = 1060.0
    with pytest.raises(LinkExpired):
        svc.resolve(link.code)
    assert svc.stats(link.code).total_clicks == 1


def test_links_without_ttl_never_expire():
    now = [0.0]
    svc = make_service(now)
    link = svc.create("https://example.com")
    now[0] = 10 ** 10
    assert svc.resolve(link.code) == "https://example.com"


@pytest.mark.parametrize("ttl", [0, 59, -5, 365 * 24 * 3600 + 1, True, "60"])
def test_rejects_invalid_ttl(ttl):
    with pytest.raises(ValidationError):
        make_service([0.0]).create("https://example.com", ttl_seconds=ttl)


def test_migrates_legacy_database_without_expires_at(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE links (code TEXT PRIMARY KEY, long_url TEXT NOT NULL, created_at REAL NOT NULL);"
        "INSERT INTO links VALUES ('legacy1', 'https://example.com', 1.0);"
    )
    conn.commit()
    conn.close()
    repo = LinkRepository(path)
    assert repo.get_link("legacy1").expires_at is None
    repo.close()
    LinkRepository(path).close()  # second open is a no-op migration (idempotent)


def test_api_returns_410_for_expired_link_and_exposes_expires_at():
    app = create_app(":memory:")
    with TestClient(app) as client:
        created = client.post("/api/v1/urls", json={"url": "https://example.com", "ttl_seconds": 60}).json()
        assert created["expires_at"] is not None
        assert client.get("/" + created["code"], follow_redirects=False).status_code == 302
        with mock.patch.object(app.state.service, "_clock", return_value=created["expires_at"] + 1):
            assert client.get("/" + created["code"], follow_redirects=False).status_code == 410
        assert client.post("/api/v1/urls", json={"url": "https://example.com", "ttl_seconds": 5}).status_code == 422
''',
    },
)

# ---------------------------------------------------------------------------------------------------
# unique_visitors: privacy-preserving visitor counting
# ---------------------------------------------------------------------------------------------------
UNIQUE_VISITORS = Feature(
    name="unique_visitors",
    summary="Count unique visitors per link using a salted, daily-rotating hash; no raw IP or user-agent is stored.",
    keywords=["click", "clicks", "record_click", "stats", "resolve"],
    marker="visitor_hash", marker_path="shortener/repository.py",
    schema_change=True, pii=True,
    api_changes=["GET /api/v1/urls/{code}/stats: response gains `unique_visitors` (integer)"],
    decisions=[
        {"id": "D-UV-1", "decision": "Identify visitors by HMAC-SHA256 keyed with a secret salt over (day, ip, user-agent), truncated to 128 bits",
         "rationale": "Raw IP/user-agent are personal data (GDPR/CCPA); a keyed hash allows counting without re-identification",
         "alternatives": ["store raw IP (rejected by policy CMP-001)", "cookie-based ids (needs consent banner, defeats curl/bots)"]},
        {"id": "D-UV-2", "decision": "Salt rotates daily, so uniqueness is per-day",
         "rationale": "Limits cross-day tracking (data minimisation); a leaked hash table cannot be linked across days",
         "alternatives": ["static salt (long-term trackable)"]},
    ],
    risks=[
        {"risk": "Multi-day counts over-count returning visitors (different hash each day)", "likelihood": "high", "impact": "low",
         "mitigation": "Documented limitation; metric is 'unique visitors per day, summed'"},
        {"risk": "Salt lost on restart if VISITOR_SALT unset (counts reset)", "likelihood": "medium", "impact": "low",
         "mitigation": "Operators set VISITOR_SALT from the secret store; startup uses a random salt otherwise"},
        {"risk": "Hash of IPv4 space is brute-forceable if the salt leaks", "likelihood": "low", "impact": "medium",
         "mitigation": "Salt is a server secret, never persisted next to the data; rotates daily in practice via VISITOR_SALT rotation"},
    ],
    edits={
        "shortener/repository.py": [
            Edit("CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);\n",
                 "CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);\n"
                 "CREATE TABLE IF NOT EXISTS visitors (\n"
                 "    code TEXT NOT NULL REFERENCES links(code),\n"
                 "    visitor_hash TEXT NOT NULL,\n"
                 "    first_seen REAL NOT NULL,\n"
                 "    PRIMARY KEY (code, visitor_hash)\n"
                 ");\n"),
            Edit('    def record_click(self, code: str, clicked_at: float) -> None:\n'
                 '        with self._lock, self._conn:\n'
                 '            self._conn.execute("INSERT INTO clicks (code, clicked_at) VALUES (?, ?)", (code, clicked_at))\n',
                 '    def record_click(self, code: str, clicked_at: float, visitor_hash: Optional[str] = None) -> None:\n'
                 '        with self._lock, self._conn:\n'
                 '            self._conn.execute("INSERT INTO clicks (code, clicked_at) VALUES (?, ?)", (code, clicked_at))\n'
                 '            if visitor_hash:\n'
                 '                self._conn.execute(\n'
                 '                    "INSERT OR IGNORE INTO visitors (code, visitor_hash, first_seen) VALUES (?, ?, ?)",\n'
                 '                    (code, visitor_hash, clicked_at),\n'
                 '                )\n'),
            Edit("    def ping(self) -> bool:\n",
                 "    def unique_visitors(self, code: str) -> int:\n"
                 "        with self._lock:\n"
                 '            return self._conn.execute("SELECT COUNT(*) FROM visitors WHERE code = ?", (code,)).fetchone()[0]\n\n'
                 "    def ping(self) -> bool:\n"),
        ],
        "shortener/service.py": [
            Edit("    last_clicked_at: Optional[float]\n", "    last_clicked_at: Optional[float]\n    unique_visitors: int = 0\n"),
            Edit("    def resolve(self, code: str) -> str:\n",
                 "    def resolve(self, code: str, visitor_hash: Optional[str] = None) -> str:\n"),
            Edit("self._repo.record_click(code, self._clock())", "self._repo.record_click(code, self._clock(), visitor_hash)"),
            Edit("return Stats(code, link.long_url, total, last)",
                 "return Stats(code, link.long_url, total, last, self._repo.unique_visitors(code))"),
        ],
        "shortener/api.py": [
            Edit("from .ratelimit import TokenBucketLimiter\n",
                 "from .privacy import visitor_hash\nfrom .ratelimit import TokenBucketLimiter\n"),
            Edit("def create_app(db_path: Optional[str] = None, limiter: Optional[TokenBucketLimiter] = None) -> FastAPI:\n",
                 "def _visitor(request: Request) -> str:\n"
                 '    ip = request.client.host if request.client else "unknown"\n'
                 '    return visitor_hash(ip, request.headers.get("user-agent", ""))\n\n\n'
                 "def create_app(db_path: Optional[str] = None, limiter: Optional[TokenBucketLimiter] = None) -> FastAPI:\n"),
            Edit("            target = service.resolve(code)\n", "            target = service.resolve(code, _visitor(request))\n"),
            Edit('"last_clicked_at": s.last_clicked_at}', '"last_clicked_at": s.last_clicked_at, "unique_visitors": s.unique_visitors}'),
        ],
    },
    new_files={
        "shortener/privacy.py": '''"""Privacy-preserving visitor identification. Raw IPs/user-agents never leave this function."""
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

_PROCESS_SALT = secrets.token_hex(16)


def visitor_hash(ip: str, user_agent: str, now: Optional[float] = None, salt: Optional[str] = None) -> str:
    """Keyed, day-scoped hash. Uniqueness is per UTC day by design (data minimisation)."""
    day = int((time.time() if now is None else now) // 86400)
    key = (salt or os.environ.get("VISITOR_SALT") or _PROCESS_SALT).encode()
    return hmac.new(key, f"{day}|{ip}|{user_agent}".encode(), hashlib.sha256).hexdigest()[:32]
''',
        "tests/test_unique_visitors.py": '''from fastapi.testclient import TestClient

from shortener.api import create_app
from shortener.privacy import visitor_hash
from shortener.repository import LinkRepository


def test_hash_is_stable_within_a_day_and_rotates_across_days():
    a = visitor_hash("203.0.113.9", "ua", now=86400 * 10 + 5, salt="s")
    assert a == visitor_hash("203.0.113.9", "ua", now=86400 * 10 + 9000, salt="s")
    assert a != visitor_hash("203.0.113.9", "ua", now=86400 * 11 + 5, salt="s")
    assert a != visitor_hash("203.0.113.10", "ua", now=86400 * 10 + 5, salt="s")
    assert "203.0.113.9" not in a


def test_repository_counts_distinct_hashes_only():
    repo = LinkRepository(":memory:")
    from shortener.repository import Link
    repo.insert_link(Link("abc1234", "https://example.com", 1.0))
    for h in ("h1", "h1", "h2", None):
        repo.record_click("abc1234", 2.0, h)
    assert repo.click_stats("abc1234")[0] == 4
    assert repo.unique_visitors("abc1234") == 2


def test_api_reports_unique_visitors_and_stores_no_raw_identifiers(tmp_path):
    db = str(tmp_path / "uv.db")
    with TestClient(create_app(db)) as client:
        code = client.post("/api/v1/urls", json={"url": "https://example.com"}).json()["code"]
        for ua in ("browser-a", "browser-a", "browser-b"):
            client.get("/" + code, headers={"user-agent": ua}, follow_redirects=False)
        stats = client.get(f"/api/v1/urls/{code}/stats").json()
        assert stats["total_clicks"] == 3 and stats["unique_visitors"] == 2
    import sqlite3
    dump = "\\n".join(sqlite3.connect(db).iterdump())
    assert "browser-a" not in dump and "testclient" not in dump
''',
    },
)

FEATURES: Dict[str, Feature] = {f.name: f for f in (EXPIRY, UNIQUE_VISITORS)}


class EditError(Exception):
    """An edit anchor was missing or ambiguous; the change set cannot be applied safely."""


def apply_edits(path: str, content: str, edits: List[Edit]) -> str:
    for edit in edits:
        hits = content.count(edit.old)
        if hits != 1:
            raise EditError(f"{path}: anchor matched {hits} times (expected 1): {edit.old.strip()[:70]!r}")
        content = content.replace(edit.old, edit.new)
    return content
