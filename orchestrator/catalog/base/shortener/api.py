"""HTTP layer (FastAPI). Run with: uvicorn --factory shortener.api:create_app"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .ratelimit import TokenBucketLimiter
from .repository import LinkRepository
from .service import (
    AliasTaken,
    CodeGenerationError,
    NotFound,
    ShortenerService,
)
from .validation import MAX_URL_LENGTH, ValidationError


class CreateRequest(BaseModel):
    url: str = Field(..., max_length=MAX_URL_LENGTH)
    alias: Optional[str] = None


def create_app(db_path: Optional[str] = None, limiter: Optional[TokenBucketLimiter] = None) -> FastAPI:
    repo = LinkRepository(db_path or os.environ.get("SHORTENER_DB", "shortener.db"))
    service = ShortenerService(repo)
    limiter = limiter or TokenBucketLimiter(capacity=10, refill_per_sec=10 / 60)  # 10 creates/min/client
    base_url = os.environ.get("SHORTENER_BASE_URL")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        repo.close()

    app = FastAPI(title="URL Shortener", version="1.0.0", lifespan=lifespan)
    app.state.service = service  # exposed for composition/testing (e.g. clock injection)

    def _payload(link, request: Request) -> dict:
        origin = (base_url or str(request.base_url)).rstrip("/")
        return {
            "code": link.code,
            "short_url": f"{origin}/{link.code}",
            "long_url": link.long_url,
            "created_at": link.created_at,
        }

    @app.get("/healthz")
    def healthz() -> dict:
        if not repo.ping():
            raise HTTPException(503, "database unavailable")
        return {"status": "ok"}

    @app.post("/api/v1/urls", status_code=201)
    def create_url(body: CreateRequest, request: Request) -> dict:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "6"})
        try:
            link = service.create(body.url, body.alias)
        except ValidationError as exc:
            raise HTTPException(422, str(exc))
        except AliasTaken:
            raise HTTPException(409, "alias already in use")
        except CodeGenerationError:
            raise HTTPException(503, "could not allocate a code, retry")
        return _payload(link, request)

    @app.get("/api/v1/urls/{code}/stats")
    def stats(code: str) -> dict:
        try:
            s = service.stats(code)
        except NotFound:
            raise HTTPException(404, "no such link")
        return {"code": s.code, "long_url": s.long_url, "total_clicks": s.total_clicks, "last_clicked_at": s.last_clicked_at}

    @app.get("/{code}")
    def redirect(code: str, request: Request):
        try:
            target = service.resolve(code)
        except NotFound:
            raise HTTPException(404, "unknown short code")
        return RedirectResponse(target, status_code=302)

    return app
