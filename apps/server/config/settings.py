"""
PineTunnel Server Configuration
Production-grade configuration management using Pydantic Settings
Version: 1.0.0
"""

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_MIN_SECRET_LENGTH = 32

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _default_data_dir() -> str:
    """Return default writable data directory for local/dev and managed deployments."""
    if Path("/data").exists():
        return "/data"
    return str(Path.cwd())


class DatabaseSettings(BaseSettings):
    """Database connection configuration."""

    model_config = SettingsConfigDict(env_prefix="DB_", extra="ignore")

    url: str | None = Field(
        default=None,
        description="PostgreSQL connection URL (required for production). Set via DATABASE_URL env var.",
        validation_alias=AliasChoices("DB_URL", "DATABASE_URL"),
    )
    pool_size: int = Field(default=25, ge=1, le=100, description="Connection pool size")
    max_overflow: int = Field(default=50, ge=0, le=200, description="Max pool overflow")
    pool_timeout: int = Field(default=30, ge=5, le=300, description="Pool timeout in seconds")
    pool_recycle: int = Field(
        default=3600, ge=300, le=7200, description="Connection recycle time in seconds"
    )


class LoggingSettings(BaseSettings):
    """Logging configuration."""

    model_config = SettingsConfigDict(env_prefix="LOG_", extra="ignore")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    format: Literal["json", "text"] = Field(default="json", description="Log format")
    max_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024 * 1024,
        le=100 * 1024 * 1024,
        description="Max log file size",
    )
    backup_count: int = Field(default=5, ge=1, le=50, description="Number of backup files")
    log_dir: Path = Field(default=Path("logs"), description="Log directory")
    correlation_id_header: str = Field(
        default="X-Correlation-ID", description="Header for correlation ID"
    )


class MT5Settings(BaseSettings):
    """MetaTrader 5 configuration."""

    model_config = SettingsConfigDict(env_prefix="MT5_", extra="ignore")

    path: str | None = Field(default=None, description="MT5 installation path")
    login: int | None = Field(default=None, description="MT5 account login")
    password: str | None = Field(default=None, description="MT5 account password", repr=False)
    server: str | None = Field(default=None, description="MT5 broker server")
    magic_number: int = Field(default=234000, ge=0, description="Default magic number")
    deviation: int = Field(default=20, ge=0, description="Maximum price deviation in points")
    timeout: int = Field(default=30, ge=5, le=120, description="MT5 operation timeout")


class ServerSettings(BaseSettings):
    """Server configuration."""

    model_config = SettingsConfigDict(env_prefix="SERVER_", extra="ignore")

    host: str = Field(
        default="127.0.0.1",
        description="Server host",
        validation_alias=AliasChoices("SERVER_HOST", "HOST"),
    )
    port: int = Field(
        default=8000,
        ge=1024,
        le=65535,
        description="Server port",
        validation_alias=AliasChoices("SERVER_PORT", "PORT"),
    )
    workers: int = Field(
        default=1,
        ge=1,
        le=8,
        description="Number of worker processes",
        validation_alias=AliasChoices("SERVER_WORKERS", "WORKERS"),
    )
    reload: bool = Field(
        default=False,
        description="Auto-reload on code changes",
        validation_alias=AliasChoices("SERVER_RELOAD", "DEV_AUTO_RELOAD"),
    )
    max_request_size: int = Field(
        default=1 * 1024 * 1024,
        ge=100 * 1024,
        le=10 * 1024 * 1024,
        description="Max request size in bytes",
    )
    request_timeout: int = Field(default=30, ge=5, le=120, description="Request timeout in seconds")
    cors_origins: str = Field(
        default="",
        description="Comma-separated CORS origins",
    )
    base_url: str = Field(
        default="http://127.0.0.1:8000",
        description="Public base URL for download link generation (e.g. https://your-server.com)",
        validation_alias=AliasChoices("SERVER_BASE_URL", "BASE_URL"),
    )
    _cached_cors_origins: list[str] | None = None

    @property
    def parsed_cors_origins(self) -> list[str]:
        """Parse comma-separated CORS origins into a list (cached on first call)."""
        if self._cached_cors_origins is None:
            if not self.cors_origins:
                self._cached_cors_origins = []
            else:
                self._cached_cors_origins = [
                    origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
                ]
        return self._cached_cors_origins


