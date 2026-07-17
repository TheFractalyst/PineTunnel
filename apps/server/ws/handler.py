"""WebSocket connection manager and handler for real-time signal delivery.

Provides per-worker WebSocket connection tracking and push delivery alongside
the existing HTTP long-polling mechanism. EAs that support WebSocket get
instant signal push; EAs that don't fall back to HTTP polling unchanged.

Both transports coexist - signals are always saved to DB first (ensuring
long-poll EAs can still fetch them), then pushed via WebSocket to any
connected EAs for that license key.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from apps.server.config.settings import get_config
from apps.server.utils import mask_string as _mask

logger = logging.getLogger(__name__)


async def _ws_reject(websocket: WebSocket, code: int, reason: str) -> None:
    """Accept a WebSocket, send an error JSON message, then close.

    Cloudflare strips close-frame payloads, so we send the reason as a
    JSON message before closing. The EA reads the JSON on reconnect.
    """
    await websocket.accept()
    await websocket.send_text(
        json.dumps(
            {"type": "error", "code": code, "reason": reason}, separators=_WS_JSON_SEPARATORS
        )
    )
    await websocket.close(code=code, reason=reason)


async def _refresh_db_connection(license_key: str) -> None:
    """Refresh the EA connection record in the DB (single source of truth)."""
    try:
        from apps.server.state import db_manager as _dbm

        if _dbm and hasattr(_dbm, "register_ea_connection"):
            await asyncio.to_thread(_dbm.register_ea_connection, license_key, "ws", os.getpid())
    except Exception as e:
        logger.debug("refresh_db_connection failed for %s: %s", license_key, e)


async def _ws_telemetry_save(
    db_manager: Any, method_name: str, license_key: str, data: Any
) -> None:
    """Run a DB telemetry save in an executor thread with error handling."""
    loop = asyncio.get_running_loop()
    method = getattr(db_manager, method_name, None)
    if method is None:
        return
    try:
        await loop.run_in_executor(None, method, license_key, data)
        logger.debug("WS %s stored for %s", method_name, _mask(license_key))
    except Exception as e:
        logger.error("WS %s failed for %s: %s", method_name, _mask(license_key), e)


# WebSocket close codes (custom range 4000-4999 per RFC 6455)
WS_CLOSE_INVALID_LICENSE = 4001
WS_CLOSE_SERVER_SHUTDOWN = 4002
WS_CLOSE_RATE_LIMITED = 4003
WS_CLOSE_IDLE_TIMEOUT = 4004
WS_CLOSE_EXPIRED_LICENSE = 4005

# Rate limiting thresholds
_MAX_WS_CONNECTIONS_PER_LICENSE = 1000  # Max simultaneous WS conns per license
_MAX_WS_CONNECTS_PER_MINUTE = 300  # Max new connections per license per minute (legit EAs connect once and stay; 300/min = 5/s survives reconnect storms)
_MAX_WS_TELEMETRY_WRITES_PER_MINUTE = 6000  # Max telemetry writes per license per minute

# Idle timeout: disconnect EAs that stop sending heartbeats
_WS_IDLE_TIMEOUT_SEC = 600  # 10 min - generous to survive brief network blips

# Compact JSON separators matching FastAPI/EA parsing expectations.
# The EA uses StringFind(json, "\"signals\":[") which requires no spaces.
_WS_JSON_SEPARATORS = (",", ":")

# Minimum interval between DB EA-connection refreshes per WebSocket. The DB
# record is the single source of truth for liveness; refreshing on every
# inbound message would block the event loop with a synchronous transaction
# (Postgres). 30s is well under the 120s stale threshold, so liveness
# detection is identical - only admin-displayed last_seen coarsens slightly.
_WS_DB_REFRESH_INTERVAL_S = 30.0


class WebSocketConnectionManager:
    """Per-worker WebSocket connection manager.

    Tracks active WebSocket connections keyed by license_key.
    Each license key can have multiple connections (e.g., multiple EAs
    sharing the same license).

    This manager is NOT shared across workers. For multi-worker deployments,
    Redis Pub/Sub is used to fan out signals from the webhook handler to
    all workers that may have WebSocket connections.
    """

    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}
        # Rate limiting: track recent connection attempts per license_key
        self._connect_timestamps: dict[str, list[float]] = defaultdict(list)
        # Rate limiting: track telemetry writes per license_key (account_stats, positions, etc.)
        self._telemetry_timestamps: dict[str, list[float]] = defaultdict(list)
        # Idle tracking: last activity time per WebSocket connection
        self._last_activity: dict[int, float] = {}  # id(ws) -> timestamp
        # DB refresh throttle: last register_ea_connection time per connection
        self._last_db_refresh: dict[int, float] = {}  # id(ws) -> timestamp
        # Metrics: cumulative counters for observability
        self._total_signals_pushed: int = 0
        self._total_acks_received: int = 0
        self._total_connections_ever: int = 0

    def cleanup_stale(self) -> None:
        """Prune rate-limit timestamps for disconnected licenses and enforce idle timeout."""
        now = time.time()
        # Clean up rate-limit timestamps
        stale = [key for key, ts in self._connect_timestamps.items() if not ts or now - ts[-1] > 60]
        for key in stale:
            del self._connect_timestamps[key]

        # Clean up idle connections
        idle_conns: list[tuple[str, WebSocket]] = []
        for license_key, conns in self._connections.items():
            for ws in conns:
                last = self._last_activity.get(id(ws), now)
                if now - last > _WS_IDLE_TIMEOUT_SEC:
                    idle_conns.append((license_key, ws))

        if not idle_conns:
            return
        loop = asyncio.get_running_loop()
        for license_key, ws in idle_conns:
            logger.info(
                "WS idle timeout: %s (no activity for %ds)",
                _mask(license_key),
                _WS_IDLE_TIMEOUT_SEC,
            )
            try:
                loop.create_task(
                    ws.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "code": WS_CLOSE_IDLE_TIMEOUT,
                                "reason": "Idle timeout",
                            },
                            separators=_WS_JSON_SEPARATORS,
                        )
                    )
                )
                loop.create_task(ws.close(code=WS_CLOSE_IDLE_TIMEOUT, reason="Idle timeout"))
            except Exception as e:
                logger.debug("Idle timeout close failed for %s: %s", _mask(license_key), e)
            self.remove(license_key, ws)

    def _check_rate_limit(self, license_key: str) -> bool:
        """Check if a new WebSocket connection is allowed for this license key.

        Returns True if the connection is allowed, False if rate limited.
        Enforces:
          - Max simultaneous connections per license
          - Max new connections per minute per license
        """
        now = time.time()

        # Remove timestamps older than 60 seconds
        timestamps = self._connect_timestamps.get(license_key, [])
        filtered = [t for t in timestamps if now - t < 60]
        self._connect_timestamps[license_key] = filtered

        # Check connection rate (connects per minute)
        if len(filtered) >= _MAX_WS_CONNECTS_PER_MINUTE:
            logger.warning(
                "WS rate limited: %s exceeded %d connects/min",
                _mask(license_key),
                _MAX_WS_CONNECTS_PER_MINUTE,
            )
            return False

        # Check max simultaneous connections
        conn_count = len(self._connections.get(license_key, []))
        if conn_count >= _MAX_WS_CONNECTIONS_PER_LICENSE:
            logger.warning(
                "WS rate limited: %s already has %d connections (max %d)",
                _mask(license_key),
                conn_count,
                _MAX_WS_CONNECTIONS_PER_LICENSE,
            )
            return False

        return True

    def _check_telemetry_rate(self, license_key: str) -> bool:
        """Check if a telemetry write is allowed for this license key.

        Returns True if allowed, False if rate limited.
        Enforces: max telemetry writes per minute per license.
        """
        now = time.time()
        timestamps = self._telemetry_timestamps.get(license_key, [])
        filtered = [t for t in timestamps if now - t < 60]
        self._telemetry_timestamps[license_key] = filtered

        if len(filtered) >= _MAX_WS_TELEMETRY_WRITES_PER_MINUTE:
            logger.debug(
                "WS telemetry rate limited: %s exceeded %d writes/min",
                _mask(license_key),
                _MAX_WS_TELEMETRY_WRITES_PER_MINUTE,
            )
            return False

        self._telemetry_timestamps[license_key].append(now)
        return True

    def add(self, license_key: str, ws: WebSocket) -> None:
        """Register a WebSocket connection for a license key."""
        if license_key not in self._connections:
            self._connections[license_key] = []
        self._connections[license_key].append(ws)
        now = time.time()
        # Track connection attempt for rate limiting
        self._connect_timestamps[license_key].append(now)
        # Track initial activity time for idle timeout
        self._last_activity[id(ws)] = now
        # Metrics: increment total connections counter
        self._total_connections_ever += 1
        logger.info(
            "WS connected: %s (total for key: %d, total all: %d)",
            _mask(license_key),
            len(self._connections[license_key]),
            self.get_total_connections(),
        )

    def remove(self, license_key: str, ws: WebSocket) -> None:
        """Remove a WebSocket connection."""
        if license_key in self._connections:
            try:
                self._connections[license_key].remove(ws)
            except ValueError:
                pass
            if not self._connections[license_key]:
                del self._connections[license_key]
                logger.info(
                    "WS disconnected: %s (no more connections for this key)",
                    _mask(license_key),
                )
        # Clean up activity tracking
        self._last_activity.pop(id(ws), None)
        self._last_db_refresh.pop(id(ws), None)

    async def send_to_license(
        self, license_key: str, message: str, *, skip_backpressured: bool = False
    ) -> int:
        """Send a message to all WebSocket connections for a license key.

        Returns the number of connections that successfully received the message.
        Dead connections are automatically cleaned up.

        If skip_backpressured is True, connections that have sent a backpressure
        message (EA's internal WS queue > 80%) are skipped - used for
        non-critical pushes like request_stats/request_positions.
        """
        if license_key not in self._connections:
            return 0

        sent = 0
        dead: list[WebSocket] = []
        conns = self._connections[license_key]
        now = time.time()
        for ws in conns:
            # Skip backpressured connections for non-critical messages
            if skip_backpressured and getattr(ws, "pt_backpressure", 0) > 0:
                continue
            try:
                await ws.send_text(message)
                # Update activity time on successful send
                self._last_activity[id(ws)] = now
                sent += 1
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.remove(license_key, ws)

        if dead:
            logger.debug(
                "WS send_to_license %s: cleaned %d dead connections",
                _mask(license_key),
                len(dead),
            )

        return sent

    def get_connections_for_key(self, license_key: str) -> list[WebSocket]:
        """Return the list of active WebSocket connections for a license key."""
        return self._connections.get(license_key, [])

    def get_connection_count(self, license_key: str) -> int:
        """Return the number of active WebSocket connections for a license key."""
        return len(self._connections.get(license_key, []))

    def get_total_connections(self) -> int:
        """Return the total number of active WebSocket connections across all keys."""
        return sum(len(conns) for conns in self._connections.values())

    def get_connected_license_keys(self) -> list[str]:
        """Return all license keys that have at least one active WebSocket connection."""
        return list(self._connections.keys())

    def build_connections_response(self) -> dict[str, Any]:
        """Build a JSON-serializable dict of WebSocket connection info."""
        ws_info: list[dict[str, Any]] = []
        for license_key, conns in self._connections.items():
            ws_info.append(
                {
                    "license": _mask(license_key),
                    "connections": len(conns),
                }
            )
        return {
            "websocket_connections": self.get_total_connections(),
            "licenses": ws_info,
            "total_signals_pushed": self._total_signals_pushed,
            "total_acks_received": self._total_acks_received,
            "total_connections_ever": self._total_connections_ever,
        }

    def build_public_connections_response(self) -> dict[str, Any]:
        """Build a public-safe (no PII) connections dict."""
        return {
            "websocket_connections": self.get_total_connections(),
            "total_signals_pushed": self._total_signals_pushed,
            "total_acks_received": self._total_acks_received,
        }


async def websocket_endpoint(
    websocket: WebSocket,
    license_key: str,
    ws_manager: WebSocketConnectionManager,
    client_manager: Any,
    db_manager: Any,
    ea_versions: dict[str, Any] | None = None,
    conn_manager: Any = None,
) -> None:
    """FastAPI WebSocket endpoint for real-time signal delivery.

    Protocol messages (server → EA):
      - {"type":"signal","signals":[...]}   - New signal(s) to execute
      - {"type":"pong","timestamp":...}     - Heartbeat response
      - {"type":"version","latest_version_mt5":...,"update_notes_mt5":...} - Version check
      - {"type":"error","code":...,"reason":"..."} - Rejection/error before close

    Protocol messages (EA → server):
      - {"type":"ack","signal_ids":["id1","id2"]}  - Acknowledge signal delivery
      - {"type":"ping","timestamp":...}              - Heartbeat keepalive

    Close codes:
      - 4001: Invalid license key
      - 4002: Server shutdown
      - 4003: Rate limited
      - 4005: License expired
    """
    # Guard against uninitialized state (can happen during server startup)
    if not client_manager or not ws_manager:
        logger.error(
            "WS handler called with uninitialized state: client_manager=%s, ws_manager=%s",
            type(client_manager).__name__ if client_manager else None,
            type(ws_manager).__name__ if ws_manager else None,
        )
        await _ws_reject(websocket, 1011, "Server not ready")
        return

    # Pre-acceptance validation: reject invalid connections before entering the main loop.
    try:
        client = client_manager.get_client_by_license(license_key)
    except Exception as e:
        logger.error(
            "WS license lookup failed for %s: %s: %s", _mask(license_key), type(e).__name__, e
        )
        await _ws_reject(websocket, 1011, "License lookup error")
        return

    if not client:
        logger.warning("WS rejected: invalid license key %s", _mask(license_key))
        await _ws_reject(websocket, WS_CLOSE_INVALID_LICENSE, "Invalid license key")
        return

    # Validate license (expiry + token integrity)
    valid, msg = client_manager.validate_license(license_key)
    if not valid:
        if "expired" in msg.lower():
            logger.warning("WS rejected: expired license %s", _mask(license_key))
            await _ws_reject(websocket, WS_CLOSE_EXPIRED_LICENSE, "License expired")
        else:
            logger.warning("WS rejected: invalid license %s: %s", _mask(license_key), msg)
            await _ws_reject(websocket, WS_CLOSE_INVALID_LICENSE, "Invalid license key")
        return

    # Validate Origin header to prevent cross-site WebSocket hijacking
    origin = websocket.headers.get("origin", "")
    if origin:
        allowed_origins = get_config().server.parsed_cors_origins
        if allowed_origins and origin not in allowed_origins:
            logger.warning("WS rejected: invalid origin %s for %s", origin, _mask(license_key))
            await _ws_reject(websocket, 4003, "Invalid origin")
            return
    # If no Origin header and no allowed origins configured, allow (EA polling scenarios)

    # Check rate limit before accepting
    if not ws_manager._check_rate_limit(license_key):
        await _ws_reject(websocket, WS_CLOSE_RATE_LIMITED, "Rate limited")
        return

    # Accept the WebSocket connection
    await websocket.accept()
    ws_manager.add(license_key, websocket)

    # Register this WS connection globally in Redis so other workers'
    # reliability monitors can see it (multi-worker false-alert fix)
    ws_id = id(websocket)
    ws_conn_id = f"{os.getpid()}:{ws_id}"
    if conn_manager and hasattr(conn_manager, "register_ws_connection"):
        try:
            await conn_manager.register_ws_connection(license_key, ws_conn_id)
        except Exception as e:
            logger.debug("register_ws_connection failed for %s: %s", _mask(license_key), e)

    # Register in PostgreSQL as single source of truth
    await _refresh_db_connection(license_key)
    ws_manager._last_db_refresh[ws_id] = time.time()

    # Send version info immediately upon connection (EA checks for updates)
    if ea_versions:
        try:
            version_msg: dict[str, Any] = {"type": "version"}
            if "mt5" in ea_versions:
                version_msg["latest_version_mt5"] = ea_versions["mt5"].get("version", "")
                version_msg["update_notes_mt5"] = ea_versions["mt5"].get("release_notes", "")
            if "mt4" in ea_versions:
                version_msg["latest_version_mt4"] = ea_versions["mt4"].get("version", "")
                version_msg["update_notes_mt4"] = ea_versions["mt4"].get("release_notes", "")
            await websocket.send_text(json.dumps(version_msg, separators=_WS_JSON_SEPARATORS))
        except Exception as e:
            logger.warning("WS version send failed for %s: %s", _mask(license_key), e)

    logger.info(
        "WS session started: license=%s (%s)",
        _mask(license_key),
        client.get("name", "Unknown") if isinstance(client, dict) else "client",
    )

    try:
        while True:
            try:
                # Wait for EA messages with 15s timeout - if no message arrives,
                # send a server-side keepalive to prevent idle disconnects
                data = await asyncio.wait_for(websocket.receive_text(), timeout=15.0)
            except asyncio.TimeoutError:
                # No message from EA in 15s - send keepalive to keep connection alive
                # This is a DATA frame (not WS protocol ping) that Cloudflare always forwards
                # and WinHTTP's receive callback on the EA side will see as activity
                try:
                    await websocket.send_text(
                        f'{{"type":"keepalive","timestamp":{int(time.time())}}}'
                    )
                except Exception:
                    break
                continue
            except WebSocketDisconnect:
                break

            # Parse incoming message from EA
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                logger.warning("WS invalid JSON from %s: %s", _mask(license_key), data[:100])
                continue

            # Update activity time on any received message (heartbeat, ack, etc.)
            now = time.time()
            ws_manager._last_activity[ws_id] = now

            # Refresh Redis TTL so other workers keep seeing this connection
            if conn_manager and hasattr(conn_manager, "refresh_ws_heartbeat"):
                try:
                    await conn_manager.refresh_ws_heartbeat(license_key, ws_conn_id)
                except Exception as e:
                    logger.debug("refresh_ws_heartbeat failed for %s: %s", _mask(license_key), e)

            # Refresh DB connection record (single source of truth) - throttled
            # to avoid a synchronous DB transaction on every inbound message.
            if now - ws_manager._last_db_refresh.get(ws_id, 0.0) >= _WS_DB_REFRESH_INTERVAL_S:
                await _refresh_db_connection(license_key)
                ws_manager._last_db_refresh[ws_id] = now

            msg_type = msg.get("type", "")

            if msg_type == "ack":
                # EA acknowledges signal delivery - mark as delivered in DB
                signal_ids = msg.get("signal_ids", [])
                if (
                    signal_ids
                    and isinstance(signal_ids, list)
                    and len(signal_ids) <= 100
                    and db_manager
                ):
                    try:
                        result = await db_manager.acknowledge_signals_batch_async(
                            signal_ids, license_key=license_key
                        )
                        ack_count = len(result.get("acknowledged", []))
                        ws_manager._total_acks_received += ack_count
                        # Also mark as acknowledged in permanent signal log.
                        # Collapse the per-signal executor round-trips into one
                        # thread submission that iterates internally - same
                        # per-sid lock+commit and partial-commit-on-failure, but
                        # one executor dispatch instead of up to 100.
                        if hasattr(db_manager, "acknowledge_signal_log"):
                            loop = asyncio.get_running_loop()
                            ack_sids = signal_ids[:100]

                            def _ack_logs(_sids: list[Any] = ack_sids) -> None:
                                for sid in _sids:
                                    db_manager.acknowledge_signal_log(sid, "ws")

                            await loop.run_in_executor(None, _ack_logs)
                        logger.debug(
                            "WS ACK: %s marked %d/%d signals delivered",
                            _mask(license_key),
                            ack_count,
                            len(signal_ids),
                        )
                    except Exception as e:
                        logger.error(
                            "WS ACK failed for %s: %s",
                            _mask(license_key),
                            e,
                        )

            elif msg_type == "ping":
                # Heartbeat keepalive - respond with pong
                await websocket.send_text(f'{{"type":"pong","timestamp":{int(time.time())}}}')

            elif msg_type == "account_stats":
                if db_manager and ws_manager._check_telemetry_rate(license_key):
                    await _ws_telemetry_save(db_manager, "save_ws_account_stats", license_key, msg)

            elif msg_type == "open_positions":
                if db_manager and ws_manager._check_telemetry_rate(license_key):
                    positions = msg.get("positions", [])
                    if isinstance(positions, list):
                        await _ws_telemetry_save(
                            db_manager, "save_ws_open_positions", license_key, positions
                        )

            elif msg_type == "trade_history":
                if db_manager and ws_manager._check_telemetry_rate(license_key):
                    deals = msg.get("deals", msg.get("orders", []))
                    if isinstance(deals, list):
                        await _ws_telemetry_save(
                            db_manager, "save_ws_trade_history", license_key, deals
                        )

            elif msg_type == "health":
                if db_manager and ws_manager._check_telemetry_rate(license_key):
                    await _ws_telemetry_save(
                        db_manager, "save_ws_health_telemetry", license_key, msg
                    )

            elif msg_type == "version":
                pass

            elif msg_type == "backpressure":
                queue_depth = msg.get("ws_queue_depth", 0)
                logger.info(
                    "WS backpressure from %s: queue_depth=%d", _mask(license_key), queue_depth
                )
                websocket.pt_backpressure = queue_depth

            else:
                logger.debug("WS unknown message type '%s' from %s", msg_type, _mask(license_key))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WS error for %s: %s", _mask(license_key), e)
    finally:
        ws_manager.remove(license_key, websocket)
        if conn_manager and hasattr(conn_manager, "deregister_ws_connection"):
            try:
                await conn_manager.deregister_ws_connection(license_key, ws_conn_id)
            except Exception as e:
                logger.debug("deregister_ws_connection failed for %s: %s", _mask(license_key), e)
        logger.info(
            "WS session ended: %s (total: %d)",
            _mask(license_key),
            ws_manager.get_total_connections(),
        )


async def broadcast_signal_to_websocket(
    ws_manager: WebSocketConnectionManager,
    license_key: str,
    signal_data: dict[str, Any],
) -> int:
    """Push a signal to all WebSocket-connected EAs for a license key.

    This is called from the webhook pipeline after saving the signal to DB.
    Returns the number of EAs that received the push (0 if none connected).
    """
    message = json.dumps(
        {"type": "signal", "signals": [signal_data]}, separators=_WS_JSON_SEPARATORS
    )
    sent = await ws_manager.send_to_license(license_key, message)
    ws_manager._total_signals_pushed += sent
    return sent


async def request_close_position(
    ws_manager: WebSocketConnectionManager,
    license_key: str,
    ticket: int,
) -> int:
    """Request an EA to close a specific position by ticket.

    Sends {"type":"request_close","ticket":T} to the EA.
    Returns the number of EAs that received the request.
    """
    message = json.dumps(
        {"type": "request_close", "ticket": ticket},
        separators=_WS_JSON_SEPARATORS,
    )
    return await ws_manager.send_to_license(license_key, message)


async def start_redis_ws_subscriber(
    ws_manager: WebSocketConnectionManager,
    redis_client: Any,
) -> asyncio.Task | None:
    """Start a Redis Pub/Sub listener that forwards signals to WebSocket connections.

    In multi-worker deployments, the webhook handler on worker A may receive
    a signal, but the EA's WebSocket connection may be on worker B. Redis
    Pub/Sub broadcasts the signal to all workers, and each worker pushes it
    to its locally-connected WebSocket clients.

    Returns the asyncio.Task for the background listener, or None if Redis
    is not available.
    """
    if redis_client is None:
        return None

    pubsub = redis_client.pubsub()

    try:
        await pubsub.psubscribe("ws_signals:*")
    except Exception as e:
        logger.error("WS Redis subscriber failed to subscribe: %s", e)
        return None

    async def _listener():
        try:
            async for message in pubsub.listen():
                if message["type"] != "pmessage":
                    continue

                # Extract license key from channel: "ws_signals:{license_key}"
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                license_key = channel.rsplit(":", 1)[-1]

                # Extract signal data
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()

                try:
                    signal_data = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    signal_data = data

                # Push to local WebSocket connections
                message_str = json.dumps(
                    {"type": "signal", "signals": [signal_data]}, separators=_WS_JSON_SEPARATORS
                )
                await ws_manager.send_to_license(license_key, message_str)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("WS Redis listener error: %s", e)
        finally:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception as e:
                logger.debug("WS Redis pubsub cleanup failed: %s", e)

    task = asyncio.create_task(_listener())
    logger.info("WS Redis subscriber started (ws_signals:*)")
    return task


async def publish_signal_to_redis(
    redis_client: Any,
    license_key: str,
    signal_data: dict[str, Any],
) -> None:
    """Publish a signal to Redis for multi-worker WebSocket fan-out.

    Called from the webhook pipeline after saving to DB and pushing to
    local WebSocket connections.
    """
    if redis_client is None:
        return

    try:
        channel = f"ws_signals:{license_key}"
        await redis_client.publish(channel, json.dumps(signal_data))
    except Exception as e:
        logger.warning("WS Redis publish failed for %s: %s", _mask(license_key), e)
