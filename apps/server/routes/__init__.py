"""Route modules extracted from app_factory.py.

Each module provides an ``APIRouter`` instance. The main app includes
all routers from this package during startup.
"""

from .admin import router as admin_router
from .auth import router as auth_router
from .diagnostics import router as diagnostics_router
from .ea_download import router as ea_download_router
from .ea_versions import router as ea_versions_router
from .health import router as health_router
from .metrics import router as metrics_router
from .replay import router as replay_router
from .signals import router as signals_router
from .webhook import router as webhook_router
from .websocket import router as websocket_router
from .ws_telemetry import router as ws_telemetry_router

__all__ = [
    "health_router",
    "auth_router",
    "webhook_router",
    "signals_router",
    "admin_router",
    "ea_versions_router",
    "websocket_router",
    "ws_telemetry_router",
    "ea_download_router",
    "metrics_router",
    "diagnostics_router",
    "replay_router",
]

routers = [
    health_router,
    auth_router,
    webhook_router,
    signals_router,
    admin_router,
    ea_versions_router,
    websocket_router,
    ws_telemetry_router,
    ea_download_router,
    metrics_router,
    diagnostics_router,
    replay_router,
]
