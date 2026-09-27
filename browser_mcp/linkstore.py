"""Short-lived download links for browser screenshots (compatibility test).

Images are kept in a private 0700 temp dir outside the workspace. Each link is
an unguessable 192-bit token that only exists as a key in an in-memory index, so
request input is never turned into a filesystem path. Links expire after at
most 10 minutes (checked on every request) and storage is capped.

Assumes a single server process (VM deployment, one uvicorn worker): the index
lives in memory, so another process/instance could not serve the link.
"""

from __future__ import annotations

import atexit
import glob
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ROUTE_PREFIX = "/browser-screenshots/"
MAX_TTL_S = 600
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")  # token_urlsafe(24) → 32 chars, 192 bits


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


TTL_S = max(1, min(_env_int("BROWSER_SCREENSHOT_LINK_TTL_S", MAX_TTL_S), MAX_TTL_S))
MAX_ITEMS = max(1, _env_int("BROWSER_SCREENSHOT_LINK_MAX_ITEMS", 20))
MAX_TOTAL_BYTES = max(1, _env_int("BROWSER_SCREENSHOT_LINK_MAX_BYTES", 50 * 1024 * 1024))


def public_base_url() -> str | None:
    """Explicitly configured public origin (never derived from a request's Host header)."""
    raw = os.environ.get("BROWSER_SCREENSHOT_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return None
    p = urlsplit(raw)
    if p.scheme != "https" or not p.hostname or p.path or p.query or p.fragment or p.username:
        logger.warning("BROWSER_SCREENSHOT_BASE_URL ignored: must be a bare https origin")
        return None
    return raw


def link_allowed_for(url: str) -> bool:
    hosts = os.environ.get(
        "BROWSER_SCREENSHOT_LINK_HOSTS", "abonnement.website,www.abonnement.website"
    )
    allowed = {h.strip().lower() for h in hosts.split(",") if h.strip()}
    host = (urlsplit(url).hostname or "").lower()
    return host in allowed


def _remove_stale_dirs() -> None:
    """Removes our temp dirs left by an earlier process (e.g. after a crash/restart).

    Only dirs owned by this user and untouched for longer than the max link lifetime
    are removed, so links of another live process are never affected.
    """
    cutoff = time.time() - MAX_TTL_S - 60
    for path in glob.glob(os.path.join(tempfile.gettempdir(), "shotlinks-*")):
        try:
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


@dataclass
class _Item:
    path: str
    size: int
    created: float
    expires: float


class LinkStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, _Item] = {}
        self._dir: str | None = None

    def _directory(self) -> str:
        if self._dir is None or not os.path.isdir(self._dir):
            _remove_stale_dirs()
            self._dir = tempfile.mkdtemp(prefix="shotlinks-")  # mode 0700
            atexit.register(shutil.rmtree, self._dir, True)
        return self._dir

    def _drop(self, token: str) -> None:
        item = self._items.pop(token, None)
        if item:
            try:
                os.unlink(item.path)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("screenshot link cleanup failed: %s", type(e).__name__)

    def purge_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for token in [t for t, i in self._items.items() if i.expires <= now]:
                self._drop(token)

    def put(self, data: bytes) -> tuple[str, float] | None:
        """Stores the image; returns (token, expires_epoch) or None if it can't fit."""
        if len(data) > MAX_TOTAL_BYTES:
            return None
        now = time.time()
        self.purge_expired(now)
        with self._lock:
            # Evict oldest until the new image fits within both limits.
            while self._items and (
                len(self._items) >= MAX_ITEMS
                or sum(i.size for i in self._items.values()) + len(data) > MAX_TOTAL_BYTES
            ):
                oldest = min(self._items, key=lambda t: self._items[t].created)
                self._drop(oldest)
            token = secrets.token_urlsafe(24)
            path = os.path.join(self._directory(), secrets.token_hex(16) + ".jpg")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            expires = now + TTL_S
            self._items[token] = _Item(path=path, size=len(data), created=now, expires=expires)
        self._schedule_purge()
        return token, expires

    def get(self, token: str) -> bytes | None:
        if not _TOKEN_RE.match(token or ""):
            return None
        now = time.time()
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            if item.expires <= now:
                self._drop(token)
                return None
            try:
                with open(item.path, "rb") as f:
                    return f.read()
            except OSError:
                self._drop(token)
                return None

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return len(self._items), sum(i.size for i in self._items.values())

    def _schedule_purge(self) -> None:
        # Physical cleanup shortly after expiry, even if nobody requests the link again.
        timer = threading.Timer(TTL_S + 1, self.purge_expired)
        timer.daemon = True
        timer.start()


store = LinkStore()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class _RedactLinkTokens(logging.Filter):
    """Keeps link tokens out of access logs (e.g. uvicorn's 'GET /browser-screenshots/<token>')."""

    _re = re.compile(re.escape(ROUTE_PREFIX) + r"[^\s\"?]*")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(
                self._re.sub(ROUTE_PREFIX + "[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str) and ROUTE_PREFIX in record.msg:
            record.msg = self._re.sub(ROUTE_PREFIX + "[redacted]", record.msg)
        return True


def install_log_redaction() -> None:
    f = _RedactLinkTokens()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        lg = logging.getLogger(name)
        if not any(isinstance(x, _RedactLinkTokens) for x in lg.filters):
            lg.addFilter(f)
