"""Generate runtime systemd-networkd and udev link files in /run/systemd/network."""

from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

from wlanpi_usb_gadget.config import GadgetConfig
from wlanpi_usb_gadget.profiles import ProfileSpec

DEFAULT_NETWORK_DIR = Path("/run/systemd/network")
FILE_PREFIX = "50-wlanpi-usb-gadget"
FALLBACK_IPV4 = "169.254.42.1/16"
FALLBACK_IPV6 = "fe80::1/64"


def _compute_dhcp_pool(iface: ipaddress.IPv4Interface) -> tuple[int, int]:
    """Return (PoolOffset, PoolSize) avoiding the bridge's own host IP."""
    network = iface.network
    host_offset = int(iface.ip) - int(network.network_address)
    total_hosts = network.num_addresses - 2  # exclude network and broadcast
    if host_offset == 1:
        pool_offset = 10 if total_hosts >= 20 else 2
        pool_size = max(1, min(100, total_hosts - pool_offset + 1))
        return pool_offset, pool_size
    pool_offset = 2
    pool_size = max(1, min(100, host_offset - 2))
    return pool_offset, pool_size


def render_networkd_files(
    cfg: GadgetConfig,
    profile: ProfileSpec,
    bridge_mac: str,
) -> dict[str, str]:
    """Render all generated .netdev, .network, and .link files as {filename: content}."""
    files: dict[str, str] = {}

    # 1. Bridge .netdev
    files[f"{FILE_PREFIX}-bridge.netdev"] = (
        "[NetDev]\n"
        f"Name={cfg.bridge_name}\n"
        "Kind=bridge\n"
        f"MACAddress={bridge_mac}\n\n"
        "[Bridge]\n"
        "STP=no\n"
    )

    # 2. Bridge .network (systemd 257-safe: no KeepConfiguration= or IPForward=)
    pool_offset, pool_size = _compute_dhcp_pool(cfg.interface)
    network_lines = [
        "[Match]",
        f"Name={cfg.bridge_name}",
        "",
        "[Network]",
        f"Address={cfg.address}",
        "DHCPServer=yes",
        "ConfigureWithoutCarrier=yes",
        "LinkLocalAddressing=no",
    ]
    network_lines.extend(
        [
            "",
            "[DHCPServer]",
            f"PoolOffset={pool_offset}",
            f"PoolSize={pool_size}",
            "EmitDNS=no",
            "EmitRouter=no",
        ]
    )
    if cfg.fallback_linklocal:
        network_lines.extend(
            [
                "",
                "[Address]",
                f"Address={FALLBACK_IPV4}",
                "RouteMetric=2048",
                "",
                "[Address]",
                f"Address={FALLBACK_IPV6}",
            ]
        )
    files[f"{FILE_PREFIX}-bridge.network"] = "\n".join(network_lines) + "\n"

    # 3. Member .network files (one per unique network function ifname)
    ifnames: list[str] = []
    for c in profile.configs:
        for fn in c.functions:
            if fn.ifname and fn.ifname not in ifnames:
                ifnames.append(fn.ifname)

    for ifname in ifnames:
        files[f"{FILE_PREFIX}-member-{ifname}.network"] = (
            "[Match]\n"
            f"Name={ifname}\n\n"
            "[Network]\n"
            f"Bridge={cfg.bridge_name}\n"
            "ConfigureWithoutCarrier=yes\n"
        )

    # 4. .link file to prevent udev from renaming or randomizing member MACs (7a.1)
    if ifnames:
        files[f"{FILE_PREFIX}.link"] = (
            "[Match]\n"
            f"OriginalName={' '.join(ifnames)}\n\n"
            "[Link]\n"
            "NamePolicy=keep\n"
            "MACAddressPolicy=none\n"
        )

    return files


def write_networkd_files(
    cfg: GadgetConfig,
    profile: ProfileSpec,
    bridge_mac: str,
    network_dir: Path = DEFAULT_NETWORK_DIR,
) -> list[Path]:
    """Write generated networkd files to network_dir and prune stale ones."""
    network_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_networkd_files(cfg, profile, bridge_mac)

    for existing in network_dir.glob(f"{FILE_PREFIX}*"):
        if existing.name not in rendered:
            existing.unlink()

    written: list[Path] = []
    for name, content in rendered.items():
        target = network_dir / name
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def remove_networkd_files(network_dir: Path = DEFAULT_NETWORK_DIR) -> int:
    """Remove all generated networkd files from network_dir."""
    if not network_dir.exists():
        return 0
    removed = 0
    for existing in network_dir.glob(f"{FILE_PREFIX}*"):
        existing.unlink()
        removed += 1
    return removed


def reload_networkd() -> None:
    """Reload systemd-networkd configuration (systemd 257+ applies .netdev too)."""
    subprocess.run(["networkctl", "reload"], capture_output=True, text=True, check=False)
