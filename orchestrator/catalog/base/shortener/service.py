"""Business logic. Framework-agnostic so it can be unit-tested without HTTP."""
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .codegen import generate_code
from .repository import Link, LinkRepository
from .validation import validate_alias, validate_url


class AliasTaken(Exception):
    """The requested custom alias already exists."""


class NotFound(Exception):
    """No link exists for the given code."""


class CodeGenerationError(Exception):
    """Could not allocate a unique code within the retry budget."""


@dataclass(frozen=True)
class Stats:
    code: str
    long_url: str
    total_clicks: int
    last_clicked_at: Optional[float]


class ShortenerService:
    def __init__(
        self,
        repo: LinkRepository,
        clock: Callable[[], float] = time.time,
        code_factory: Callable[[], str] = generate_code,
        max_attempts: int = 5,
    ):
        self._repo = repo
        self._clock = clock
        self._code_factory = code_factory
        self._max_attempts = max_attempts

    def create(self, long_url: str, alias: Optional[str] = None) -> Link:
        url = validate_url(long_url)
        if alias is not None:
            link = Link(validate_alias(alias), url, self._clock())
            if not self._repo.insert_link(link):
                raise AliasTaken(link.code)
            return link
        for _ in range(self._max_attempts):  # bounded retry on (rare) random collisions
            link = Link(self._code_factory(), url, self._clock())
            if self._repo.insert_link(link):
                return link
        raise CodeGenerationError("could not allocate a unique code")

    def resolve(self, code: str) -> str:
        link = self._repo.get_link(code)
        if link is None:
            raise NotFound(code)
        self._repo.record_click(code, self._clock())
        return link.long_url

    def stats(self, code: str) -> Stats:
        link = self._repo.get_link(code)
        if link is None:
            raise NotFound(code)
        total, last = self._repo.click_stats(code)
        return Stats(code, link.long_url, total, last)
