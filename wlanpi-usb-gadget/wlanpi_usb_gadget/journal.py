"""Native systemd journald datagram logger (stdlib only)."""

from __future__ import annotations

import os
import socket
import struct
import sys
from pathlib import Path

JOURNAL_SOCKET = Path("/run/systemd/journal/socket")

PRIORITY_ERR = 3
PRIORITY_WARNING = 4
PRIORITY_INFO = 6
PRIORITY_DEBUG = 7


def _encode_field(key: str, value: str) -> bytes:
    """Encode a single journald field using native protocol framing."""
    if "\n" in value:
        key_bytes = key.encode("utf-8")
        val_bytes = value.encode("utf-8")
        return key_bytes + b"\n" + struct.pack("<Q", len(val_bytes)) + val_bytes + b"\n"
    return f"{key}={value}\n".encode("utf-8")


def encode_journal_datagram(
    message: str,
    *,
    priority: int = PRIORITY_INFO,
    identifier: str = "wlanpi-usb-gadget",
    fields: dict[str, str] | None = None,
) -> bytes:
    """Build a journald native protocol datagram payload."""
    parts = [
        _encode_field("MESSAGE", message),
        _encode_field("PRIORITY", str(priority)),
        _encode_field("SYSLOG_IDENTIFIER", identifier),
        _encode_field("PID", str(os.getpid())),
    ]
    if fields:
        for k, v in fields.items():
            if v is not None and v != "":
                parts.append(_encode_field(k.upper(), str(v)))
    return b"".join(parts)


def log_journal(
    message: str,
    *,
    priority: int = PRIORITY_INFO,
    socket_path: Path = JOURNAL_SOCKET,
    **fields: str,
) -> None:
    """Send a structured log entry to journald, falling back to stderr if unavailable."""
    clean_fields = {k: v for k, v in fields.items() if v}
    if socket_path.exists():
        payload = encode_journal_datagram(message, priority=priority, fields=clean_fields)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.sendto(payload, str(socket_path))
            return
        except OSError:
            pass

    extra = " ".join(f"{k}={v}" for k, v in sorted(clean_fields.items()))
    line = f"[{priority}] {message}" + (f" ({extra})" if extra else "")
    print(line, file=sys.stderr)
