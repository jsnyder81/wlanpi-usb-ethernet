"""Deterministic MAC address derivation and validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

SERIAL_PATH = Path("/sys/firmware/devicetree/base/serial-number")
MACHINE_ID_PATH = Path("/etc/machine-id")
MAC_REGEX = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
VALID_ROLES = ("host", "device", "bridge", "ecm", "rndis", "ncm")


class MacDerivationError(RuntimeError):
    """Raised when no stable board identifier exists or a MAC is invalid."""


@dataclass(frozen=True)
class DerivedMacs:
    """MAC addresses derived for a single gadget instance."""

    host: str
    device: str
    bridge: str
    source: str
    ecm: str = ""
    rndis: str = ""
    ncm: str = ""

    def dev_addr_for(self, kind: str) -> str:
        """Return the per-function device MAC for `kind`, falling back to `device`."""
        val = getattr(self, kind, "")
        return val or self.device


def read_board_serial(
    serial_path: Path = SERIAL_PATH,
    machine_id_path: Path = MACHINE_ID_PATH,
) -> tuple[str, str]:
    """Return (identifier, source_label) from device-tree serial or machine-id."""
    if serial_path.exists():
        try:
            raw = serial_path.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="ignore").strip()
            if raw and not all(c == "0" for c in raw):
                return raw, "devicetree-serial"
        except OSError:
            pass

    if machine_id_path.exists():
        try:
            raw = machine_id_path.read_text(encoding="utf-8", errors="ignore").strip()
            if raw and not all(c == "0" for c in raw):
                return raw, "machine-id"
        except OSError:
            pass

    raise MacDerivationError(
        "No stable board identifier found in /sys/firmware/devicetree/base/serial-number "
        "or /etc/machine-id; refusing random MAC fallback"
    )


def validate_mac(mac: str) -> str:
    """Validate lowercase colon-separated unicast MAC format."""
    if not MAC_REGEX.match(mac):
        raise MacDerivationError(f"Invalid MAC format: {mac!r}")
    first_octet = int(mac.split(":")[0], 16)
    if first_octet & 0x01:
        raise MacDerivationError(f"Multicast bit set in MAC: {mac!r}")
    return mac


def derive_mac(board_serial: str, role: str) -> str:
    """Derive a locally-administered unicast MAC from board_serial and role."""
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid MAC role {role!r}; expected one of {VALID_ROLES}")
    if not board_serial or not board_serial.strip():
        raise MacDerivationError("Empty board_serial")

    digest = hashlib.sha256(f"wlanpi-usb-gadget:{board_serial.strip()}:{role}".encode("utf-8")).digest()
    raw = bytearray(digest[:6])
    raw[0] = (raw[0] | 0x02) & 0xFE
    mac = ":".join(f"{b:02x}" for b in raw)
    return validate_mac(mac)


def derive_all_macs(
    serial_path: Path = SERIAL_PATH,
    machine_id_path: Path = MACHINE_ID_PATH,
) -> DerivedMacs:
    """Derive and validate host, device, bridge, and per-function MACs from the board identifier."""
    identifier, source = read_board_serial(serial_path=serial_path, machine_id_path=machine_id_path)
    return DerivedMacs(
        host=derive_mac(identifier, "host"),
        device=derive_mac(identifier, "device"),
        bridge=derive_mac(identifier, "bridge"),
        source=source,
        ecm=derive_mac(identifier, "ecm"),
        rndis=derive_mac(identifier, "rndis"),
        ncm=derive_mac(identifier, "ncm"),
    )
