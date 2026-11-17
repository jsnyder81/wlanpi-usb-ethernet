"""Generate runtime systemd-networkd and udev link files in /run/systemd/network."""

from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

from wlanpi_usb_gadget.config import GadgetConfig
from wlanpi_usb_gadget.journal import PRIORITY_WARNING, log_journal
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
    *,
    member_ifnames: list[str] | None = None,
) -> dict[str, str]:
    """Render generated .network and .link files as {filename: content}.

    The bridge netdev itself is created and enslaved via ``ip link`` after UDC
    bind rather than via a ``.netdev`` file, avoiding systemd-networkd's
    ``EEXIST`` creation race when networkd is already running. Networkd owns
    the bridge's IP address, DHCP server, and fallback link-local addresses.
    """
    files: dict[str, str] = {}

    # 1. Bridge .network (systemd 257-safe: no KeepConfiguration= or IPForward=)
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
    if member_ifnames is not None:
        for name in member_ifnames:
            if name and name != cfg.bridge_name and name not in ifnames:
                ifnames.append(name)
    else:
        for c in profile.configs:
            for fn in c.functions:
                if fn.ifname:
                    resolved = fn.ifname.replace("%d", "0")
                    if resolved != cfg.bridge_name and resolved not in ifnames:
                        ifnames.append(resolved)

    for ifname in ifnames:
        files[f"{FILE_PREFIX}-member-{ifname}.network"] = (
            "[Match]\n"
            f"Name={ifname}\n"
            "Kind=!bridge\n\n"
            "[Network]\n"
            "ConfigureWithoutCarrier=yes\n"
            "LinkLocalAddressing=no\n"
            "IPv6AcceptRA=no\n"
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
    *,
    member_ifnames: list[str] | None = None,
) -> list[Path]:
    """Write generated networkd files to network_dir and prune stale ones."""
    network_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_networkd_files(cfg, profile, bridge_mac, member_ifnames=member_ifnames)

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


def ensure_bridge_exists(
    bridge_name: str,
    bridge_mac: str,
    *,
    sys_class_net: Path = Path("/sys/class/net"),
) -> None:
    """Create the bridge netdev after UDC bind if it does not already exist.

    With ``%d`` ifnames, gadget functions never take ``usb0``. In the fallback
    path where ``ifname`` is rejected and the kernel assigns ``usb0`` to a
    non-bridge function netdev, rename it to ``usbgadget0`` first before
    creating the ``usb0`` bridge.
    """
    iface_dir = sys_class_net / bridge_name
    if iface_dir.exists():
        if (iface_dir / "bridge").exists():
            return
        # A non-bridge netdev occupies bridge_name (fallback usb%d collision); move it aside.
        r1 = subprocess.run(["ip", "link", "set", "dev", bridge_name, "down"], capture_output=True, text=True, check=False)
        if r1.returncode != 0:
            log_journal(
                f"ip link set {bridge_name} down failed (rc={r1.returncode}): {r1.stderr.strip()}",
                priority=PRIORITY_WARNING,
                GADGET_EVENT="bridge_setup_warn",
            )
        r2 = subprocess.run(
            ["ip", "link", "set", "dev", bridge_name, "name", "usbgadget0"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r2.returncode != 0:
            log_journal(
                f"ip link rename {bridge_name}->usbgadget0 failed (rc={r2.returncode}): {r2.stderr.strip()}",
                priority=PRIORITY_WARNING,
                GADGET_EVENT="bridge_setup_warn",
            )

    r3 = subprocess.run(
        ["ip", "link", "add", "name", bridge_name, "address", bridge_mac, "type", "bridge", "stp_state", "0"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r3.returncode != 0:
        log_journal(
            f"ip link add bridge {bridge_name} failed (rc={r3.returncode}): {r3.stderr.strip()}",
            priority=PRIORITY_WARNING,
            GADGET_EVENT="bridge_setup_warn",
        )


def enslave_and_up_bridge(bridge_name: str, member_ifnames: list[str] | tuple[str, ...]) -> None:
    """Explicitly enslave gadget member netdevs to the bridge and bring all links UP.

    Ensures member netdevs are IFF_UP immediately after UDC bind so f_ecm/f_rndis
    signal carrier to the host without waiting on systemd-networkd.
    """
    for member in member_ifnames:
        if not member or member == bridge_name:
            continue
        r = subprocess.run(
            ["ip", "link", "set", "dev", member, "master", bridge_name, "up"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            log_journal(
                f"ip link set {member} master {bridge_name} up failed (rc={r.returncode}): {r.stderr.strip()}",
                priority=PRIORITY_WARNING,
                GADGET_EVENT="bridge_setup_warn",
            )

    r_br = subprocess.run(
        ["ip", "link", "set", "dev", bridge_name, "up"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r_br.returncode != 0:
        log_journal(
            f"ip link set {bridge_name} up failed (rc={r_br.returncode}): {r_br.stderr.strip()}",
            priority=PRIORITY_WARNING,
            GADGET_EVENT="bridge_setup_warn",
        )


def reload_networkd() -> None:
    """Reload systemd-networkd configuration."""
    subprocess.run(["networkctl", "reload"], capture_output=True, text=True, check=False)
