"""PineTunnel Webhook Server — main entry point."""

import os
import sys

# Defensive: block psycopg2 from being imported.
#
# psycopg2 is incompatible with Python 3.13 (undefined symbol:
# _PyInterpreterState_Get). We use psycopg[binary] (v3) instead. However,
# Render caches the venv between deploys. If a previous deploy installed
# psycopg2-binary (before the switch to psycopg3), the stale .so file
# persists in the cached venv. The buildCommand's `pip uninstall
# psycopg2-binary psycopg2 -y` removes it, but as a belt-and-suspenders
# guard, we also set sys.modules["psycopg2"] = None so that any import
# chain touching psycopg2 raises ImportError instead of crashing with a
# symbol error. SQLAlchemy then falls back to the psycopg (v3) dialect
# via postgresql+psycopg://.
sys.modules["psycopg2"] = None  # type: ignore[assignment]

import uvicorn

from apps.server.config.logging_config import get_logger, setup_logging
from apps.server.config.settings import get_config

setup_logging()
logger = get_logger(__name__)

# The app factory builds the FastAPI app with full lifespan, middleware, routes,
# and service wiring. main.py re-exports it as the entrypoint.
from apps.server.app_factory import app  # noqa: E402

logger.info("Using app_factory app with all webhook handlers")


def main() -> None:
    """Run PineTunnel server using configured settings."""
    settings = get_config()
    workers = int(os.environ.get("RENDER_WEB_CONCURRENCY", settings.server.workers))
    uvicorn.run(
        "apps.server.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        workers=workers,
        log_level=settings.logging.level.lower(),
        timeout_keep_alive=75,
        timeout_graceful_shutdown=30,
        ws_ping_interval=15.0,
        ws_ping_timeout=30.0,
    )


if __name__ == "__main__":
    main()
