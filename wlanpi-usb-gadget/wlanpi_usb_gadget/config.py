"""Configuration parsing and validation for /etc/wlanpi-usb-gadget/config."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/wlanpi-usb-gadget/config")
BT_PAN_SUBNET = ipaddress.IPv4Network("198.18.43.0/24")

VALID_PROFILES = ("legacy", "ncm", "ncm-ecm")
VALID_LOG_LEVELS = ("info", "debug")


class ConfigError(ValueError):
    """Raised when configuration values are invalid."""


def _parse_bool(value: str, key: str) -> bool:
    norm = value.strip().lower()
    if norm in ("yes", "true", "1", "on"):
        return True
    if norm in ("no", "false", "0", "off"):
        return False
    raise ConfigError(f"{key}: invalid boolean value {value!r} (expected yes/no, on/off, true/false)")


def _parse_int(value: str, key: str, minimum: int = 1) -> int:
    try:
        parsed = int(value.strip(), 10)
    except ValueError as exc:
        raise ConfigError(f"{key}: invalid integer {value!r}") from exc
    if parsed < minimum:
        raise ConfigError(f"{key}: must be >= {minimum}, got {parsed}")
    return parsed


@dataclass(frozen=True)
class GadgetConfig:
    """Effective runtime configuration for wlanpi-usb-gadget."""

    profile: str = "legacy"
    safe_profile: str = "legacy"
    address: str = "198.18.42.1/24"
    bridge_name: str = "usb0"
    fallback_linklocal: bool = True
    serial_console: bool = True
    status_page: bool = True
    status_port: int = 8088
    monitor_recovery: bool = False
    reset_budget: int = 3
    reset_window_sec: int = 600
    trial_timeout_sec: int = 300
    log_level: str = "info"
    dwc2_blacklist_workaround: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def interface(self) -> ipaddress.IPv4Interface:
        return ipaddress.IPv4Interface(self.address)

    @property
    def network(self) -> ipaddress.IPv4Network:
        return self.interface.network

    def as_dict(self) -> dict[str, object]:
        return {
            "PROFILE": self.profile,
            "SAFE_PROFILE": self.safe_profile,
            "ADDRESS": self.address,
            "BRIDGE_NAME": self.bridge_name,
            "FALLBACK_LINKLOCAL": "yes" if self.fallback_linklocal else "no",
            "SERIAL_CONSOLE": "yes" if self.serial_console else "no",
            "STATUS_PAGE": "yes" if self.status_page else "no",
            "STATUS_PORT": self.status_port,
            "MONITOR_RECOVERY": "on" if self.monitor_recovery else "off",
            "RESET_BUDGET": self.reset_budget,
            "RESET_WINDOW_SEC": self.reset_window_sec,
            "TRIAL_TIMEOUT_SEC": self.trial_timeout_sec,
            "LOG_LEVEL": self.log_level,
            "DWC2_BLACKLIST_WORKAROUND": "on" if self.dwc2_blacklist_workaround else "off",
        }


def parse_env_text(text: str) -> dict[str, str]:
    """Parse systemd EnvironmentFile-style KEY=value text."""
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"line {lineno}: expected KEY=value, got {raw_line!r}")
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            raise ConfigError(f"line {lineno}: empty key")
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        values[key] = val
    return values


def validate_config(raw: dict[str, str]) -> GadgetConfig:
    """Validate raw key/value pairs and return an effective GadgetConfig."""
    warnings: list[str] = []
    known_keys = {
        "PROFILE",
        "SAFE_PROFILE",
        "ADDRESS",
        "BRIDGE_NAME",
        "FALLBACK_LINKLOCAL",
        "SERIAL_CONSOLE",
        "STATUS_PAGE",
        "STATUS_PORT",
        "MONITOR_RECOVERY",
        "RESET_BUDGET",
        "RESET_WINDOW_SEC",
        "TRIAL_TIMEOUT_SEC",
        "LOG_LEVEL",
        "DWC2_BLACKLIST_WORKAROUND",
    }
    for key in sorted(raw):
        if key not in known_keys:
            warnings.append(f"Unknown configuration key ignored: {key}")

    profile = raw.get("PROFILE", "legacy").strip()
    if profile not in VALID_PROFILES:
        raise ConfigError(f"PROFILE: invalid profile {profile!r} (valid: {', '.join(VALID_PROFILES)})")

    safe_profile = raw.get("SAFE_PROFILE", "legacy").strip()
    if safe_profile not in VALID_PROFILES:
        raise ConfigError(
            f"SAFE_PROFILE: invalid profile {safe_profile!r} (valid: {', '.join(VALID_PROFILES)})"
        )

    address_raw = raw.get("ADDRESS", "198.18.42.1/24").strip()
    try:
        iface = ipaddress.IPv4Interface(address_raw)
    except ValueError as exc:
        raise ConfigError(f"ADDRESS: invalid IPv4 CIDR {address_raw!r}") from exc
    if iface.network.prefixlen > 30:
        raise ConfigError(f"ADDRESS: prefix length /{iface.network.prefixlen} is too small for DHCP pool")
    if iface.ip == iface.network.network_address or iface.ip == iface.network.broadcast_address:
        raise ConfigError(f"ADDRESS: host IP {iface.ip} cannot be network or broadcast address")
    if iface.network.overlaps(BT_PAN_SUBNET):
        warnings.append(
            f"ADDRESS {address_raw} overlaps Bluetooth PAN subnet {BT_PAN_SUBNET}; "
            "consider changing ADDRESS if PAN is used"
        )

    bridge_name = raw.get("BRIDGE_NAME", "usb0").strip()
    if not bridge_name or len(bridge_name) > 15 or any(c.isspace() or c == "/" for c in bridge_name):
        raise ConfigError(f"BRIDGE_NAME: invalid Linux interface name {bridge_name!r}")
    if bridge_name != "usb0":
        warnings.append(
            f"BRIDGE_NAME={bridge_name!r} is unsupported: avahi allow-list and wlanpi-common expect 'usb0'"
        )

    fallback_linklocal = _parse_bool(raw.get("FALLBACK_LINKLOCAL", "yes"), "FALLBACK_LINKLOCAL")
    serial_console = _parse_bool(raw.get("SERIAL_CONSOLE", "yes"), "SERIAL_CONSOLE")
    status_page = _parse_bool(raw.get("STATUS_PAGE", "yes"), "STATUS_PAGE")
    status_port = _parse_int(raw.get("STATUS_PORT", "8088"), "STATUS_PORT", minimum=1)
    if status_port > 65535:
        raise ConfigError(f"STATUS_PORT: must be <= 65535, got {status_port}")

    monitor_recovery = _parse_bool(raw.get("MONITOR_RECOVERY", "off"), "MONITOR_RECOVERY")
    reset_budget = _parse_int(raw.get("RESET_BUDGET", "3"), "RESET_BUDGET", minimum=1)
    reset_window_sec = _parse_int(raw.get("RESET_WINDOW_SEC", "600"), "RESET_WINDOW_SEC", minimum=10)
    trial_timeout_sec = _parse_int(raw.get("TRIAL_TIMEOUT_SEC", "300"), "TRIAL_TIMEOUT_SEC", minimum=10)

    log_level = raw.get("LOG_LEVEL", "info").strip().lower()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL: invalid value {log_level!r} (expected info or debug)")

    dwc2_workaround = _parse_bool(
        raw.get("DWC2_BLACKLIST_WORKAROUND", "off"), "DWC2_BLACKLIST_WORKAROUND"
    )

    return GadgetConfig(
        profile=profile,
        safe_profile=safe_profile,
        address=str(iface),
        bridge_name=bridge_name,
        fallback_linklocal=fallback_linklocal,
        serial_console=serial_console,
        status_page=status_page,
        status_port=status_port,
        monitor_recovery=monitor_recovery,
        reset_budget=reset_budget,
        reset_window_sec=reset_window_sec,
        trial_timeout_sec=trial_timeout_sec,
        log_level=log_level,
        dwc2_blacklist_workaround=dwc2_workaround,
        warnings=tuple(warnings),
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> GadgetConfig:
    """Load and validate configuration from disk, returning defaults if missing."""
    if not path.exists():
        return validate_config({})
    text = path.read_text(encoding="utf-8")
    raw = parse_env_text(text)
    return validate_config(raw)
