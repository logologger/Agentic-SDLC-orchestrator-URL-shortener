"""SQLite persistence for links and click events."""
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code TEXT PRIMARY KEY,
    long_url TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES links(code),
    clicked_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);
"""


@dataclass(frozen=True)
class Link:
    code: str
    long_url: str
    created_at: float


class LinkRepository:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Forward-only, idempotent schema upgrades for databases created by older releases."""

    def insert_link(self, link: Link) -> bool:
        """Insert a link; returns False when the code is already taken."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO links (code, long_url, created_at) VALUES (?, ?, ?)",
                    (link.code, link.long_url, link.created_at),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_link(self, code: str) -> Optional[Link]:
        with self._lock:
            row = self._conn.execute(
                "SELECT code, long_url, created_at FROM links WHERE code = ?", (code,)
            ).fetchone()
        return Link(*row) if row else None

    def record_click(self, code: str, clicked_at: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO clicks (code, clicked_at) VALUES (?, ?)", (code, clicked_at))

    def click_stats(self, code: str) -> Tuple[int, Optional[float]]:
        with self._lock:
            total, last = self._conn.execute(
                "SELECT COUNT(*), MAX(clicked_at) FROM clicks WHERE code = ?", (code,)
            ).fetchone()
        return total, last

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()
