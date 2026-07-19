"""
PineTunnel Server -- Application factory.

Route handlers live in apps.server.routes.* -- this module
wires them together.  Lifespan, service initialization, and startup
helpers live in apps.server.config.lifespan.
"""

import os
from importlib.resources import files
from pathlib import Path as _Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Backward-compatible re-exports for tests and external importers.
# These are None at import time; actual instances are created by
# _init_services() during lifespan startup.
from apps.server.config.lifespan import (  # noqa: F401
    CONFIG,
    PINETUNNEL_AVAILABLE,
    _is_production,
    _security_self_test,
    _ws_push_signal,
    client_manager,
    conn_manager,
    db_manager,
    lifespan,
    pinetunnel_router,
    ws_manager,
)
from apps.server.config.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------
app = FastAPI(
    title="API",
    version="1.0",
    description="",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    openapi_url=None if _is_production else "/openapi.json",
    redoc_url=None if _is_production else "/redoc",
)

# CORS -- default to empty (deny all cross-origin requests) for safety.
# If SERVER_CORS_ORIGINS is set, only those origins are allowed.
# Never allow allow_origins=["*"] with allow_credentials=True.
_cors_env = os.getenv("SERVER_CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else []
_use_credentials = bool(_cors_origins)  # Only send credentials when specific origins are set

if not _cors_origins:
    # No origins configured: allow same-origin only (empty list denies all cross-origin).
    # This is safer than ["*"] which permits any origin with credentials.
    logger.warning("No SERVER_CORS_ORIGINS set - cross-origin requests will be denied")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_use_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"] if _cors_origins else [],
    allow_headers=(
        ["Authorization", "Content-Type", "X-Admin-Key", "X-PT-Signature", "X-Correlation-ID"]
        if _cors_origins
        else []
    ),
)

_TELEGRAM_BOT_URL = os.getenv("TELEGRAM_BOT_URL", "")


@app.get("/", include_in_schema=False)
async def root_redirect():
    if _TELEGRAM_BOT_URL:
        return RedirectResponse(url=_TELEGRAM_BOT_URL, status_code=301)
    return JSONResponse({"status": "ok", "service": "PineTunnel"})


# Custom error handlers -- sanitize error details in production
@app.exception_handler(404)
async def custom_404(request, exc):
    return JSONResponse(status_code=404, content={"detail": "Not found"})


@app.exception_handler(405)
async def custom_405(request, exc):
    return JSONResponse(status_code=405, content={"detail": "Method not allowed"})


@app.exception_handler(500)
async def custom_500(request, exc):
    logger.error("Unhandled exception: %s: %s", type(exc).__name__, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Catch-all exception handler -- logs details, returns sanitized response."""
    logger.error(
        "Unhandled exception on %s %s: %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    """Sanitize Pydantic validation errors to prevent schema exposure."""
    logger.warning(
        "Validation error on %s %s: %s",
        request.method,
        request.url.path,
        exc.errors(),
    )
    if _is_production:
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


_security_self_test(app)


# ---------------------------------------------------------------------------
# Production middleware
# ---------------------------------------------------------------------------
try:
    from apps.server.middleware.main import setup_middleware

    setup_middleware(app)
except ImportError as e:
    logger.warning("Production modules not loaded: %s", e)

# ---------------------------------------------------------------------------
# Include route routers
# ---------------------------------------------------------------------------
from apps.server.routes import routers

for router in routers:
    app.include_router(router)

# Include PineTunnel router
if PINETUNNEL_AVAILABLE and pinetunnel_router:
    app.include_router(pinetunnel_router)
    logger.info("PineTunnel webhook integration loaded")

# Trade analytics routers
try:
    from apps.server.routes.trade_analytics import admin_router
    from apps.server.routes.trade_analytics import router as analytics_router

    app.include_router(analytics_router)
    app.include_router(admin_router)
    logger.info("Trade Analytics API loaded")
except ImportError as e:
    logger.warning("Trade Analytics API not loaded: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        app,
        host=CONFIG.get("host", "0.0.0.0"),
        port=CONFIG.get("port", 8000),
        reload=CONFIG.get("debug", False),
        log_level="info",
        timeout_keep_alive=30,
    )
