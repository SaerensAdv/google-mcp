"""Daily cap on Google Places calls (Google bills every request).

Counter per calendar day (Europe/Brussels), stored on disk so restarts do not
reset it. Configure with env PLACES_DAILY_CAP (default 200; 0 disables Places).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_DAILY_CAP = 200
TZ = ZoneInfo("Europe/Brussels")

_lock = threading.Lock()


class DailyCapReached(RuntimeError):
    pass


def _path() -> Path:
    return Path(os.environ.get("PLACES_USAGE_FILE", str(Path.home() / ".cache" / "places-usage" / "usage.json")))


def daily_cap() -> int:
    raw = os.environ.get("PLACES_DAILY_CAP", "").strip()
    if not raw:
        return DEFAULT_DAILY_CAP
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Invalid PLACES_DAILY_CAP=%r, using default %d", raw, DEFAULT_DAILY_CAP)
        return DEFAULT_DAILY_CAP


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def reserve_call() -> Tuple[int, int]:
    """Count one Places call for today; raise DailyCapReached when the cap is used up."""
    cap = daily_cap()
    with _lock:
        path = _path()
        today = _today()
        data = _load(path)
        used = int(data.get(today, 0)) if isinstance(data, dict) else 0
        if used >= cap:
            raise DailyCapReached(
                f"Daily Places limit reached ({used}/{cap} calls today). It resets at midnight "
                "(Brussels time). Raise PLACES_DAILY_CAP to allow more (each call costs money at Google)."
            )
        used += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({today: used}))  # only today's count is kept
        os.replace(tmp, path)
        return used, cap


def usage_today() -> Tuple[int, int]:
    with _lock:
        data = _load(_path())
    return int(data.get(_today(), 0)) if isinstance(data, dict) else 0, daily_cap()
