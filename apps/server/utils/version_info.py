"""Version info reader for PineTunnel."""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_VERSION_FILE = Path(__file__).parent.parent / "version.json"

_DEFAULT_VERSION_INFO: dict = {
    "version": "2.1.0",
    "release_date": "",
    "changes": [],
    "notes": "",
}

_VERSION_TTL = 60.0
_version_cache: tuple[float, dict] | None = None


def get_version_info() -> dict:
    """Read version.json and return structured version info.

    Cached with a 60-second TTL - the file changes only on deploy.
    """
    global _version_cache
    now = time.time()
    if _version_cache is not None:
        ts, data = _version_cache
        if now - ts < _VERSION_TTL:
            return data
    if _VERSION_FILE.exists():
        try:
            with _VERSION_FILE.open("r") as f:
                data = json.load(f)
            _version_cache = (now, data)
            return data
        except Exception as e:
            logger.warning("Could not read version.json: %s", e)
    return _DEFAULT_VERSION_INFO
