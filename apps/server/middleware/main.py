"""Middleware re-export shim + setup_middleware().

All middleware classes live in their own focused modules:
- security.py: SecurityHeadersMiddleware, FailedAttemptTracker
- rate_limit.py: RateLimitMiddleware, set_redis_rate_limiter
- ip_validation.py: CloudflareIPMiddleware, TradingViewIPMiddleware
- request_validation.py: RequestValidationMiddleware, _ResponseRecorder, _send_json
"""

from typing import Any

from starlette.types import ASGIApp

from apps.server.middleware.ip_validation import CloudflareIPMiddleware, TradingViewIPMiddleware
from apps.server.middleware.rate_limit import RateLimitMiddleware, set_redis_rate_limiter
from apps.server.middleware.request_validation import RequestValidationMiddleware
from apps.server.middleware.security import FailedAttemptTracker, SecurityHeadersMiddleware

# Re-export for backward compat
failed_attempt_tracker = FailedAttemptTracker()

__all__ = [
    "SecurityHeadersMiddleware",
    "RateLimitMiddleware",
    "RequestValidationMiddleware",
    "CloudflareIPMiddleware",
    "TradingViewIPMiddleware",
    "FailedAttemptTracker",
    "failed_attempt_tracker",
    "setup_middleware",
    "set_redis_rate_limiter",
]

# Module-level reference to the rate limit middleware instance
_rate_limit_middleware: RateLimitMiddleware | None = None


def setup_middleware(app: ASGIApp) -> None:
    """Install the production ASGI middleware chain onto the FastAPI app.

    Order (outermost -> innermost): CF IP -> TradingView IP -> security headers
    -> rate limit -> request validation -> (FastAPI's built stack).
    """
    global _rate_limit_middleware

    original_stack = app.build_middleware_stack()

    innermost = RequestValidationMiddleware(original_stack)
    rate_limit = RateLimitMiddleware(innermost)
    security_headers = SecurityHeadersMiddleware(rate_limit)
    tv_ip = TradingViewIPMiddleware(security_headers)
    outermost = CloudflareIPMiddleware(tv_ip)

    app.middleware_stack = outermost
    _rate_limit_middleware = rate_limit


def __getattr__(name: str) -> Any:
    """Dynamic attribute access for _rate_limit_middleware."""
    if name == "_rate_limit_middleware":
        return globals().get("_rate_limit_middleware")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
