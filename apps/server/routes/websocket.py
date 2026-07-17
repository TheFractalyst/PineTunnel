"""WebSocket endpoint for real-time signal push to EAs."""

import json
import logging

from fastapi import APIRouter, WebSocket

from apps.server.ws.handler import websocket_endpoint as ws_endpoint_handler

from .ea_versions import _ea_versions

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


# ---------------------------------------------------------------------------
# NOTE: The _ws_push_signal function is defined in app_factory.py and wired
# into the webhook pipeline via init_ws_push(). It handles both Redis Pub/Sub
# fan-out and local WebSocket push. Do NOT add another _ws_push_signal here —
# that would create a duplicate delivery path.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket route
# ---------------------------------------------------------------------------


@router.websocket("/ws/{license_key}")
async def ws_signal_endpoint(websocket: WebSocket, license_key: str):
    """WebSocket endpoint for real-time signal delivery to EAs.

    EAs that support WebSocket connect here and receive instant signal push
    instead of HTTP polling. The EA sends ACK and heartbeat messages; the
    server pushes signal JSON in the same format as the HTTP polling response.

    If the EA disconnects or the DLL is unavailable, the EA falls back to
    HTTP long-polling automatically (signals are always saved to DB first).
    """
    # Import state at call time — module-level imports capture None values
    # set during state.py initialization, not the real singletons wired
    # during the application lifespan.
    from apps.server.state import client_manager, conn_manager, db_manager, ws_manager

    try:
        await ws_endpoint_handler(
            websocket=websocket,
            license_key=license_key,
            ws_manager=ws_manager,
            client_manager=client_manager,
            db_manager=db_manager,
            ea_versions=_ea_versions,
            conn_manager=conn_manager,
        )
    except Exception as e:
        logger.error(
            "WS endpoint unhandled error for %s: %s: %s",
            license_key[:4] + "..." if len(license_key) > 4 else license_key,
            type(e).__name__,
            e,
            exc_info=True,
        )
        # Try to send error JSON before closing (Cloudflare strips close frame payloads)
        try:
            await websocket.accept()
        except Exception as e:
            logger.debug("WS accept failed for %s: %s", license_key[:4] + "...", e)
        try:
            await websocket.send_text(
                json.dumps(
                    {"type": "error", "code": 1011, "reason": "Internal server error"},
                    separators=(",", ":"),
                )
            )
        except Exception as e:
            logger.debug("WS error send failed for %s: %s", license_key[:4] + "...", e)
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception as e:
            logger.debug("WS close failed for %s: %s", license_key[:4] + "...", e)
