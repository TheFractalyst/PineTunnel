"""IP validation middleware - Cloudflare and TradingView IP allowlisting."""

import ipaddress
import logging
import os

from starlette.types import ASGIApp, Receive, Scope, Send

from apps.server.config.logging_config import get_correlation_id, log_security_event
from apps.server.config.settings import get_config
from apps.server.middleware.request_validation import _get_client_ip_from_scope, _send_json

logger = logging.getLogger(__name__)

# --- Constants ---

_HTTP_403 = 403

# Cloudflare IP ranges (IPv4 + IPv6) - updated 2024-06
# https://www.cloudflare.com/ips/
# CANONICAL SOURCE for app-level middleware. .env or server config
# mirrors this for Scale-plan infrastructure-level allowlisting.
_CLOUDFLARE_IPV4 = frozenset(
    {
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
    }
)
_CLOUDFLARE_IPV6 = frozenset(
    {
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    }
)

# TradingView webhook egress IPs (IPv4 only - TV has no IPv6 support)
# Source: TradingView webhook documentation
# Override via TRADINGVIEW_IPS env var (comma-separated)
_TRADINGVIEW_IPS = frozenset(
    {
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    }
)


class CloudflareIPMiddleware:
    """Reject requests from non-Cloudflare IPs in production.

    When CLOUDFLARE_IP_ALLOWLIST is enabled (default in production), this
    middleware rejects any request whose direct connecting IP is not in
    Cloudflare's published IP ranges, preventing direct-to-origin attacks.

    Disabled in development or when the env var is explicitly set to ''.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._enabled: bool | None = None  # lazy-initialized

        self._ipv4_networks = [ipaddress.ip_network(cidr) for cidr in _CLOUDFLARE_IPV4]
        self._ipv6_networks = [ipaddress.ip_network(cidr) for cidr in _CLOUDFLARE_IPV6]

    def _is_enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        env_val = os.environ.get("CLOUDFLARE_IP_ALLOWLIST", "").lower()
        if env_val in ("0", "false", "no", ""):
            # Empty string: auto-detect from environment
            if env_val == "":
                cfg = get_config()
                self._enabled = cfg.environment == "production"
            else:
                self._enabled = False
        else:
            self._enabled = True
        return self._enabled

    def _ip_in_cloudflare(self, ip_str: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        # Localhost and private IPs are allowed (health checks from load balancer)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return True
        networks = self._ipv6_networks if addr.version == 6 else self._ipv4_networks
        return any(addr in net for net in networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._is_enabled():
            await self.app(scope, receive, send)
            return

        # In production, the request path is: Cloudflare -> reverse proxy -> uvicorn.
        # scope["client"] is the reverse proxy IP (not Cloudflare, not private).
        # get_trusted_client_ip() returns CF-Connecting-IP (the original client's
        # IP, e.g. TradingView's AWS IP). Neither is a Cloudflare IP.
        # The correct check: CF-Connecting-IP header presence proves the request
        # came through Cloudflare (Cloudflare sets and overwrites this header).
        # If absent, fall back to checking the direct TCP peer against CF ranges.
        has_cf_header = any(k == b"cf-connecting-ip" for k, _ in scope.get("headers", []))
        if has_cf_header:
            await self.app(scope, receive, send)
            return

        client_ip = scope.get("client", (None, None))[0]
        if client_ip and not self._ip_in_cloudflare(client_ip):
            log_security_event(
                event_type="non_cloudflare_ip",
                description=f"Rejected request from non-Cloudflare IP {client_ip}",
                severity="warning",
                source_ip=client_ip,
            )
            await _send_json(
                send,
                _HTTP_403,
                {
                    "detail": "Forbidden",
                    "correlation_id": get_correlation_id(),
                },
            )
            return

        await self.app(scope, receive, send)


class TradingViewIPMiddleware:
    """Reject webhook requests from non-TradingView IPs.

    Only applies to POST requests on webhook endpoints (/, /webhook,
    /pinetunnel/webhook, /pinetunnel/webhook/simple). Non-webhook paths
    (health checks, EA polling, admin, etc.) are unaffected.

    Uses ``get_trusted_client_ip()`` to identify the real client IP behind
    Cloudflare (via CF-Connecting-IP or X-Forwarded-For).

    Disabled in development or when ``TRADINGVIEW_IP_ALLOWLIST=false``.
    TradingView IPs can be overridden via ``TRADINGVIEW_IPS`` env var
    (comma-separated). Defaults to the 4 known TradingView egress IPs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._enabled: bool | None = None
        self._allowed_ips: frozenset[str] | None = None

    def _is_enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        env_val = os.environ.get("TRADINGVIEW_IP_ALLOWLIST", "").lower()
        if env_val in ("0", "false", "no"):
            self._enabled = False
        elif env_val in ("1", "true", "yes"):
            self._enabled = True
        else:
            cfg = get_config()
            self._enabled = cfg.environment == "production"
        return self._enabled

    def _get_allowed_ips(self) -> frozenset[str]:
        if self._allowed_ips is not None:
            return self._allowed_ips
        env_ips = os.environ.get("TRADINGVIEW_IPS", "")
        if env_ips:
            self._allowed_ips = frozenset(ip.strip() for ip in env_ips.split(",") if ip.strip())
        else:
            self._allowed_ips = _TRADINGVIEW_IPS
        return self._allowed_ips

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._is_enabled():
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        is_webhook = ((path == "/" or path == "/webhook") and method == "POST") or (
            path.startswith("/pinetunnel/webhook") and method == "POST"
        )
        if not is_webhook:
            await self.app(scope, receive, send)
            return

        client_ip = _get_client_ip_from_scope(scope)
        allowed = self._get_allowed_ips()

        if client_ip and client_ip not in allowed:
            log_security_event(
                event_type="non_tradingview_ip",
                description=f"Rejected webhook from non-TradingView IP {client_ip}",
                severity="warning",
                source_ip=client_ip,
            )
            await _send_json(
                send,
                _HTTP_403,
                {
                    "detail": "Forbidden",
                    "correlation_id": get_correlation_id(),
                },
            )
            return

        await self.app(scope, receive, send)