class RateLimitSettings(BaseSettings):
    """Rate limiting configuration.

    IMPORTANT: Webhook endpoints receive traffic from TradingView's shared
    server IPs. With 1000+ clients each running portfolios of signals, the
    per-IP rate must be very high. Do NOT lower these defaults - see the
    "No Cloudflare/CDN Rate Limiting on Webhook Paths" policy in the security policy.
    """

    model_config = SettingsConfigDict(env_prefix="RATE_LIMIT_", extra="ignore")

    requests_per_minute: int = Field(
        default=1000,
        ge=10,
        le=100000,
        validation_alias=AliasChoices("RATE_LIMIT_REQUESTS_PER_MINUTE", "RATE_LIMIT_PER_MINUTE"),
    )
    requests_per_hour: int = Field(
        default=100000,
        ge=100,
        le=1000000,
        validation_alias=AliasChoices("RATE_LIMIT_REQUESTS_PER_HOUR", "RATE_LIMIT_PER_HOUR"),
    )
    webhook_requests_per_minute: int = Field(
        default=10000,
        ge=10,
        le=100000,
        validation_alias=AliasChoices(
            "RATE_LIMIT_WEBHOOK_REQUESTS_PER_MINUTE",
            "RATE_LIMIT_WEBHOOK_PER_MINUTE",
        ),
    )


