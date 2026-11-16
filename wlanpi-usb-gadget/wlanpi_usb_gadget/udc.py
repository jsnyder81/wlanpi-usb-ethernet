"""USB Device Controller (UDC) and dwc2 driver helpers."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DWC2_DRIVER_DIR = Path("/sys/bus/platform/drivers/dwc2")
UDC_CLASS_DIR = Path("/sys/class/udc")
SYS_MODULE_DWC2 = Path("/sys/module/dwc2")
PROC_MODULES = Path("/proc/modules")


class UdcError(RuntimeError):
    """Raised on UDC detection or control failures."""


@dataclass(frozen=True)
class ControllerInfo:
    """Detected dwc2 controller and driver status."""

    controller: str | None
    dr_mode: str  # "peripheral", "otg", "host", "unknown"
    dwc2_mode: str  # "module", "builtin", "not_loaded"


def detect_dwc2_mode(
    sys_module_dir: Path = SYS_MODULE_DWC2,
    proc_modules: Path = PROC_MODULES,
) -> str:
    """Return whether dwc2 is loaded as a module, built-in, or not loaded."""
    if proc_modules.exists():
        for line in proc_modules.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("dwc2 "):
                return "module"
    if sys_module_dir.exists():
        # Built-in modules appear in /sys/module/<name> without appearing in /proc/modules
        return "builtin"
    return "not_loaded"


def detect_controller(
    driver_dir: Path = DWC2_DRIVER_DIR,
    sys_module_dir: Path = SYS_MODULE_DWC2,
    proc_modules: Path = PROC_MODULES,
) -> ControllerInfo:
    """Detect the dwc2 platform controller and its dr_mode."""
    dwc2_mode = detect_dwc2_mode(sys_module_dir=sys_module_dir, proc_modules=proc_modules)
    if not driver_dir.exists():
        return ControllerInfo(controller=None, dr_mode="unknown", dwc2_mode=dwc2_mode)

    candidates = sorted(p for p in driver_dir.glob("*.usb") if p.is_dir())
    if not candidates:
        return ControllerInfo(controller=None, dr_mode="unknown", dwc2_mode=dwc2_mode)

    ctrl = candidates[0]
    dr_mode_path = ctrl / "of_node" / "dr_mode"
    dr_mode = "unknown"
    if dr_mode_path.exists():
        dr_mode = (
            dr_mode_path.read_bytes()
            .replace(b"\x00", b"")
            .decode("utf-8", errors="ignore")
            .strip()
            or "unknown"
        )
    return ControllerInfo(controller=ctrl.name, dr_mode=dr_mode, dwc2_mode=dwc2_mode)


def list_udcs(udc_dir: Path = UDC_CLASS_DIR) -> list[str]:
    """Return sorted list of UDC names under /sys/class/udc."""
    if not udc_dir.exists():
        return []
    return sorted(p.name for p in udc_dir.iterdir())


def ensure_kernel_module(name: str) -> None:
    """Load kernel module via modprobe (no-op if built-in or already loaded)."""
    res = subprocess.run(["modprobe", name], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise UdcError(f"modprobe {name} failed (rc={res.returncode}): {res.stderr.strip()}")


def wait_for_udc(
    timeout_sec: float = 5.0,
    udc_dir: Path = UDC_CLASS_DIR,
    poll_interval: float = 0.1,
) -> str:
    """Wait using monotonic time until at least one UDC appears."""
    deadline = time.monotonic() + timeout_sec
    while True:
        udcs = list_udcs(udc_dir=udc_dir)
        if udcs:
            return udcs[0]
        if time.monotonic() >= deadline:
            raise UdcError(f"Timed out after {timeout_sec:.1f}s waiting for a UDC in {udc_dir}")
        time.sleep(poll_interval)


def read_udc_attr(udc_name: str, attr: str, udc_dir: Path = UDC_CLASS_DIR) -> str | None:
    """Read a single UDC sysfs attribute, returning None if missing."""
    path = udc_dir / udc_name / attr
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None


def soft_reconnect(udc_name: str, udc_dir: Path = UDC_CLASS_DIR, delay_sec: float = 0.2) -> None:
    """Toggle UDC soft_connect attribute to force host re-enumeration."""
    sc_path = udc_dir / udc_name / "soft_connect"
    if not sc_path.exists():
        raise UdcError(f"{sc_path} does not exist")
    try:
        sc_path.write_text("disconnect\n", encoding="utf-8")
        time.sleep(delay_sec)
        sc_path.write_text("connect\n", encoding="utf-8")
    except OSError as exc:
        raise UdcError(f"soft_reconnect failed on {sc_path}: {exc}") from exc
