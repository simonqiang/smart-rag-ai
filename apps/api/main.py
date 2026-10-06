"""FastAPI composition root (Task 4).

Health endpoints, structured secret-redacted JSON logging with correlation
IDs, and a lifecycle that shuts down cleanly. Readiness is distinct from
liveness: readiness fails closed when PostgreSQL or Redis is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from apps.api.routes import auth as auth_routes
from apps.api.routes import sources as source_routes
from apps.api.routes import users as user_routes
from foundation.config import Settings
from identity_access.authorization import AccessDenied
from identity_access.grants import TargetNotFound
from source_catalog.catalog import CollectionNotFound, SourceNotFound

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

_CHECK_TIMEOUT_SECONDS = 3.0


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, with secrets redacted."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [secret for secret in (secrets or []) if secret]

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[redacted]")
        return json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
                "correlation_id": correlation_id.get(),
            },
            ensure_ascii=False,
        )


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.secret_values()))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


class ReadinessChecker:
    """Probe PostgreSQL and Redis; failures report type names only, never DSNs."""

    def __init__(self, database_url: str, redis_url: str) -> None:
        self._database_url = database_url
        self._redis_url = redis_url

    async def run(self) -> tuple[int, dict]:
        postgres, redis = await asyncio.gather(self._postgres(), self._redis())
        checks = {"postgres": postgres, "redis": redis}
        ok = all(check["ok"] for check in checks.values())
        return (200 if ok else 503), {
            "status": "ready" if ok else "unavailable",
            "checks": checks,
        }

    async def _postgres(self) -> dict:
        try:
            engine = create_async_engine(self._database_url, poolclass=NullPool)
            try:
                async with engine.connect() as connection:
                    await asyncio.wait_for(
                        connection.execute(text("SELECT 1")), timeout=_CHECK_TIMEOUT_SECONDS
                    )
            finally:
                await engine.dispose()
        except Exception as error:  # noqa: BLE001 - readiness must fail closed
            return {"ok": False, "error": type(error).__name__}
        return {"ok": True}

    async def _redis(self) -> dict:
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(
                self._redis_url, socket_connect_timeout=_CHECK_TIMEOUT_SECONDS
            )
            try:
                await asyncio.wait_for(client.ping(), timeout=_CHECK_TIMEOUT_SECONDS)
            finally:
                await client.aclose()
        except Exception as error:  # noqa: BLE001 - readiness must fail closed
            return {"ok": False, "error": type(error).__name__}
        return {"ok": True}


settings = Settings.load()
configure_logging(settings)
readiness = ReadinessChecker(settings.database_url, settings.redis_url)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Persistent engines and broker connections arrive with Task 5.
    yield


app = FastAPI(title="Smart RAG AI", lifespan=_lifespan)
app.include_router(auth_routes.router)
app.include_router(user_routes.router)
app.include_router(source_routes.router)


@app.exception_handler(TargetNotFound)
async def _target_not_found(_request: Request, _exc: TargetNotFound) -> JSONResponse:
    # Unknown and cross-workspace targets are indistinguishable by design:
    # object IDs must not be probeable.
    return JSONResponse(status_code=404, content={"detail": "no such user"})


@app.exception_handler(SourceNotFound)
async def _source_not_found(_request: Request, _exc: SourceNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "no such source"})


@app.exception_handler(CollectionNotFound)
async def _collection_not_found(
    _request: Request, _exc: CollectionNotFound
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "no such collection"})


@app.exception_handler(AccessDenied)
async def _access_denied(_request: Request, _exc: AccessDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "not allowed"})


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    request_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:12]
    token = correlation_id.set(request_id)
    try:
        response = await call_next(request)
    finally:
        correlation_id.reset(token)
    response.headers["x-correlation-id"] = request_id
    return response


@app.get("/health/live")
async def live() -> dict:
    return {"status": "live"}


@app.get("/health/ready")
async def ready() -> JSONResponse:
    status, body = await readiness.run()
    return JSONResponse(status_code=status, content=body)
