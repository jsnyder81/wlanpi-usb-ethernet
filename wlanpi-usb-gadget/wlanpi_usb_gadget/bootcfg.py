"""Board-scoped config.txt gadget-mode management (D16, section 7)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BEGIN_MARKER = "# BEGIN wlanpi-usb-gadget"
END_MARKER = "# END wlanpi-usb-gadget"
DEFAULT_BLOCK_LINES = (
    BEGIN_MARKER,
    "[cm4]",
    "dtoverlay=dwc2",
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


def find_config_txt(candidates: tuple[Path, ...] = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt"))) -> Path | None:
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
        model = wlanpi_model_path.read_text(encoding="utf-8", errors="ignore").strip()
        if model:
            lower = model.lower()
            is_go = "go" in lower.split() or "oscium go" in lower or "wlan pi go" in lower
            return model, is_go

    if dt_model_path.exists():
        model = dt_model_path.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="ignore").strip()
        if model:
            lower = model.lower()
            is_go = "oscium go" in lower or "wlan pi go" in lower
            return model, is_go

    return "unknown", False


def has_marked_block(text: str) -> bool:
    """Return True if text contains our marked block."""
    lines = text.splitlines()
    return BEGIN_MARKER in lines and END_MARKER in lines


def has_dwc2_overlay(text: str) -> bool:
    """Return True if an uncommented dtoverlay=dwc2 line exists."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("dtoverlay=dwc2"):
            return True
    return False


def insert_marked_block(text: str) -> str:
    """Insert or return unchanged text with exactly one marked block."""
    if has_marked_block(text):
        return text

    suffix = "" if (not text or text.endswith("\n")) else "\n"
    block = "\n".join(DEFAULT_BLOCK_LINES) + "\n"
    return f"{text}{suffix}{block}"


def strip_marked_block(text: str) -> str:
    """Remove the marked block from text without touching any unmarked lines."""
    if not has_marked_block(text):
        return text

    out_lines: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped == BEGIN_MARKER:
            inside = True
            continue
        if inside:
            if stripped == END_MARKER:
                inside = False
            continue
        out_lines.append(line)
    return "".join(out_lines)


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

    original = target.read_text(encoding="utf-8")
    if not is_go:
        present = has_dwc2_overlay(original)
        msg = (
            f"Board '{model}' is not a WLAN Pi Go; leaving {target} untouched "
            f"(dtoverlay=dwc2 {'present' if present else 'MISSING - check wlanpi-common'})"
        )
        return BootCfgResult(
            is_go=False,
            board_model=model,
            changed=False,
            block_present=has_marked_block(original),
            reboot_required=False,
            message=msg,
        )

    if has_marked_block(original):
        return BootCfgResult(
            is_go=True,
            board_model=model,
            changed=False,
            block_present=True,
            reboot_required=boot_state_path.exists(),
            message=f"Marked block already present in {target}",
        )

    backup = target.with_name(f"{target.name}.wlanpi-usb-gadget.bak")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")

    updated = insert_marked_block(original)
    target.write_text(updated, encoding="utf-8")
    boot_state_path.parent.mkdir(parents=True, exist_ok=True)
    boot_state_path.write_text("1\n", encoding="utf-8")
    return BootCfgResult(
        is_go=True,
        board_model=model,
        changed=True,
        block_present=True,
        reboot_required=True,
        message=f"Added wlanpi-usb-gadget block to {target}; reboot required",
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

    current = target.read_text(encoding="utf-8")
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
        bak_text = backup.read_text(encoding="utf-8")
        if stripped == bak_text:
            target.write_text(bak_text, encoding="utf-8")
        else:
            target.write_text(stripped, encoding="utf-8")
        backup.unlink(missing_ok=True)
    else:
        target.write_text(stripped, encoding="utf-8")

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

    text = target.read_text(encoding="utf-8")
    marked = has_marked_block(text)
    dwc2_ok = has_dwc2_overlay(text)
    reboot_req = boot_state_path.exists()
    if is_go:
        msg = (
            f"Go board ({model}): marked block {'present' if marked else 'missing'}, "
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
