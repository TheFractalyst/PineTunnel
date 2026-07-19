"""
PineTunnel Server -- Application lifespan, service initialization, and helpers.

Extracted from app_factory.py to separate lifecycle management from
route wiring and app creation.
"""

import asyncio
import json
import os
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from dotenv import load_dotenv
from fastapi import FastAPI

# Load .env BEFORE any project imports that read env vars
# Skip in production -- .env may not be available (env vars set by OS)
_env = os.environ.get("ENVIRONMENT", os.environ.get("APP_ENV", "")).lower()
_is_render = os.environ.get("RENDER", "").lower() == "true"
if not _is_render and _env not in ("production", "staging"):
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from apps.server.config.logging_config import get_logger, setup_logging
from apps.server.config.settings import get_config
from apps.server.services.admin_logger import AdminLogger
from apps.server.services.auth_manager import AuthManager
from apps.server.services.client_manager import ClientManager
from apps.server.services.rate_limiter import RateLimiter
from apps.server.services.risk_manager import RiskManager
from apps.server.ws.connection import ConnectionManager

try:
    from apps.server.middleware.main import failed_attempt_tracker
except ImportError:
    failed_attempt_tracker = None

# Shared application state
from apps.server import state
from apps.server.config.reliability import ReliabilityMonitor
from apps.server.utils import mask_string as _mask
from apps.server.utils.auth import create_auth_dependency
from apps.server.webhook.pipeline import init_pipeline as init_webhook_pipeline
from apps.server.webhook.pipeline import init_ws_push as init_webhook_ws_push
from apps.server.webhook.signal_queue import init_signal_queue as _init_signal_queue
from apps.server.webhook.signal_queue import queue_signal_async
from apps.server.ws.handler import (
    WS_CLOSE_SERVER_SHUTDOWN,
    WebSocketConnectionManager,
    broadcast_signal_to_websocket,
    publish_signal_to_redis,
    start_redis_ws_subscriber,
)

# Setup logging
setup_logging()
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
settings = get_config()

_is_production = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "")).lower() == "production"

# Original config_path was relative to apps/server/app_factory.py;
# lifespan.py lives in apps/server/config/ so we point to the same core/ dir.
_core_dir = Path(__file__).resolve().parents[1] / "core"
config_path = _core_dir / "config" / "config.json"
if not config_path.exists():
    config_path = _core_dir / "config.json"

CONFIG: dict = {}
if not config_path.exists():
    logger.warning("config.json not found! Using default configuration")
    CONFIG = {
        "magic_number": settings.mt5.magic_number,
        "deviation": settings.mt5.deviation,
        "host": settings.server.host,
        "port": settings.server.port,
        "debug": settings.debug,
        "max_concurrent_positions": 20,
        "max_daily_trades": 50,
        "max_risk_per_trade": 2.0,
        "max_daily_loss": 5.0,
    }
else:
    with open(config_path, "r") as f:
        CONFIG = json.load(f)
    logger.info("Loaded config from: %s", config_path)

# ---------------------------------------------------------------------------
# Service singletons -- initialized lazily by _init_services() during lifespan.
# Set to None at module level so that type checkers and import-time references
# do not raise NameError.  Uvicorn prefork workers will inherit these as None
# and then _init_services() builds real instances in each worker after fork.
# ---------------------------------------------------------------------------
db_manager = None
client_manager = None
conn_manager = None
rate_limiter = None
risk_manager = None
auth_manager = None
_require_auth = None
admin_logger = None
mt5_manager = None
ws_manager = None
_redis_client = None
_redis_url = None
telegram_bot = None
http_polling_clients: dict = {}
signal_queues: dict = {}
_ws_subscriber_task = None
_bot_watchdog_task = None

# Telegram config (read at module level since it is configuration, not a service)
_tg_token = ""
_tg_admin_ids: list = []
try:
    from apps.server.services.telegram import PineTunnelTelegramBot  # noqa: F401

    _tg_token = settings.telegram.bot_token
    _tg_admin_ids = settings.telegram.parsed_admin_ids

    if settings.telegram.is_configured:
        logger.info(
            "Telegram bot configured with %s admin(s) - will start in lifespan", len(_tg_admin_ids)
        )
    else:
        if not _tg_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set - Telegram bot disabled")
        if not _tg_admin_ids:
            logger.warning("TELEGRAM_ADMIN_IDS not set - Telegram bot disabled")
