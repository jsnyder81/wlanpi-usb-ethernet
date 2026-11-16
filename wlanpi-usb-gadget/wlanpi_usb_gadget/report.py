"""Diagnostic tar.gz report generator (no secrets)."""

from __future__ import annotations

import io
import json
import platform
import subprocess
import tarfile
import time
from pathlib import Path
from typing import BinaryIO

from wlanpi_usb_gadget.config import load_config
from wlanpi_usb_gadget.configfs import DEFAULT_CONFIGFS_ROOT, GADGET_NAME
from wlanpi_usb_gadget.events import read_events
from wlanpi_usb_gadget.status import collect_status, run_selftest


def _run_text(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
        return f"$ {' '.join(cmd)} (rc={res.returncode})\n{res.stdout}\n{res.stderr}".strip() + "\n"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"$ {' '.join(cmd)} -> error: {exc}\n"


def _add_text_member(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8", errors="replace")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _dump_configfs(root: Path = DEFAULT_CONFIGFS_ROOT) -> str:
    gadget = root / GADGET_NAME
    if not gadget.exists():
        return f"{gadget} does not exist\n"
    lines: list[str] = []
    for p in sorted(gadget.rglob("*")):
        rel = p.relative_to(gadget)
        if p.is_symlink():
            lines.append(f"{rel} -> {p.resolve()}")
        elif p.is_dir():
            lines.append(f"{rel}/")
        else:
            try:
                val = p.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError as exc:
                val = f"<err: {exc}>"
            lines.append(f"{rel} = {val}")
    return "\n".join(lines) + "\n"


def write_report_tgz(out: BinaryIO) -> None:
    """Write a diagnostic .tar.gz bundle to `out`."""
    cfg = load_config()
    status = collect_status(cfg)
    selftest = [vars(c) for c in run_selftest(cfg)]
    events = read_events(newest_first=True)
    j_dir = Path("/var/log/journal")
    journal_persistent = j_dir.exists() and any(j_dir.iterdir())

    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        _add_text_member(tar, "report/status.json", json.dumps(status, indent=2) + "\n")
        _add_text_member(tar, "report/selftest.json", json.dumps(selftest, indent=2) + "\n")
        _add_text_member(tar, "report/config.json", json.dumps(cfg.as_dict(), indent=2) + "\n")
        _add_text_member(
            tar,
            "report/events.jsonl",
            "".join(json.dumps(e) + "\n" for e in events),
        )
        _add_text_member(tar, "report/kernel.txt", f"{platform.platform()}\n{platform.release()}\n")
        _add_text_member(tar, "report/configfs.txt", _dump_configfs())
        _add_text_member(tar, "report/ip_link.txt", _run_text(["ip", "-d", "link"]))
        _add_text_member(tar, "report/ip_addr.txt", _run_text(["ip", "addr"]))
        _add_text_member(tar, "report/ip_neigh.txt", _run_text(["ip", "neigh"]))
        _add_text_member(tar, "report/networkctl.txt", _run_text(["networkctl", "status", cfg.bridge_name]))
        _add_text_member(
            tar,
            "report/journal_current.txt",
            _run_text(["journalctl", "-u", "wlanpi-usb-gadget-*", "-b", "--no-pager", "-n", "500"]),
        )
        if journal_persistent:
            _add_text_member(
                tar,
                "report/journal_prev.txt",
                _run_text(["journalctl", "-u", "wlanpi-usb-gadget-*", "-b", "-1", "--no-pager", "-n", "300"]),
            )
