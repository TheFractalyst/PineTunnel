"""Webhook replay protection - dedup captured signals beyond the DB's 5-min window.

The signal-queue's ``save_signal`` dedup only covers a 5-minute window (designed for
webhook retries, not replay attacks). A captured valid signal is re-accepted after
that window expires. This module provides a longer-TTL replay store keyed on
``(license_key, content_hash)`` so a replayed signal is rejected for up to 7 days.

Design (behavior-preserving):
- A *legit* signal always carries fresh per-bar content (different symbol/price/volume
  → different content_hash), so it is never rejected here.
- A *replayed* signal has identical content → same content_hash → hit.
- On a hit, the caller returns the same ``{duplicate: true}`` 200 shape that the
  existing 5-min dedup already uses, so TradingView does not retry-loop and no client
  observes a new error code.

Backends:
- Redis (``state.redis_client``) when available: SET with TTL, multi-worker safe.
- In-process ``set`` fallback for single-worker / no-Redis: bounded by an LRU-style
  cap to avoid unbounded growth; not shared across workers (acceptable - the DB
  dedup is the cross-worker floor).

This module is intentionally self-contained: it is **not yet wired** into the webhook
execute path (that call site lives in the in-flight v3→v4 license migration file
``pinetunnel_webhook.py``). Wire it there once v4 lands:

    from apps.server.db.replay_store import is_replay
    if is_replay(license_key, content_hash):
        return JSONResponse({"duplicate": True}, status_code=200)

where ``content_hash`` is the same ``signal_hash`` computed by ``save_signal``
(``hashlib.md5(json.dumps(hash_data, sort_keys=True).encode()).hexdigest()``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# Replay window: a content_hash is remembered for this long so a captured signal
# cannot be replayed after the 5-min DB dedup expires. 7 days is well beyond any
# legit retry cadence; legit signals have fresh content per bar so are never hit.
REPLAY_TTL_S = 7 * 24 * 3600

# Cap the in-process fallback so a flood of distinct content hashes can't grow it
# unbounded. 4096 entries × ~80 bytes ≈ 320 KB ceiling.
_INPROC_MAX_ENTRIES = 4096

_inproc_store: "OrderedDict[str, float]" = OrderedDict()


def _redis() -> Any | None:
    """Return the shared async Redis client from state, or None if not configured."""
    try:
        from apps.server.state import redis_client

        return redis_client
    except Exception:
        return None


def _key(license_key: str, content_hash: str) -> str:
    """Redis key for a (license, content_hash) replay entry."""
    return f"pt:replay:{license_key}:{content_hash}"


def compute_content_hash(signal_data: dict) -> str:
    """Compute the content hash matching ``save_signal``'s dedup hash.

    Excludes volatile metadata (queued_at/timestamp/signal_id) so two captures of the
    same logical signal produce the same hash, exactly like the DB dedup. Uses the
    SAME serialization as ``database_manager_*.save_signal`` (``json.dumps(...,
    sort_keys=True)`` with default separators) so the hashes align when wired.
    """
    hash_data = {
        k: v for k, v in signal_data.items() if k not in ["queued_at", "timestamp", "signal_id"]
    }
    return hashlib.md5(json.dumps(hash_data, sort_keys=True).encode()).hexdigest()


def is_replay(license_key: str, content_hash: str) -> bool:
    """Synchronous check: has this (license, content_hash) been seen within the TTL?

    Returns True if the entry is present (a replay); False (and records the entry)
    if it is new. Safe to call on every signal - a new entry is recorded only once.

    Note: this is the in-process fallback path. For the Redis path use
    ``await is_replay_async`` which is preferred when ``state.redis_client`` is set.
    """
    now = time.time()
    key = _key(license_key, content_hash)

    # Purge expired head entries (OrderedDict insertion order ≈ age order)
    while _inproc_store:
        k, ts = next(iter(_inproc_store.items()))
        if now - ts >= REPLAY_TTL_S:
            _inproc_store.popitem(last=False)
        else:
            break

    if key in _inproc_store:
        # Refresh TTL on access so a repeatedly-replayed signal stays blocked
        _inproc_store.move_to_end(key)
        _inproc_store[key] = now
        return True

    _inproc_store[key] = now
    # Enforce the cap (evict oldest)
    while len(_inproc_store) > _INPROC_MAX_ENTRIES:
        _inproc_store.popitem(last=False)
    return False


async def is_replay_async(license_key: str, content_hash: str) -> bool:
    """Async check: prefer Redis (multi-worker safe); fall back to in-process.

    On Redis: SETNX with TTL - returns True if the key already existed (replay),
    False if it was newly set. A replay refreshes the TTL.
    """
    redis = _redis()
    if redis is None:
        return is_replay(license_key, content_hash)

    key = _key(license_key, content_hash)
    try:
        # SET key value NX EX ttl  → True if set (new), None if already exists (replay)
        was_new = await redis.set(key, "1", nx=True, ex=REPLAY_TTL_S)
        if was_new:
            return False
        # Already existed → replay. Refresh TTL so repeated replays stay blocked.
        await redis.expire(key, REPLAY_TTL_S)
        return True
    except Exception as e:
        # Redis down → degrade to in-process (never block a legit signal on infra)
        logger.warning("Replay-store Redis check failed, using in-process fallback: %s", e)
        return is_replay(license_key, content_hash)