except ImportError as _tg_err:
    _tg_token = ""
    _tg_admin_ids = []
    logger.warning("Telegram bot module not available: %s", _tg_err)

# ---------------------------------------------------------------------------
# PineTunnel webhook (optional) -- import router at module level so it can be
# included in the FastAPI app, but defer set_pinetunnel_deps() to _init_services.
# ---------------------------------------------------------------------------
PINETUNNEL_AVAILABLE = False
pinetunnel_router = None
set_pinetunnel_deps = None

try:
    from apps.server.webhook.pinetunnel_webhook import router as pinetunnel_router
    from apps.server.webhook.pinetunnel_webhook import set_dependencies as set_pinetunnel_deps

    PINETUNNEL_AVAILABLE = True
except Exception as e:
    logger.warning("PineTunnel webhook not available: %s", e)


# ---------------------------------------------------------------------------
# Signal queue helpers
# ---------------------------------------------------------------------------


async def _notify_signal_queue(license_key: str, signal_data: dict | None = None) -> None:
    if hasattr(conn_manager, "publish_signal"):
        try:
            await conn_manager.publish_signal(license_key, signal_data)
            return
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
    conn_manager.notify_signal_queue(license_key, signal_data)


async def _ws_push_signal(license_key: str, signal_data: dict) -> int:
    # Always push locally first for instant delivery to this worker's WS connections.
    # Then publish to Redis so other workers' subscribers can fan out to their connections.
    # Previously: when Redis was available, local push was skipped (bug) -- local EAs
    # waited for Redis subscriber loopback instead of getting instant delivery.
    local_sent = await broadcast_signal_to_websocket(ws_manager, license_key, signal_data)
    if _redis_client is not None:
        try:
            await publish_signal_to_redis(_redis_client, license_key, signal_data)
        except Exception as e:
            logger.warning("WS Redis publish failed for %s: %s", _mask(license_key), e)
    return local_sent


# ---------------------------------------------------------------------------
# Service initialization
# ---------------------------------------------------------------------------


