"""Board-scoped config.txt gadget-mode management (D16, section 7)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wlanpi_usb_gadget.journal import PRIORITY_INFO, log_journal

BEGIN_MARKER = "# BEGIN wlanpi-usb-gadget"
END_MARKER = "# END wlanpi-usb-gadget"
MANAGED_COMMENT = "# managed by wlanpi-usb-gadget"
# Documented dtoverlay syntax; also avoids old package postrm `sed '/^dtoverlay=dwc2$/d'`.
DWC2_OVERLAY_LINE = "dtoverlay=dwc2,dr_mode=otg"
DEFAULT_BLOCK_LINES = (
    BEGIN_MARKER,
    MANAGED_COMMENT,
    "[cm4]",
    DWC2_OVERLAY_LINE,
    "[all]",
    END_MARKER,
)

WLANPI_MODEL_PATH = Path("/etc/wlanpi-model")
DT_MODEL_PATH = Path("/sys/firmware/devicetree/base/model")
DEFAULT_BOOT_STATE_PATH = Path("/run/wlanpi-usb-gadget/bootcfg.changed")


@dataclass(frozen=True)
class BootCfgResult:
    """Result of a bootcfg ensure/remove/check operation."""

    is_go: bool
    board_model: str
    changed: bool
    block_present: bool
    reboot_required: bool
    message: str


def find_config_txt(
    candidates: tuple[Path, ...] = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt")),
) -> Path | None:
    """Return the first existing config.txt path."""
    for path in candidates:
        if path.exists():
            return path
    return None


def detect_board_model(
    wlanpi_model_path: Path = WLANPI_MODEL_PATH,
    dt_model_path: Path = DT_MODEL_PATH,
) -> tuple[str, bool]:
    """Return (model_name, is_go)."""
    if wlanpi_model_path.exists():
        try:
            model = wlanpi_model_path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            model = ""
        if model:
            lower = model.lower()
            is_go = "go" in lower.split() or "oscium go" in lower or "wlan pi go" in lower
            log_journal(
                f"bootcfg board detection via {wlanpi_model_path}: model={model!r} is_go={is_go}",
                priority=PRIORITY_INFO,
                GADGET_EVENT="bootcfg_detect",
            )
            return model, is_go

    if dt_model_path.exists():
        try:
            model = dt_model_path.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="ignore").strip()
        except OSError:
            model = ""
        if model:
            lower = model.lower()
            is_go = "oscium go" in lower or "wlan pi go" in lower
            log_journal(
                f"bootcfg board detection via {dt_model_path} ({wlanpi_model_path} absent): "
                f"model={model!r} is_go={is_go}",
                priority=PRIORITY_INFO,
                GADGET_EVENT="bootcfg_detect",
            )
            return model, is_go

    log_journal(
        f"bootcfg board detection: neither {wlanpi_model_path} nor {dt_model_path} present; is_go=False",
        priority=PRIORITY_INFO,
        GADGET_EVENT="bootcfg_detect",
    )
    return "unknown", False


def _read_text_preserve_nl(target: Path) -> str:
    return target.read_bytes().decode("utf-8")


def _atomic_write_text(target: Path, text: str) -> None:
    """Atomically write text to target via temp file + fsync + os.replace."""
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _is_effective_dwc2_line(raw_line: str) -> bool:
    """Match documented dtoverlay=dwc2 forms (no inline comments)."""
    code = raw_line.strip()
    if not code or code.startswith("#"):
        return False
    return code == "dtoverlay=dwc2" or code.startswith("dtoverlay=dwc2,")


def _is_deprecated_inline_dwc2_line(raw_line: str) -> bool:
    """Match legacy inline-comment form for check_bootcfg reporting only."""
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or "#" not in stripped:
        return False
    code = stripped.split("#", 1)[0].strip()
    return code == "dtoverlay=dwc2" or code.startswith("dtoverlay=dwc2,")


def _find_block_span(lines: list[str]) -> tuple[int, int] | None:
    """Return (begin_idx, end_idx) for a properly ordered BEGIN..END pair, or None."""
    try:
        begin_idx = lines.index(BEGIN_MARKER)
    except ValueError:
        return None
    for idx in range(begin_idx + 1, len(lines)):
        if lines[idx] == END_MARKER:
            return begin_idx, idx
    return None


def has_marked_block(text: str) -> bool:
    """Return True if text contains a properly ordered BEGIN..END block."""
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    return _find_block_span(lines) is not None


def has_valid_marked_block(text: str) -> bool:
    """Return True if text contains a properly ordered block with an effective dtoverlay=dwc2."""
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    span = _find_block_span(lines)
    if span is None:
        return False
    begin_idx, end_idx = span
    block_body = lines[begin_idx + 1 : end_idx]
    return any(_is_effective_dwc2_line(ln) for ln in block_body)


def has_dwc2_overlay(text: str) -> bool:
    """Return True if an uncommented dtoverlay=dwc2 line exists anywhere in text."""
    return any(_is_effective_dwc2_line(ln) for ln in text.splitlines())


def insert_marked_block(text: str) -> str:
    """Insert or repair the marked block, preserving existing line endings (LF or CRLF)."""
    if has_valid_marked_block(text):
        return text

    cleaned = strip_marked_block(text)
    nl = _detect_newline(cleaned if cleaned else text)
    suffix = "" if (not cleaned or cleaned.endswith("\n")) else nl
    block = nl.join(DEFAULT_BLOCK_LINES) + nl
    return f"{cleaned}{suffix}{block}"


def strip_marked_block(text: str) -> str:
    """Remove a properly ordered marked block without touching unmarked or out-of-order lines."""
    raw_lines = text.splitlines(keepends=True)
    stripped_lines = [ln.rstrip("\r\n") for ln in raw_lines]
    span = _find_block_span(stripped_lines)
    if span is None:
        return text
    begin_idx, end_idx = span
    return "".join(raw_lines[:begin_idx] + raw_lines[end_idx + 1 :])


def ensure_bootcfg(
    config_txt_path: Path | None = None,
    *,
    wlanpi_model_path: Path = WLANPI_MODEL_PATH,
    dt_model_path: Path = DT_MODEL_PATH,
    boot_state_path: Path = DEFAULT_BOOT_STATE_PATH,
) -> BootCfgResult:
    """Ensure gadget-mode config.txt block on the Go; verify-and-warn on other boards."""
    model, is_go = detect_board_model(wlanpi_model_path=wlanpi_model_path, dt_model_path=dt_model_path)
    target = config_txt_path or find_config_txt()
    if target is None or not target.exists():
        return BootCfgResult(
            is_go=is_go,
            board_model=model,
            changed=False,
            block_present=False,
            reboot_required=False,
            message="config.txt not found; skipping bootcfg ensure",
        )

    original = _read_text_preserve_nl(target)
    if not is_go:
        present = has_dwc2_overlay(original)
        hint = "" if wlanpi_model_path.exists() else " (/etc/wlanpi-model absent)"
        msg = (
            f"Board '{model}'{hint} is not identified as a WLAN Pi Go; leaving {target} untouched "
            f"(dtoverlay=dwc2 {'present' if present else 'MISSING - check wlanpi-common'})"
        )
        return BootCfgResult(
            is_go=False,
            board_model=model,
            changed=False,
            block_present=has_valid_marked_block(original),
            reboot_required=False,
            message=msg,
        )

    if has_valid_marked_block(original):
        return BootCfgResult(
            is_go=True,
            board_model=model,
            changed=False,
            block_present=True,
            reboot_required=boot_state_path.exists(),
            message=f"Marked block already present and valid in {target}",
        )

    backup = target.with_name(f"{target.name}.wlanpi-usb-gadget.bak")
    if not backup.exists():
        _atomic_write_text(backup, original)

    updated = insert_marked_block(original)
    _atomic_write_text(target, updated)
    boot_state_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(boot_state_path, "1\n")
    return BootCfgResult(
        is_go=True,
        board_model=model,
        changed=True,
        block_present=True,
        reboot_required=True,
        message=f"Added/repaired wlanpi-usb-gadget block in {target}; reboot required",
    )


def remove_bootcfg(
    config_txt_path: Path | None = None,
    *,
    wlanpi_model_path: Path = WLANPI_MODEL_PATH,
    dt_model_path: Path = DT_MODEL_PATH,
    boot_state_path: Path = DEFAULT_BOOT_STATE_PATH,
) -> BootCfgResult:
    """Remove our marked block on the Go, restoring backup byte-for-byte if unmodified."""
    model, is_go = detect_board_model(wlanpi_model_path=wlanpi_model_path, dt_model_path=dt_model_path)
    target = config_txt_path or find_config_txt()
    if target is None or not target.exists():
        return BootCfgResult(
            is_go=is_go,
            board_model=model,
            changed=False,
            block_present=False,
            reboot_required=False,
            message="config.txt not found; nothing to remove",
        )

    current = _read_text_preserve_nl(target)
    if not is_go or not has_marked_block(current):
        return BootCfgResult(
            is_go=is_go,
            board_model=model,
            changed=False,
            block_present=False,
            reboot_required=False,
            message=f"No wlanpi-usb-gadget block to remove in {target}",
        )

    stripped = strip_marked_block(current)
    backup = target.with_name(f"{target.name}.wlanpi-usb-gadget.bak")
    if backup.exists():
        bak_text = _read_text_preserve_nl(backup)
        _atomic_write_text(target, bak_text if stripped == bak_text else stripped)
        backup.unlink(missing_ok=True)
    else:
        _atomic_write_text(target, stripped)

    boot_state_path.unlink(missing_ok=True)
    return BootCfgResult(
        is_go=True,
        board_model=model,
        changed=True,
        block_present=False,
        reboot_required=False,
        message=f"Removed wlanpi-usb-gadget block from {target}",
    )


def check_bootcfg(
    config_txt_path: Path | None = None,
    *,
    wlanpi_model_path: Path = WLANPI_MODEL_PATH,
    dt_model_path: Path = DT_MODEL_PATH,
    boot_state_path: Path = DEFAULT_BOOT_STATE_PATH,
) -> BootCfgResult:
    """Inspect config.txt gadget-mode enablement without modifying anything."""
    model, is_go = detect_board_model(wlanpi_model_path=wlanpi_model_path, dt_model_path=dt_model_path)
    target = config_txt_path or find_config_txt()
    if target is None or not target.exists():
        return BootCfgResult(
            is_go=is_go,
            board_model=model,
            changed=False,
            block_present=False,
            reboot_required=False,
            message="config.txt not found",
        )

    try:
        text = _read_text_preserve_nl(target)
    except OSError as exc:
        return BootCfgResult(
            is_go=is_go,
            board_model=model,
            changed=False,
            block_present=False,
            reboot_required=False,
            message=f"Cannot read {target}: {exc}",
        )
    marked = has_valid_marked_block(text)
    deprecated_inline = (not marked) and any(_is_deprecated_inline_dwc2_line(ln) for ln in text.splitlines())
    dwc2_ok = has_dwc2_overlay(text) or deprecated_inline
    reboot_req = boot_state_path.exists()
    if is_go:
        block_desc = "present" if marked else ("deprecated inline-comment form (run bootcfg ensure)" if deprecated_inline else "missing")
        msg = (
            f"Go board ({model}): valid marked block {block_desc}, "
            f"dtoverlay=dwc2 {'present' if dwc2_ok else 'missing'}"
            + (", reboot required" if reboot_req else "")
        )
    else:
        msg = f"Non-Go board ({model}): dtoverlay=dwc2 {'present' if dwc2_ok else 'missing'} (owned by wlanpi-common)"
    return BootCfgResult(
        is_go=is_go,
        board_model=model,
        changed=False,
        block_present=marked,
        reboot_required=reboot_req,
        message=msg,
    )