class TelegramSettings(BaseSettings):
    """Telegram bot configuration."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", extra="ignore")

    bot_token: str = Field(
        default="",
        description="Telegram bot token from @BotFather",
        repr=False,
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN"),
    )
    admin_ids: str = Field(
        default="",
        description="Comma-separated Telegram admin user IDs",
        validation_alias=AliasChoices("TELEGRAM_ADMIN_IDS"),
    )
    _cached_admin_ids: list[int] | None = None

    @property
    def parsed_admin_ids(self) -> list[int]:
        """Parse comma-separated admin IDs into a list of integers (cached on first call)."""
        if self._cached_admin_ids is None:
            self._cached_admin_ids = [
                int(id_str.strip())
                for id_str in self.admin_ids.split(",")
                if id_str.strip().isdigit()
            ]
        return self._cached_admin_ids

    @property
    def is_configured(self) -> bool:
        """Check if both bot token and admin IDs are present."""
        return bool(self.bot_token and self.parsed_admin_ids)

    test_env: bool = Field(
        default=False,
        description="Use Telegram test environment",
        validation_alias=AliasChoices("TELEGRAM_TEST_ENV"),
    )


class Settings(BaseSettings):
    """Main application settings combining all sub-settings for centralized configuration."""

    model_config = SettingsConfigDict(
        # Only read .env in development. In production (Render/Vercel),
        # env vars are set via Render dashboard/API
        # and would produce garbage if read as text.
        env_file=(
            ".env"
            if os.environ.get("ENVIRONMENT", os.environ.get("APP_ENV", "")).lower()
            not in ("production", "staging")
            else None
        ),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Deployment environment",
        validation_alias=AliasChoices("ENVIRONMENT", "APP_ENV"),
    )
    debug: bool = Field(
        default=False,
        description="Debug mode",
        validation_alias=AliasChoices("DEBUG", "APP_DEBUG"),
    )
    version: str = Field(
        default="7.0.0",
        description="Application version",
        validation_alias=AliasChoices("VERSION", "APP_VERSION"),
    )
    data_dir: str = Field(
        default_factory=_default_data_dir,
        description="Persistent application data directory",
        validation_alias=AliasChoices("DATA_DIR", "SERVER_DATA_DIR"),
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis connection URL for sessions, caching, and rate limiting",
        validation_alias=AliasChoices("REDIS_URL", "REDIS_CONNECTION_STRING"),
    )
    enable_license_sync_endpoints: bool = Field(
        default=False,
        description="Enable temporary license sync management endpoints (disabled by default).",
        validation_alias=AliasChoices("ENABLE_LICENSE_SYNC_ENDPOINTS"),
    )

    # Security settings (flattened for environment variables)
    jwt_secret: str = Field(
        default="",
        description="JWT signing secret (REQUIRED in production)",
        repr=False,
        validation_alias=AliasChoices("JWT_SECRET", "JWT_SECRET_KEY"),
    )
    jwt_algorithm: str = Field(
        default="HS256",
        description="JWT algorithm",
        validation_alias=AliasChoices("JWT_ALGORITHM", "SECURITY_JWT_ALGORITHM"),
    )
    jwt_expiry_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="JWT token expiry",
        validation_alias=AliasChoices("JWT_EXPIRY_DAYS", "JWT_EXPIRATION_DAYS"),
    )
    webhook_secret: str = Field(
        default="",
        description="Webhook validation secret (REQUIRED in production)",
        repr=False,
        validation_alias=AliasChoices("WEBHOOK_SECRET", "WEBHOOK_SECRET_KEY"),
    )
    require_trade_report_secret: bool = Field(
        default=False,
        description=(
            "Require secret_key on /api/trades/report|close|stats (fail-closed when missing). "
            "Default False for backward compat with EAs < v1.2 that do not send secret_key."
        ),
        validation_alias=AliasChoices("REQUIRE_TRADE_REPORT_SECRET"),
    )
    admin_api_key: str = Field(
        default="",
        description="Admin API key for admin endpoints (REQUIRED in production, min 32 chars)",
        repr=False,
        validation_alias=AliasChoices("ADMIN_API_KEY"),
    )
    proxy_secret: str = Field(
        default="",
        description=(
            "Shared secret between Vercel proxy and backend. "
            "When set, /api/me requires X-Proxy-Secret header to match. "
            "Prevents direct-to-backend IDOR attacks."
        ),
        repr=False,
        validation_alias=AliasChoices("PROXY_SECRET"),
    )
    admin_api_key_previous: str = Field(
        default="",
        description="Previous admin API key - accepted during key rotation (empty after rotation complete)",
        repr=False,
        validation_alias=AliasChoices("ADMIN_API_KEY_PREVIOUS"),
    )

    # Render platform environment variables (auto-set by Render)
    is_render: bool = Field(
        default=False,
        description="Auto-detected via RENDER env var (Render sets RENDER=true)",
        validation_alias=AliasChoices("RENDER"),
    )
    render_service_id: str = Field(
        default="",
        description="Render service identifier (auto-set by Render)",
        validation_alias=AliasChoices("RENDER_SERVICE_ID"),
    )
    render_external_url: str = Field(
        default="",
        description="Full server URL (auto-set by cloud platforms)",
        validation_alias=AliasChoices("RENDER_EXTERNAL_URL"),
    )
    render_instance_id: str = Field(
        default="",
        description="Unique instance identifier for scaled services (auto-set by Render)",
        validation_alias=AliasChoices("RENDER_INSTANCE_ID"),
    )
    render_web_concurrency: int = Field(
        default=1,
        ge=1,
        le=16,
        description="Recommended worker count based on instance type (auto-set by Render)",
        validation_alias=AliasChoices("RENDER_WEB_CONCURRENCY"),
    )

    # Sub-settings - cached on first access to avoid per-request Pydantic instantiation
    _database: DatabaseSettings | None = None
    _logging: LoggingSettings | None = None
    _mt5: MT5Settings | None = None
    _server: ServerSettings | None = None
    _rate_limit: RateLimitSettings | None = None
    _telegram: TelegramSettings | None = None

    @property
    def database(self) -> DatabaseSettings:
        if self._database is None:
            self._database = DatabaseSettings()
        return self._database

    @property
    def logging(self) -> LoggingSettings:
        if self._logging is None:
            self._logging = LoggingSettings()
        return self._logging

    @property
    def mt5(self) -> MT5Settings:
        if self._mt5 is None:
            self._mt5 = MT5Settings()
        return self._mt5

    @property
    def server(self) -> ServerSettings:
        if self._server is None:
            self._server = ServerSettings()
        return self._server

    @property
    def rate_limit(self) -> RateLimitSettings:
        if self._rate_limit is None:
            self._rate_limit = RateLimitSettings()
        return self._rate_limit

    @property
    def telegram(self) -> TelegramSettings:
        if self._telegram is None:
            self._telegram = TelegramSettings()
        return self._telegram

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def get_log_level(self) -> int:
        """Map the logging level string to a Python logging constant."""
        return _LOG_LEVELS.get(self.logging.level, logging.INFO)


@lru_cache()
def get_settings() -> Settings:
    """Create and cache a Settings instance from environment variables.

    Cached via @lru_cache - returns the same instance on every call.
    Use get_config() for the validated singleton used at runtime;
    use get_settings() when you need an independent validation pass.
    """
    try:
        settings = Settings()
        logger.info("Settings loaded for environment: %s", settings.environment)
        return settings
    except ValidationError as e:
        logger.error("Configuration validation failed: %s", e)
        raise RuntimeError(f"Invalid configuration: {e}") from e


def validate_settings() -> Settings:
    """Validate all settings and fail-fast if production requirements are missing."""
    settings = get_settings()

    logger.info("=" * 60)
    logger.info("Configuration Summary")
    logger.info("=" * 60)
    logger.info("Environment: %s", settings.environment)
    logger.info("Debug: %s", settings.debug)
    logger.info("Version: %s", settings.version)
    logger.info("Server: %s:%s", settings.server.host, settings.server.port)
    logger.info(
        "Database: %s",
        "PostgreSQL" if settings.database.url else "SQLite (fallback)",
    )
    logger.info("Data Dir: %s", settings.data_dir)
    logger.info(
        "License Sync Endpoints: %s",
        "enabled" if settings.enable_license_sync_endpoints else "disabled",
    )
    logger.info("Log Level: %s", settings.logging.level)
    logger.info("Log Format: %s", settings.logging.format)
    logger.info("=" * 60)

    if settings.is_production:
        if settings.debug:
            raise RuntimeError("Debug mode cannot be enabled in production")
        if settings.server.reload:
            raise RuntimeError("Auto-reload cannot be enabled in production")
        if not settings.jwt_secret or len(settings.jwt_secret) < _MIN_SECRET_LENGTH:
            raise RuntimeError(
                f"JWT_SECRET must be set to at least {_MIN_SECRET_LENGTH} characters in production. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if not settings.webhook_secret:
            raise RuntimeError(
                "WEBHOOK_SECRET must be set in production. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if not settings.admin_api_key or len(settings.admin_api_key) < _MIN_SECRET_LENGTH:
            raise RuntimeError(
                f"ADMIN_API_KEY must be set to at least {_MIN_SECRET_LENGTH} characters in production. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
    else:
        if not settings.jwt_secret:
            settings.jwt_secret = secrets.token_urlsafe(48)
            logger.warning("JWT_SECRET not set - generated random key for this session")
        if not settings.webhook_secret:
            settings.webhook_secret = secrets.token_urlsafe(48)
            logger.warning("WEBHOOK_SECRET not set - generated random key for this session")

    return settings


_settings: Settings | None = None


def get_config() -> Settings:
    """Get the global singleton Settings instance.

    Creates and validates the singleton on first call, then caches it.
    Always use get_config() instead of get_settings() at runtime.
    """
    global _settings
    if _settings is None:
        _settings = validate_settings()
    return _settings
