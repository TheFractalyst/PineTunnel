import re
from datetime import datetime, timezone
from functools import lru_cache

from telegram.helpers import escape_markdown as _escape_md
from telegram import InlineKeyboardButton

SEP = "-" * 20
_PAGE_SIZE = 8

_PATH_PY_RE = re.compile(r"/[^\s]+\.py")
_PATH_WIN_RE = re.compile(r"C:\\[^\s]+")


def escape_md(text: str) -> str:
    return _escape_md(str(text), version=1)


def mask_license_key(key: str) -> str:
    if not key:
        return "****"
    s = str(key)
    if len(s) <= 8:
        return s + "..."
    return s[:8] + "..."


def mask_secret(secret: str) -> str:
    if not secret:
        return "****"
    s = str(secret)
    if len(s) <= 4:
        return s + "****"
    return s[:4] + "****"


@lru_cache(maxsize=128)
def calc_pagination(page: int, total_items: int, page_size: int = 8) -> tuple[int, int, int]:
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return page, total_pages, start


def format_relative_time(iso_str: str) -> str:
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
    except Exception:
        return str(iso_str)[:16]
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    secs = int(diff.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def sanitize_error(e: Exception) -> str:
    text = str(e)[:200]
    text = _PATH_PY_RE.sub("<path>", text)
    text = _PATH_WIN_RE.sub("<path>", text)
    return escape_md(text)