def _init_services() -> None:
    """Create all service singletons and wire shared state.

    Called at the start of ``lifespan()`` -- AFTER the uvicorn master has
    forked workers -- so that PostgreSQL connection pools, file handles, and
    other per-process resources are created in each worker independently.
    """
    global db_manager, client_manager, conn_manager, rate_limiter, risk_manager
    global auth_manager, _require_auth, admin_logger, mt5_manager, ws_manager
    global _redis_client, _redis_url, telegram_bot
    global http_polling_clients, signal_queues, _ws_subscriber_task
    global _bot_watchdog_task

    data_dir = settings.data_dir
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    # --- Database ---
    _database_url = settings.database.url
    if _database_url and _database_url.startswith("postgresql"):
        from apps.server.db.postgres import create_database_manager

        db_manager = create_database_manager(
            _database_url,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_timeout=settings.database.pool_timeout,
            pool_recycle=settings.database.pool_recycle,
        )
        logger.info("Using PostgreSQL database: %s", _database_url.split("@")[-1])
    else:
        from apps.server.db.sqlite import create_database_manager as _sqlite_factory

        db_manager = _sqlite_factory(data_dir=data_dir)
        logger.info("Using SQLite database: %s/pinetunnel.db", data_dir)

    # --- Redis URL (connection established later in lifespan) ---
    _redis_url = settings.redis_url
    _redis_client = None

    # --- Risk & rate limiting ---
    risk_manager = RiskManager(CONFIG)
    rate_limiter = RateLimiter(
        max_requests_per_minute=CONFIG.get("rate_limit_per_minute", 10000),
        max_requests_per_hour=CONFIG.get("rate_limit_per_hour", 500000),
    )

    # --- Licenses (JSON-backed with automatic migration) ---
    persistent_license_path = Path(data_dir) / "licenses.json"
    legacy_license_path = str(persistent_license_path) if persistent_license_path.exists() else None
    client_manager = ClientManager(license_file=legacy_license_path, data_dir=data_dir)
    logger.info("Loaded %s licenses", len(client_manager.clients))

    # --- Auth manager (session store wired in during lifespan if Redis available) ---
    auth_data_path = Path(data_dir) / "auth_config.json"
    auth_core_path = _core_dir / "config" / "auth_config.json"
    persistent_auth_path = (
        auth_data_path
        if auth_data_path.exists()
        else (auth_core_path if auth_core_path.exists() else auth_data_path)
    )

    auth_config_env = os.environ.get("AUTH_CONFIG_JSON")
    if auth_config_env and not persistent_auth_path.exists():
        try:
            config_data = json.loads(auth_config_env)
            persistent_auth_path.parent.mkdir(parents=True, exist_ok=True)
            with open(persistent_auth_path, "w") as f:
                json.dump(config_data, f, indent=2)
            logger.info("Created auth config from AUTH_CONFIG_JSON environment variable")
        except (json.JSONDecodeError, OSError, PermissionError) as e:
            logger.error(
                "Failed to create auth config from AUTH_CONFIG_JSON: %s: %s", type(e).__name__, e
            )

    auth_manager = AuthManager(str(persistent_auth_path))
    _require_auth = create_auth_dependency(auth_manager)
    admin_logger = AdminLogger(str(Path(data_dir) / "admin_activity.db"))

    # --- MT5 manager ---
    from apps.server.services.mt5_service import MT5_AVAILABLE, MT5Manager

    mt5_manager = MT5Manager(CONFIG)

    # --- Connection manager & WebSocket ---
    conn_manager = ConnectionManager()
    ws_manager = WebSocketConnectionManager()
    _ws_subscriber_task = None
    _bot_watchdog_task = None

    # Backward-compatible aliases
    http_polling_clients = conn_manager.http_polling_clients
    signal_queues = conn_manager.signal_queues

    # Telegram Bot instance (created here; lifespan starts background task)
    telegram_bot = None

    # --- PineTunnel webhook dependency injection ---
    if PINETUNNEL_AVAILABLE and set_pinetunnel_deps:
        set_pinetunnel_deps(mt5_manager, client_manager, risk_manager, rate_limiter, db_manager)

    # --- Signal queue & webhook pipeline wiring ---
    _init_signal_queue(db_manager=db_manager, notify_signal_queue_fn=_notify_signal_queue)
    init_webhook_pipeline(queue_signal_fn=queue_signal_async)
    init_webhook_ws_push(ws_push_fn=_ws_push_signal)

    # --- Publish shared state for route modules ---
    state.db_manager = db_manager
    state.client_manager = client_manager
    state.mt5_manager = mt5_manager
    state.risk_manager = risk_manager
    state.rate_limiter = rate_limiter
    state.auth_manager = auth_manager
    state.admin_logger = admin_logger
    state.telegram_bot = telegram_bot
    state.redis_client = _redis_client
    state._redis_client = _redis_client
    state.settings = settings
    state.conn_manager = conn_manager
    state.http_polling_clients = http_polling_clients
    state.signal_queues = signal_queues
    state.ws_manager = ws_manager
    state.MT5_AVAILABLE = MT5_AVAILABLE
    state.PINETUNNEL_AVAILABLE = PINETUNNEL_AVAILABLE
    state._require_auth_dependency = _require_auth

    logger.info("Service singletons initialized")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global _redis_client, telegram_bot, _ws_subscriber_task, _bot_watchdog_task

    # Fail-fast: validate required env vars BEFORE initializing services.
    # This prevents the server from silently starting with missing secrets.
    from apps.server.config.startup_check import assert_startup_ok

    assert_startup_ok()

    # Initialize all service singletons (DB pools, file handles, etc.)
    # inside each worker -- must happen AFTER uvicorn fork.
    _init_services()

    data_dir = settings.data_dir

    logger.info("=" * 70)
    logger.info("Starting PineTunnel Webhook Server v2.0")
    logger.info("=" * 70)
    if _is_render:
        logger.info(
            "Platform: Render (service=%s type=%s instance=%s)",
            os.environ.get("RENDER_SERVICE_ID", "?"),
            os.environ.get("RENDER_SERVICE_TYPE", "web"),
            os.environ.get("RENDER_INSTANCE_ID", "?"),
        )
        logger.info(
            "Git: %s@%s",
            os.environ.get("RENDER_GIT_BRANCH", "?"),
            os.environ.get("RENDER_GIT_COMMIT", "?")[:12],
        )
    logger.info("Database: %s", "PostgreSQL" if settings.database.url and settings.database.url.startswith("postgresql") else "SQLite")
    logger.info("Persistent storage: %s", "YES" if data_dir == "/data" else "NO (local only)")

    redis_client = None

    # --- Redis wiring ---
    if _redis_url:
        try:
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(
                _redis_url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            await redis_client.ping()
            _redis_client = redis_client
            state.redis_client = redis_client
            state._redis_client = redis_client
            logger.info(
                "Redis connected: %s",
                _redis_url.split("@")[-1] if "@" in _redis_url else _redis_url,
            )

            from apps.server.db.session_store import RedisSessionStore

            _redis_session_store = RedisSessionStore(redis_client)
            auth_manager._session_store = _redis_session_store
            logger.info("Redis session store wired into auth manager")

            try:
                from apps.server.middleware.main import set_redis_rate_limiter
                from apps.server.services.rate_limiter_redis import RedisRateLimiter

                _redis_rate_limiter = RedisRateLimiter(redis_client)
                set_redis_rate_limiter(_redis_rate_limiter)
                logger.info("Redis rate limiter wired into middleware")
            except Exception as e:
                logger.warning("Redis rate limiter wiring failed: %s", e)

            if failed_attempt_tracker:
                try:
                    failed_attempt_tracker.set_redis(redis_client)
                    logger.info("Redis failed-attempt tracker wired")
                except Exception as e:
                    logger.warning("Redis failed-attempt tracker wiring failed: %s", e)

            try:
                from apps.server.ws.connection_redis import RedisConnectionManager

                redis_conn_manager = RedisConnectionManager(redis_client)
                global conn_manager
                old_polling = conn_manager.http_polling_clients
                old_queues = conn_manager.signal_queues
                conn_manager = redis_conn_manager
                conn_manager.http_polling_clients.update(old_polling)
                conn_manager.signal_queues.update(old_queues)
                global http_polling_clients, signal_queues
                http_polling_clients = conn_manager.http_polling_clients
                signal_queues = conn_manager.signal_queues
                state.conn_manager = conn_manager
                state.http_polling_clients = http_polling_clients
                state.signal_queues = signal_queues
                logger.info("Redis connection manager wired (multi-worker)")
            except Exception as e:
                logger.warning("Redis connection manager wiring failed: %s", e)

            try:
                _ws_subscriber_task = await start_redis_ws_subscriber(ws_manager, redis_client)
                if _ws_subscriber_task:
                    logger.info("WebSocket Redis subscriber started")
                state._ws_subscriber_task = _ws_subscriber_task
            except Exception as e:
                logger.warning("WebSocket Redis subscriber wiring failed: %s", e)

        except Exception as e:
            logger.warning("Redis connection failed, using in-memory fallbacks: %s", e)
            redis_client = None
    else:
        logger.warning(
            "REDIS_URL not configured - rate limiting, session management, "
            "and state replication will use per-process in-memory fallbacks."
        )

    # --- Database ---
    if db_manager is None:
        raise RuntimeError("Database manager not initialized - this should not happen")
    db_manager.init_database()
    logger.info("Database initialized")

    # --- Trade analytics Redis wiring (fire-and-forget, non-blocking) ---
    if redis_client:
        try:
            from apps.server.db.analytics_store import (
                account_stats_latest,
                license_stats,
                set_redis_client,
                warm_account_stats,
            )

            set_redis_client(redis_client)

            async def _warm_account_stats():
                try:
                    redis_count = await account_stats_latest.warm_from_redis()
                    if redis_count:
                        logger.info(
                            "Warmed account stats for %s license(s) from Redis", redis_count
                        )
                    else:
                        warmed = warm_account_stats()
                        if warmed:
                            logger.info("Warmed account stats for %s license(s) from DB", warmed)
                except Exception:
                    warmed = warm_account_stats()
                    if warmed:
                        logger.info("Warmed account stats for %s license(s) from DB", warmed)

            async def _warm_license_stats():
                try:
                    redis_count = await license_stats.warm_from_redis()
                    if redis_count:
                        logger.info(
                            "Warmed license stats for %s license(s) from Redis", redis_count
                        )
                except Exception as e:
                    logger.debug("Unexpected error: %s", e)

            asyncio.create_task(_warm_account_stats())
            asyncio.create_task(_warm_license_stats())
            logger.info("Trade analytics warming scheduled (non-blocking)")
        except Exception as e:
            logger.warning("Trade analytics Redis wiring failed: %s", e)
    else:
        try:
            from apps.server.db.analytics_store import warm_account_stats

            warmed = warm_account_stats()
            if warmed:
                logger.info("Warmed account stats for %s license(s) from DB", warmed)
        except Exception as e:
            logger.warning("Account stats warm-up failed: %s", e)

    # --- Count pending signals ---
    logger.info("Checking for pending signals...")
    try:
        rows = db_manager.execute_query(
            "SELECT COUNT(*) as count FROM signal_queue WHERE status = 'pending'"
        )
        pending_count = rows[0]["count"] if rows else 0
        if pending_count > 0:
            logger.info("Found %s pending signals from previous session", pending_count)
    except Exception as e:
        logger.warning("Could not count pending signals: %s", e)

    # --- MT5 (non-blocking, run in thread with timeout) ---
    logger.info("Initializing MT5...")
    try:
        loop = asyncio.get_event_loop()
        mt5_ok = await asyncio.wait_for(
            loop.run_in_executor(None, mt5_manager.initialize),
            timeout=5,
        )
        if mt5_ok:
            logger.info("MT5 connected successfully")
        else:
            logger.warning("MT5 running in mock mode (cloud deployment)")
    except asyncio.TimeoutError:
        logger.warning("MT5 init timed out (5s), running in mock mode")
    except Exception as e:
        logger.warning("MT5 init failed: %s", e)

    # --- Background cleanup ---
    async def cleanup_task():
        while True:
            await asyncio.sleep(3600)
            try:
                result = db_manager.cleanup_old_signals(days_to_keep=7, stale_hours=24)
                if result["total"] > 0:
                    logger.info(
                        "Cleanup: Removed %s acknowledged + %s stale pending signals",
                        result["acknowledged"],
                        result["stale_pending"],
                    )
            except Exception as e:
                logger.error("Cleanup task error: %s", e)

            try:
                stale_queues = [key for key in signal_queues if key not in http_polling_clients]
                for key in stale_queues:
                    del signal_queues[key]
                if stale_queues:
                    logger.debug("Cleaned up %s idle signal queue(s)", len(stale_queues))
            except Exception as e:
                logger.error("Queue cleanup error: %s", e)

            try:
                db_manager.cleanup_old_account_stats(days_to_keep=30)
            except Exception as e:
                logger.error("Account stats cleanup error: %s", e)

            try:
                from apps.server.middleware.main import _rate_limit_middleware

                if _rate_limit_middleware is not None:
                    _rate_limit_middleware.cleanup()
                from apps.server.routes.trade_analytics import _prune_stats_alert_cooldowns

                _prune_stats_alert_cooldowns()
                if hasattr(auth_manager, "_session_store") and hasattr(
                    auth_manager._session_store, "cleanup"
                ):
                    auth_manager._session_store.cleanup()
                if failed_attempt_tracker:
                    failed_attempt_tracker.cleanup()
            except Exception as e:
                logger.error("Middleware cleanup error: %s", e)

    cleanup_job = asyncio.create_task(cleanup_task())

    # --- WS stale connection cleanup (frequent) ---
    async def ws_cleanup_task():
        while True:
            await asyncio.sleep(120)
            try:
                ws_manager.cleanup_stale()
            except Exception as e:
                logger.error("WS cleanup error: %s", e)

    ws_cleanup_job = asyncio.create_task(ws_cleanup_task())

    # --- EA connection tracker ---
    _prev_connected_licenses: set = set()
    _disconnect_candidates: set = set()

    async def _ea_connection_tracker():
        nonlocal _prev_connected_licenses, _disconnect_candidates
        await asyncio.sleep(5)
        _prev_connected_licenses = set(http_polling_clients.keys()) | set(
            ws_manager.get_connected_license_keys()
        )
        _disconnect_candidates = set()
        while True:
            await asyncio.sleep(30)
            try:
                currently_connected = set(http_polling_clients.keys()) | set(
                    ws_manager.get_connected_license_keys()
                )
                newly_connected = currently_connected - _prev_connected_licenses
                now_missing = _prev_connected_licenses - currently_connected

                for lic in newly_connected:
                    _disconnect_candidates.discard(lic)
                    # EA connect/disconnect notifications disabled (preserved in events.py for re-enabling)
                    # try:
                    #         asyncio.create_task(telegram_bot.on_ea_connected_user(lic))
                    # except Exception:
                    #     pass
                    pass

                confirmed_disconnected = _disconnect_candidates & now_missing
                _disconnect_candidates = now_missing - confirmed_disconnected

                for lic in confirmed_disconnected:
                    # EA connect/disconnect notifications disabled (preserved in events.py for re-enabling)
                    # try:
                    #         asyncio.create_task(telegram_bot.on_ea_disconnected_user(lic))
                    # except Exception:
                    #     pass
                    pass

                _prev_connected_licenses = currently_connected
            except Exception as e:
                logger.error("EA connection tracker error: %s", e)

    ea_tracker_task = asyncio.create_task(_ea_connection_tracker())

    # Start rate limiter auto-cleanup
    rate_limiter.start_auto_cleanup(interval=120)

    # Periodic signal queue pruning
    async def _prune_signal_queues():
        while True:
            await asyncio.sleep(300)
            try:
                conn_manager.get_active_http_clients()
                conn_manager.prune_idle_signal_queues()
            except Exception as e:
                logger.error("Signal queue pruning error: %s", e)

    prune_task = asyncio.create_task(_prune_signal_queues())

    # --- Telegram Bot ---
    global telegram_bot
    if _tg_token and _tg_admin_ids:
        try:
            telegram_bot = PineTunnelTelegramBot(
                token=_tg_token,
                admin_ids=_tg_admin_ids,
                client_manager=client_manager,
                db_manager=db_manager,
                data_dir=data_dir,
                http_polling_clients=http_polling_clients,
                signal_queues=signal_queues,
                conn_manager=conn_manager,
                ws_manager=ws_manager,
                test_env=settings.telegram.test_env,
                auth_store=getattr(state, "_auth_store", None),
            )
            tg_task = asyncio.create_task(telegram_bot.start())

            def _tg_task_done(t: asyncio.Task):
                if t.cancelled():
                    logger.warning("Telegram bot task was cancelled")
                elif exc := t.exception():
                    logger.error("Telegram bot task failed: %s", exc, exc_info=exc)

            tg_task.add_done_callback(_tg_task_done)
            logger.info("Telegram bot starting in background")
            state.telegram_bot = telegram_bot

            # Watchdog: check every 60s if the updater is actually polling.
            # start_polling() is non-blocking and can silently fail (e.g. 409
            # conflict from a previous instance). updater.running may stay True
            # even when the internal polling loop has died.
            async def _bot_watchdog():
                _restart_attempts = 0
                while True:
                    await asyncio.sleep(60)
                    if not telegram_bot._started or not telegram_bot.app:
                        _restart_attempts += 1
                        # Exponential backoff: 60s, 120s, 240s, ... max 600s
                        delay = min(60 * (2 ** (_restart_attempts - 1)), 600)
                        if _restart_attempts > 1:
                            logger.warning(
                                "Bot not started - watchdog retry #%d (waiting %ds)",
                                _restart_attempts, delay,
                            )
                            await asyncio.sleep(delay - 60)  # already slept 60s
                        else:
                            logger.warning("Bot not started - watchdog restarting")
                        try:
                            await telegram_bot.start()
                            if telegram_bot._started:
                                _restart_attempts = 0
                        except Exception as e:
                            logger.error("Watchdog restart failed: %s", e)
                        continue
                    updater = telegram_bot.app.updater
                    if not updater or not updater.running:
                        logger.warning("Bot updater not running - restarting")
                        try:
                            await telegram_bot.stop()
                        except Exception as e:
                            logger.debug("Unexpected error: %s", e)
                        try:
                            await telegram_bot.start()
                            if telegram_bot._started:
                                _restart_attempts = 0
                                logger.info("Bot restarted by watchdog (updater not running)")
                        except Exception as e:
                            logger.error("Watchdog restart failed: %s", e)
                    else:
                        # Bot is healthy -- reset backoff counter
                        _restart_attempts = 0

            _bot_watchdog_task = asyncio.create_task(_bot_watchdog())
        except Exception as tg_err:
            logger.error("Failed to initialize Telegram bot: %s", tg_err)
            telegram_bot = None

    # --- Reliability monitor ---
    logger.info("Starting reliability monitor...")
    reliability = ReliabilityMonitor(
        data_dir=data_dir,
        client_manager=client_manager,
        conn_manager=conn_manager,
        ws_manager=ws_manager,
        telegram_bot=telegram_bot,
        notify_admin_fn=telegram_bot.notify_admin if telegram_bot else None,
        db_manager=db_manager,
    )
    await reliability.start()
    logger.info("Reliability monitor started")

    # Mark all health components as ready (wires /health/ready endpoint)
    from apps.server.config.health import health_manager
    health_manager.mark_ready()
    logger.info("Health readiness flag set")

    logger.info("Server started successfully")

    _first_run_marker = Path.home() / ".pinetunnel" / "initialized"
    _open_browser = (
        os.getenv("PINETUNNEL_NO_OPEN_BROWSER", "") != "1"
        and not _first_run_marker.exists()
        and os.getenv("RENDER_WEB_CONCURRENCY") is None
        and sys.stdout.isatty()
    )
    if _open_browser:
        port = os.getenv("PORT", "8000")
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/admin/", new=2, autoraise=True)
        except Exception:
            logger.debug("Could not open browser", exc_info=True)
        _first_run_marker.parent.mkdir(parents=True, exist_ok=True)
        _first_run_marker.write_text("1")

    yield

    # --- Shutdown ---
    # Order matters: cancel consumers before closing resources they use.
    logger.info("Shutting down server...")

    # 0. Mark health as not ready so Render stops routing traffic
    from apps.server.config.health import health_manager, COMPONENT_APP
    health_manager.mark_component_not_ready(COMPONENT_APP)

    # 1. Cancel bot watchdog FIRST — it checks _started and tries to restart
    #    a restart race during shutdown.
    if _bot_watchdog_task:
        _bot_watchdog_task.cancel()
        try:
            await _bot_watchdog_task
        except asyncio.CancelledError:
            pass
        logger.info("Bot watchdog stopped")

    # 2. Stop reliability monitor (cancels its internal background task)
    try:
        await reliability.stop()
    except Exception as rel_err:
        logger.error("Error stopping reliability monitor: %s", rel_err)

    # 3. Stop Telegram bot (after watchdog is cancelled)
    if telegram_bot:
        try:
            await telegram_bot.stop()
        except Exception as tg_err:
            logger.error("Error stopping Telegram bot: %s", tg_err)

    # 4. Close connection manager (Redis subscriber, signal queues)
    if hasattr(conn_manager, "close"):
        try:
            await conn_manager.close()
        except Exception as e:
            logger.debug("Unexpected error: %s", e)

    # 5. Cancel WS Redis subscriber task
    if _ws_subscriber_task:
        _ws_subscriber_task.cancel()
        try:
            await _ws_subscriber_task
        except asyncio.CancelledError:
            pass
        logger.info("WebSocket Redis subscriber stopped")

    # 6. Close all WebSocket connections gracefully (send shutdown frame)
    shutdown_msg = json.dumps(
        {"type": "shutdown", "reason": "server_restart"}, separators=(",", ":")
    )
    for license_key in list(ws_manager.get_connected_license_keys()):
        for ws in ws_manager.get_connections_for_key(license_key):
            try:
                await ws.send_text(shutdown_msg)
                await ws.close(code=WS_CLOSE_SERVER_SHUTDOWN, reason="Server shutting down")
            except Exception as e:
                logger.debug("Unexpected error: %s", e)
    if ws_manager.get_total_connections() > 0:
        await asyncio.sleep(0.2)  # Brief pause for EAs to process shutdown
        logger.info(
            "WebSocket connections closed: %d remaining", ws_manager.get_total_connections()
        )

    # 7. Cancel all background asyncio tasks (before closing resources they use)
    _bg_tasks = [cleanup_job, ws_cleanup_job, ea_tracker_task, prune_task]
    for task in _bg_tasks:
        task.cancel()
    results = await asyncio.gather(*_bg_tasks, return_exceptions=True)
    for task, result in zip(_bg_tasks, results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.warning("Background task %s ended with error: %s", task.get_name(), result)
    logger.info("Background tasks cancelled")

    # 8. Stop rate limiter auto-cleanup (thread-based, not asyncio)
    rate_limiter.stop_auto_cleanup()

    # 9. Close Redis (after tasks that might publish are cancelled)
    if redis_client:
        try:
            await redis_client.close()
            _redis_client = None
            state.redis_client = None
            state._redis_client = None
            logger.info("Redis connection closed")
        except Exception as e:
            logger.debug("Unexpected error: %s", e)

    # 10. Dispose database connection pool (SQLAlchemy engine)
    if db_manager:
        try:
            if hasattr(db_manager, "close_async"):
                await db_manager.close_async()
            elif hasattr(db_manager, "close"):
                db_manager.close()
            logger.info("Database connections closed")
        except Exception as e:
            logger.error("Error closing database: %s", e)

    # 11. Shut down MT5 manager
    mt5_manager.shutdown()

    # 12. Close auth manager (session store, etc.)
    try:
        await auth_manager.close()
    except Exception as e:
        logger.debug("Unexpected error: %s", e)

    logger.info("Server stopped")


# ---------------------------------------------------------------------------
# Security self-test
# ---------------------------------------------------------------------------


def _security_self_test(app: FastAPI) -> None:
    """Run startup security checks and log warnings for misconfigurations.

    This is a non-blocking advisory check -- it logs warnings but does not
    prevent the server from starting.
    """
    issues: list[str] = []

    # 1. JWT secret length
    if len(settings.jwt_secret) < 32:
        issues.append(f"JWT_SECRET is only {len(settings.jwt_secret)} chars (recommend 32+)")

    # 2. Admin API key length
    if settings.admin_api_key and len(settings.admin_api_key) < 32:
        issues.append(f"ADMIN_API_KEY is only {len(settings.admin_api_key)} chars (recommend 32+)")

    # 3. Webhook secret
    if settings.webhook_secret and len(settings.webhook_secret) < 32:
        issues.append(
            f"WEBHOOK_SECRET is only {len(settings.webhook_secret)} chars (recommend 32+)"
        )

    # 4. CORS wildcard check
    cors_origins = (
        settings.server.parsed_cors_origins
        if hasattr(settings.server, "parsed_cors_origins")
        else []
    )
    if cors_origins == ["*"]:
        issues.append("SERVER_CORS_ORIGINS is set to wildcard '*' - allows any origin")

    # 5. Production should disable docs
    if _is_production and app.docs_url:
        issues.append("API docs are enabled in production - set DOCS_ENABLED=false")

    # 6. Production requires secrets
    if _is_production:
        if not settings.jwt_secret:
            issues.append("JWT_SECRET is empty in production")
        if not settings.admin_api_key:
            issues.append("ADMIN_API_KEY is empty in production")
        if not settings.webhook_secret:
            issues.append("WEBHOOK_SECRET is empty in production")

    if issues:
        logger.warning("Security self-test found %d issue(s):", len(issues))
        for issue in issues:
            logger.warning("  - %s", issue)
    else:
        logger.info("Security self-test passed - no misconfigurations detected")
