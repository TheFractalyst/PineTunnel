"""Request/response validation middleware - response recorder, JSON sender, IP helper, and request validation."""

import json
import logging
import time
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from apps.server.config.logging_config import (
    get_correlation_id,
    log_request,
    log_security_event,
    set_correlation_id,
)
from apps.server.config.settings import get_config
from apps.server.utils.security import TRUSTED_PROXY_COUNT, UNKNOWN_IP

logger = logging.getLogger(__name__)

# --- Constants ---

_HTTP_413 = 413
_HTTP_411 = 411
_HTTP_415 = 415
_HTTP_500 = 500


class _ResponseRecorder:
    """Captures response headers and body from ASGI send calls."""

    def __init__(self) -> None:
        self.status_code: int = 200
        self.headers: list[tuple[bytes, bytes]] = []
        self.body_parts: list[bytes] = []
        self.extra_headers: dict[str, str] = {}

    async def __call__(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status_code = message["status"]
            self.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.body_parts.append(message.get("body", b""))

    def patch_headers(self) -> None:
        for key, value in self.extra_headers.items():
            encoded_key = key.encode() if isinstance(key, str) else key
            encoded_value = value.encode() if isinstance(value, str) else value
            self.headers = [(k, v) for k, v in self.headers if k.lower() != encoded_key.lower()]
            self.headers.append((encoded_key, encoded_value))

    async def replay(self, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.headers}
        )
        body = b"".join(self.body_parts)
        await send({"type": "http.response.body", "body": body})


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    """Send a JSON response directly via ASGI."""
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(payload)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _get_client_ip_from_scope(scope: Scope) -> str:
    """Extract client IP from ASGI scope, cached for downstream middleware.

    The IP is extracted once and stored in ``scope["_pt_client_ip"]`` so all
    middleware in the chain reuse the same value without re-parsing headers
    or creating a Starlette ``Request`` object.
    """
    cached = scope.get("_pt_client_ip")
    if cached is not None:
        return cached

    cf_ip = None
    xff = None
    real_ip = None

    for key, val in scope.get("headers", ()):
        if key == b"cf-connecting-ip":
            cf_ip = val.decode().strip()
            break
        if key == b"x-forwarded-for":
            xff = val.decode()
        elif key == b"x-real-ip":
            real_ip = val.decode().strip()

    if cf_ip:
        scope["_pt_client_ip"] = cf_ip
        return cf_ip

    if xff:
        parts = [p.strip() for p in xff.split(",")]
        ip = parts[-(TRUSTED_PROXY_COUNT + 1)] if len(parts) > TRUSTED_PROXY_COUNT else parts[0]
        scope["_pt_client_ip"] = ip
        return ip

    if real_ip:
        scope["_pt_client_ip"] = real_ip
        return real_ip

    client = scope.get("client")
    if client:
        ip = client[0]
        scope["_pt_client_ip"] = ip
        return ip

    scope["_pt_client_ip"] = UNKNOWN_IP
    return UNKNOWN_IP


class RequestValidationMiddleware:
    """Request validation, size checks, content-type validation, and request logging - pure ASGI."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.config = get_config()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cid = self._extract_correlation_id(scope)

        validation_error = self._validate_request(scope)
        if validation_error:
            await _send_json(send, validation_error[0], validation_error[1])
            return

        start_time = time.time()
        recorder = _ResponseRecorder()
        try:
            await self.app(scope, receive, recorder)
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            client_ip = _get_client_ip_from_scope(scope)
            log_request(
                method=scope.get("method", "?"),
                path=scope.get("path", "?"),
                status_code=_HTTP_500,
                duration_ms=duration_ms,
                client_ip=client_ip,
                user_agent=self._get_header(scope, "user-agent"),
                extra={"error_type": type(e).__name__},
            )
            await _send_json(
                send, _HTTP_500, {"detail": "Internal server error", "correlation_id": cid}
            )
            return

        recorder.extra_headers = {self.config.logging.correlation_id_header.lower(): cid}
        recorder.patch_headers()

        duration_ms = (time.time() - start_time) * 1000
        client_ip = _get_client_ip_from_scope(scope)
        log_request(
            method=scope.get("method", "?"),
            path=scope.get("path", "?"),
            status_code=recorder.status_code,
            duration_ms=duration_ms,
            client_ip=client_ip,
            user_agent=self._get_header(scope, "user-agent"),
            extra={"query_params": scope.get("query_string", b"").decode()},
        )

        await recorder.replay(send)

    def _extract_correlation_id(self, scope: Scope) -> str:
        """Extract correlation ID from request headers or generate a new one."""
        corr_header = self.config.logging.correlation_id_header.lower()
        cid = self._get_header(scope, corr_header)
        if cid is not None:
            set_correlation_id(cid)
            return cid
        return get_correlation_id()

    def _validate_request(self, scope: Scope) -> tuple[int, dict[str, Any]] | None:
        """Return (status_code, body_dict) for validation failure, or None if valid."""
        method = scope.get("method", "")
        path = scope.get("path", "")

        if method in ("POST", "PUT", "PATCH"):
            error = self._validate_content_length(scope, path, method)
            if error:
                return error

        if method in ("POST", "PUT"):
            content_type = self._get_header(scope, "content-type", "") or ""
            if "/webhook" in path and not path.endswith("/simple"):
                if not content_type.startswith("application/json"):
                    return (
                        _HTTP_415,
                        {
                            "detail": "Unsupported media type. Use application/json",
                            "correlation_id": get_correlation_id(),
                        },
                    )

        if not self._get_header(scope, "user-agent"):
            client_ip = _get_client_ip_from_scope(scope)
            log_security_event(
                event_type="missing_user_agent",
                description="Request without User-Agent header",
                severity="info",
                source_ip=client_ip,
            )

        return None

    def _validate_content_length(
        self, scope: Scope, path: str, method: str
    ) -> tuple[int, dict[str, Any]] | None:
        """Validate content-length for mutation requests."""
        content_length = self._get_header(scope, "content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                return None

            if size > self.config.server.max_request_size:
                client_ip = _get_client_ip_from_scope(scope)
                log_security_event(
                    event_type="request_too_large",
                    description=(
                        f"Request size {size} exceeds limit {self.config.server.max_request_size}"
                    ),
                    severity="warning",
                    source_ip=client_ip,
                )
                return (
                    _HTTP_413,
                    {
                        "detail": "Request entity too large",
                        "max_size": self.config.server.max_request_size,
                        "correlation_id": get_correlation_id(),
                    },
                )
        else:
            is_api_or_webhook = (
                path.startswith("/api/")
                or path == "/webhook"
                or path == "/"
                or path.startswith("/pinetunnel/")
            )
            if is_api_or_webhook:
                return (
                    _HTTP_411,
                    {
                        "detail": "Content-Length header required",
                        "correlation_id": get_correlation_id(),
                    },
                )

        return None

    @staticmethod
    def _get_header(scope: Scope, name: str, default: str | None = None) -> str | None:
        """Look up a request header by name (case-insensitive).

        Builds a lowercased header map on first call and caches it in
        ``scope["_pt_header_map"]`` for O(1) lookups by all downstream
        middleware in the same request.
        """
        header_map = scope.get("_pt_header_map")
        if header_map is None:
            header_map = {k.decode().lower(): v.decode() for k, v in scope.get("headers", ())}
            scope["_pt_header_map"] = header_map
        return header_map.get(name.lower(), default)
