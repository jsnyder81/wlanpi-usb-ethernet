"""Mandatory bounded on-disk event history (/var/lib/wlanpi-usb-gadget/events.jsonl)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_EVENTS_PATH = Path("/var/lib/wlanpi-usb-gadget/events.jsonl")
MAX_EVENTS = 200


def record_event(
    event: str,
    *,
    state: str = "",
    prev_state: str = "",
    profile: str = "",
    udc_state: str = "",
    iface: str = "",
    incident: str = "",
    detail: str = "",
    events_path: Path = DEFAULT_EVENTS_PATH,
    max_events: int = MAX_EVENTS,
) -> dict[str, Any]:
    """Append an event entry, keeping at most `max_events` via atomic rewrite."""
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mono": round(time.monotonic(), 3),
        "event": event,
        "state": state,
        "prev_state": prev_state,
        "profile": profile,
        "udc_state": udc_state,
        "iface": iface,
        "incident": incident,
        "detail": detail,
    }

    existing = read_events(
        events_path=events_path,
        newest_first=False,
        raise_on_permission_error=True,
    )
    existing.append(entry)
    trimmed = existing[-max_events:]

    events_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(events_path.parent), prefix=".events.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for item in trimmed:
                fh.write(json.dumps(item, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, events_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return entry


def read_events(
    *,
    events_path: Path = DEFAULT_EVENTS_PATH,
    limit: int | None = None,
    newest_first: bool = True,
    raise_on_permission_error: bool = False,
) -> list[dict[str, Any]]:
    """Read recorded events from disk, tolerating corrupt lines and permission errors."""
    try:
        if not events_path.exists():
            return []
        text = events_path.read_text(encoding="utf-8", errors="ignore")
    except PermissionError:
        if raise_on_permission_error:
            raise
        if limit == 0:
            return []
        return [{"event": "permission_denied", "detail": "events: permission denied (use sudo)"}]
    except OSError as exc:
        if raise_on_permission_error:
            raise
        if limit == 0:
            return []
        return [{"event": "read_error", "detail": f"events: {exc}"}]

    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    if newest_first:
        items.reverse()
    if limit is not None and limit >= 0:
        return items[:limit]
    return items
