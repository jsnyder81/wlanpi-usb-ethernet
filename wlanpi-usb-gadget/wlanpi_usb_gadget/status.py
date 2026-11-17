"""Status collection, selftest checks, and best-effort host OS guess."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from wlanpi_usb_gadget import __version__
from wlanpi_usb_gadget.bootcfg import check_bootcfg
from wlanpi_usb_gadget.config import ConfigError, GadgetConfig, load_config
from wlanpi_usb_gadget.configfs import (
    DEFAULT_CONFIGFS_ROOT,
    GADGET_NAME,
    discover_member_ifnames,
    find_foreign_gadget,
    read_bound_udc,
)
from wlanpi_usb_gadget.events import read_events
from wlanpi_usb_gadget.mac import MacDerivationError, derive_all_macs
from wlanpi_usb_gadget.netcfg import DEFAULT_NETWORK_DIR, FILE_PREFIX
from wlanpi_usb_gadget.udc import detect_controller, list_udcs, read_udc_attr

DEFAULT_STATE_PATH = Path("/run/wlanpi-usb-gadget/state.json")
AVAHI_CONF_PATH = Path("/etc/avahi/avahi-daemon.conf")
NETIF_LEASES_DIR = Path("/run/systemd/netif/leases")


@dataclass(frozen=True)
class SelftestCheck:
    """Single selftest check result."""

    name: str
    status: str  # "pass", "warn", "fail", "info"
    detail: str
    hint: str = ""


def write_runtime_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    """Atomically write monitor runtime state JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def read_runtime_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Read monitor runtime state JSON, returning empty dict if missing/corrupt."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_netdev_info(ifname: str) -> dict[str, Any]:
    net_dir = Path("/sys/class/net") / ifname
    if not net_dir.exists():
        return {"name": ifname, "present": False}
    info: dict[str, Any] = {"name": ifname, "present": True}
    for key, attr in (
        ("carrier", "carrier"),
        ("operstate", "operstate"),
        ("mac", "address"),
        ("rx_packets", "statistics/rx_packets"),
        ("tx_packets", "statistics/tx_packets"),
    ):
        p = net_dir / attr
        if p.exists():
            try:
                val = p.read_text(encoding="utf-8", errors="ignore").strip()
                info[key] = int(val) if key in ("carrier", "rx_packets", "tx_packets") else val
            except (OSError, ValueError):
                pass
    return info


def _read_bridge_addrs(bridge_name: str) -> list[str]:
    try:
        res = subprocess.run(["ip", "-j", "addr", "show", "dev", bridge_name], capture_output=True, text=True, check=False)
    except OSError:
        return []
    if res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        parsed = json.loads(res.stdout)
        addrs: list[str] = []
        for iface in parsed:
            for ai in iface.get("addr_info", []):
                local = ai.get("local")
                plen = ai.get("prefixlen")
                if local is not None and plen is not None:
                    addrs.append(f"{local}/{plen}")
        return addrs
    except json.JSONDecodeError:
        return []


def _read_neighbours(bridge_name: str) -> list[dict[str, str]]:
    try:
        res = subprocess.run(["ip", "-j", "neigh", "show", "dev", bridge_name], capture_output=True, text=True, check=False)
    except OSError:
        return []
    if res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        parsed = json.loads(res.stdout)
        out: list[dict[str, str]] = []
        for n in parsed:
            out.append(
                {
                    "dst": str(n.get("dst", "")),
                    "lladdr": str(n.get("lladdr", "")),
                    "state": ",".join(n.get("state", [])),
                }
            )
        return out
    except json.JSONDecodeError:
        return []


def _read_dhcp_leases(leases_dir: Path = NETIF_LEASES_DIR) -> list[dict[str, str]]:
    if not leases_dir.exists():
        return []
    leases: list[dict[str, str]] = []
    try:
        entries = sorted(leases_dir.iterdir())
    except OSError:
        return []
    for lf in entries:
        if not lf.is_file():
            continue
        try:
            for line in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("ADDRESS=") or line.startswith("CLIENTID=") or line.startswith("HOSTNAME="):
                    pass
            kv: dict[str, str] = {}
            for line in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
            if "ADDRESS" in kv:
                leases.append(
                    {
                        "address": kv.get("ADDRESS", ""),
                        "hostname": kv.get("HOSTNAME", ""),
                        "client_id": kv.get("CLIENTID", ""),
                    }
                )
        except OSError:
            continue
    return leases


def guess_host(
    active_iface: str,
    leases: list[dict[str, str]],
    udc_speed: str,
    profile_name: str,
) -> str:
    """Best-effort host OS guess with supporting evidence."""
    hostnames = [l["hostname"] for l in leases if l.get("hostname")]
    hn_note = f"; hostname {hostnames[0]}" if hostnames else ""
    speed_note = f"; speed {udc_speed}" if udc_speed and udc_speed != "UNKNOWN" else ""

    if active_iface == "usbrndis0":
        if hostnames and hostnames[0].upper().startswith("DESKTOP-"):
            return f"Windows (likely, best-effort): activated RNDIS{hn_note}{speed_note}"
        return f"Windows or Linux (best-effort): activated RNDIS ({profile_name}){hn_note}{speed_note}"
    if active_iface == "usbncm0":
        if hostnames and hostnames[0].upper().startswith("DESKTOP-"):
            return f"Windows 10/11 (likely, best-effort): activated NCM{hn_note}{speed_note}"
        return f"Windows 10+/macOS/iOS/Linux (best-effort): activated NCM{hn_note}{speed_note}"
    if active_iface == "usbecm0":
        return f"macOS/iOS/Linux (likely, best-effort): activated CDC-ECM{hn_note}{speed_note}"
    return "unknown (no active function carrier)"


def _read_configfs_summary(configfs_root: Path = DEFAULT_CONFIGFS_ROOT) -> dict[str, Any]:
    gadget_dir = configfs_root / GADGET_NAME
    if not gadget_dir.exists():
        return {"present": False, "bound_udc": ""}
    summary: dict[str, Any] = {
        "present": True,
        "bound_udc": read_bound_udc(gadget_dir),
    }
    for attr in ("idVendor", "idProduct", "bcdDevice"):
        p = gadget_dir / attr
        if p.exists():
            try:
                summary[attr] = p.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                pass
    configs: list[dict[str, Any]] = []
    cfg_root = gadget_dir / "configs"
    os_desc_targets: set[str] = set()
    os_desc_dir = gadget_dir / "os_desc"
    if os_desc_dir.exists():
        try:
            for item in os_desc_dir.iterdir():
                if item.is_symlink():
                    os_desc_targets.add(item.name)
        except OSError:
            pass
    if cfg_root.exists():
        try:
            cfg_dirs = sorted(cfg_root.iterdir())
        except OSError:
            cfg_dirs = []
        for c in cfg_dirs:
            if not c.is_dir():
                continue
            try:
                funcs = sorted(x.name for x in c.iterdir() if x.is_symlink())
            except OSError:
                funcs = []
            configs.append({"name": c.name, "functions": funcs, "os_desc": c.name in os_desc_targets})
    summary["configs"] = configs
    return summary


def run_selftest(
    cfg: GadgetConfig | None = None,
    *,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    network_dir: Path = DEFAULT_NETWORK_DIR,
    avahi_conf: Path = AVAHI_CONF_PATH,
) -> list[SelftestCheck]:
    """Run diagnostic checks with pass/warn/fail/info and actionable hints."""
    checks: list[SelftestCheck] = []

    # 1. Config validity
    if cfg is None:
        try:
            cfg = load_config()
            checks.append(SelftestCheck("config_valid", "pass", f"profile={cfg.profile}, address={cfg.address}"))
        except ConfigError as exc:
            checks.append(SelftestCheck("config_valid", "fail", str(exc), "Fix /etc/wlanpi-usb-gadget/config"))
            cfg = GadgetConfig()
    else:
        checks.append(SelftestCheck("config_valid", "pass", f"profile={cfg.profile}, address={cfg.address}"))

    for w in cfg.warnings:
        checks.append(SelftestCheck("config_warning", "warn", w))

    # 2. Controller + dwc2 mode
    ctrl = detect_controller()
    if ctrl.controller:
        status = "pass" if ctrl.dr_mode in ("peripheral", "otg") else "info"
        checks.append(
            SelftestCheck(
                "controller_dr_mode",
                status,
                f"controller={ctrl.controller}, dr_mode={ctrl.dr_mode}, dwc2={ctrl.dwc2_mode}",
            )
        )
    else:
        checks.append(
            SelftestCheck(
                "controller_dr_mode",
                "warn",
                f"no dwc2 controller bound (dwc2={ctrl.dwc2_mode})",
                "Check dtoverlay=dwc2 in config.txt and reboot",
            )
        )

    # 3. configfs mounted
    if configfs_root.exists():
        checks.append(SelftestCheck("configfs_mounted", "pass", str(configfs_root)))
    else:
        checks.append(
            SelftestCheck(
                "configfs_mounted",
                "fail",
                f"{configfs_root} missing",
                "Ensure sys-kernel-config.mount is active and libcomposite is loaded",
            )
        )

    # 4. Foreign gadget
    udcs = list_udcs()
    foreign = find_foreign_gadget(configfs_root=configfs_root, own_name=GADGET_NAME)
    if foreign:
        checks.append(
            SelftestCheck(
                "no_foreign_gadget",
                "fail",
                f"foreign gadget '{foreign}' is bound",
                "Remove/stop wlanpi-usb-ethernet or tear down the foreign gadget",
            )
        )
    else:
        checks.append(SelftestCheck("no_foreign_gadget", "pass", f"udcs={udcs or ['none']}"))

    # 5. MAC derivation
    try:
        macs = derive_all_macs()
        checks.append(SelftestCheck("macs_valid", "pass", f"source={macs.source}, host={macs.host}"))
    except MacDerivationError as exc:
        checks.append(SelftestCheck("macs_valid", "fail", str(exc), "Ensure serial-number or machine-id exists"))

    # 6. Networkd files
    br_net = network_dir / f"{FILE_PREFIX}-bridge.network"
    if br_net.exists():
        checks.append(SelftestCheck("networkd_files", "pass", f"present in {network_dir}"))
    else:
        checks.append(
            SelftestCheck(
                "networkd_files",
                "warn",
                f"{br_net} not present",
                "Run 'wlanpi-usb-gadget apply' or start wlanpi-usb-gadget-setup.service",
            )
        )

    # 7. Bridge & fallback addresses
    br_info = _read_netdev_info(cfg.bridge_name)
    if br_info.get("present"):
        addrs = _read_bridge_addrs(cfg.bridge_name)
        checks.append(SelftestCheck("bridge_up", "pass", f"{cfg.bridge_name} addrs={addrs}"))
        if cfg.fallback_linklocal:
            has_169 = any(a.startswith("169.254.42.1/") for a in addrs)
            has_fe80 = any(a.startswith("fe80::1/") for a in addrs)
            if has_169 and has_fe80:
                checks.append(SelftestCheck("fallback_addresses", "pass", "169.254.42.1/16 and fe80::1/64 present"))
            else:
                checks.append(
                    SelftestCheck(
                        "fallback_addresses",
                        "warn",
                        f"missing fallback addr on {cfg.bridge_name}: {addrs}",
                        "Run 'networkctl reload' or 'wlanpi-usb-gadget apply'",
                    )
                )
    else:
        checks.append(
            SelftestCheck(
                "bridge_up",
                "warn",
                f"bridge {cfg.bridge_name} not present",
                "Start systemd-networkd and wlanpi-usb-gadget-setup.service",
            )
        )

    # 8. Avahi allow-interfaces & use-ipv6
    if avahi_conf.exists():
        try:
            av_text = avahi_conf.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            checks.append(SelftestCheck("avahi_allow_interfaces", "info", f"cannot read {avahi_conf}: {exc}"))
        else:
            allow_line = next(
                (
                    l.strip()
                    for l in av_text.splitlines()
                    if not l.strip().startswith("#") and l.strip().split("=", 1)[0].strip() == "allow-interfaces"
                ),
                "",
            )
            use_ipv6 = next(
                (
                    l.strip()
                    for l in av_text.splitlines()
                    if not l.strip().startswith("#") and l.strip().split("=", 1)[0].strip() == "use-ipv6"
                ),
                "use-ipv6=unset",
            )
            if allow_line:
                allowed = [x.strip() for x in allow_line.split("=", 1)[1].split(",") if x.strip()]
                if cfg.bridge_name in allowed:
                    checks.append(SelftestCheck("avahi_allow_interfaces", "pass", f"{allow_line}; {use_ipv6}"))
                else:
                    checks.append(
                        SelftestCheck(
                            "avahi_allow_interfaces",
                            "warn",
                            f"{cfg.bridge_name} missing from {allow_line}",
                            f"Add {cfg.bridge_name} to allow-interfaces in {avahi_conf} and run 'sudo systemctl reload avahi-daemon'",
                        )
                    )
            else:
                checks.append(
                    SelftestCheck("avahi_allow_interfaces", "pass", f"no allow-interfaces restriction; {use_ipv6}")
                )
    else:
        checks.append(SelftestCheck("avahi_allow_interfaces", "info", f"{avahi_conf} not found"))

    # 9. Bootcfg
    bcfg = check_bootcfg()
    if bcfg.is_go and not bcfg.block_present:
        checks.append(
            SelftestCheck(
                "bootcfg",
                "warn",
                bcfg.message,
                "Run 'wlanpi-usb-gadget bootcfg ensure' and reboot",
            )
        )
    elif bcfg.reboot_required:
        checks.append(SelftestCheck("bootcfg", "warn", bcfg.message, "Reboot to activate config.txt changes"))
    else:
        checks.append(SelftestCheck("bootcfg", "pass" if bcfg.is_go else "info", bcfg.message))

    # 10. Journal persistence (informational)
    j_dir = Path("/var/log/journal")
    try:
        persistent = j_dir.exists() and any(j_dir.iterdir())
    except OSError:
        persistent = False
    checks.append(
        SelftestCheck(
            "journal_persistence",
            "info",
            "persistent (/var/log/journal non-empty)" if persistent else "volatile (use 'wlanpi-usb-gadget events')",
        )
    )

    # 11. Old package leftovers
    leftovers = [
        p
        for p in (
            Path("/etc/modprobe.d/wlanpi-usb-gadget.conf"),
            Path("/etc/systemd/network/usb0.network"),
            Path("/etc/systemd/network/usb1.network"),
        )
        if p.exists()
    ]
    if leftovers:
        checks.append(
            SelftestCheck(
                "old_package_leftovers",
                "warn",
                f"found leftover conffiles: {', '.join(str(p) for p in leftovers)}",
                "Run 'sudo apt purge wlanpi-usb-ethernet' to clean old conffiles",
            )
        )
    else:
        checks.append(SelftestCheck("old_package_leftovers", "pass", "none"))

    return checks


def collect_status(
    cfg: GadgetConfig | None = None,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    events_path: Path | None = None,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
) -> dict[str, Any]:
    """Collect full status dictionary for CLI and HTTP status page."""
    if cfg is None:
        try:
            cfg = load_config()
        except (ConfigError, OSError) as exc:
            cfg = GadgetConfig(warnings=(str(exc),))
    rt = read_runtime_state(state_path)
    ctrl = detect_controller()
    udcs = list_udcs()
    udc_name = rt.get("udc_name") or (udcs[0] if udcs else "")
    udc_state = (read_udc_attr(udc_name, "state") if udc_name else None) or rt.get("udc_state", "unknown")
    udc_speed = (read_udc_attr(udc_name, "current_speed") if udc_name else None) or "UNKNOWN"

    discovered = discover_member_ifnames(configfs_root=configfs_root, bridge_name=cfg.bridge_name)
    candidate_names = list(dict.fromkeys([*discovered, "usbncm0", "usbecm0", "usbrndis0"]))
    netdevs = [_read_netdev_info(n) for n in candidate_names]
    netdevs = [n for n in netdevs if n.get("present")]
    active_iface = next((n["name"] for n in netdevs if n.get("carrier") == 1), rt.get("active_iface", ""))

    leases = _read_dhcp_leases()
    neighs = _read_neighbours(cfg.bridge_name)
    br_addrs = _read_bridge_addrs(cfg.bridge_name)
    ev_kwargs = {"events_path": events_path} if events_path is not None else {}
    all_events = read_events(**ev_kwargs, limit=50, newest_first=True)
    events_note = next(
        (str(e.get("detail", "")) for e in all_events if e.get("event") in ("permission_denied", "read_error")),
        "",
    )
    recoveries = [e for e in all_events if e.get("event") in ("recovery", "hold", "deadman_revert")][:5]
    selftest = [asdict(c) for c in run_selftest(cfg, configfs_root=configfs_root)]

    out: dict[str, Any] = {
        "version": __version__,
        "profile": rt.get("profile", cfg.profile),
        "profile_status": rt.get("profile_status", "committed"),
        "safe_profile": cfg.safe_profile,
        "state": rt.get("state", "UNBOUND" if not _read_configfs_summary(configfs_root).get("bound_udc") else "DETACHED"),
        "prev_state": rt.get("prev_state", ""),
        "time_in_state_sec": rt.get("time_in_state_sec", 0.0),
        "udc": {
            "name": udc_name,
            "state": udc_state,
            "speed": udc_speed,
            "dr_mode": ctrl.dr_mode,
            "dwc2_mode": ctrl.dwc2_mode,
        },
        "configfs": _read_configfs_summary(configfs_root),
        "netdevs": netdevs,
        "bridge": {
            "name": cfg.bridge_name,
            "addresses": br_addrs,
            "leases": leases,
            "neighbours": neighs,
        },
        "host_guess": guess_host(active_iface, leases, udc_speed, rt.get("profile", cfg.profile)),
        "recoveries": recoveries,
        "reset_budget_remaining": rt.get("reset_budget_remaining", cfg.reset_budget),
        "selftest": selftest,
    }
    if events_note:
        out["events_note"] = events_note
    return out


def status_exit_code(status: dict[str, Any]) -> int:
    """Return 0 online/host_mode, 1 degraded, 2 broken."""
    state = status.get("state", "UNBOUND")
    if state in ("ONLINE", "HOST_MODE"):
        return 0
    if state in ("DETACHED", "ENUMERATING", "CONFIGURED_NO_LINK", "LINK_UP", "SUSPENDED", "RECOVERING"):
        return 1
    return 2


def format_status_human(status: dict[str, Any]) -> str:
    """Render status dict into a concise human-readable report."""
    lines = [
        f"wlanpi-usb-gadget {status['version']}",
        f"Profile: {status['profile']} ({status['profile_status']}), safe={status['safe_profile']}",
        f"State: {status['state']} (prev={status.get('prev_state') or '-'}, {status.get('time_in_state_sec', 0):.1f}s)",
        (
            f"UDC: {status['udc']['name'] or '-'} state={status['udc']['state']} "
            f"speed={status['udc']['speed']} dr_mode={status['udc']['dr_mode']} dwc2={status['udc']['dwc2_mode']}"
        ),
        f"Host guess: {status['host_guess']}",
        f"Bridge ({status['bridge']['name']}): addrs={status['bridge']['addresses']}",
        f"  Leases: {status['bridge']['leases'] or 'none'}",
        f"  Neighbours: {status['bridge']['neighbours'] or 'none'}",
        f"Reset budget remaining: {status.get('reset_budget_remaining')}",
    ]
    if status.get("events_note"):
        lines.append(f"Events: {status['events_note']}")
    failed = [c for c in status.get("selftest", []) if c["status"] in ("fail", "warn")]
    if failed:
        lines.append("Selftest issues:")
        for c in failed:
            lines.append(f"  [{c['status'].upper()}] {c['name']}: {c['detail']}")
    return "\n".join(lines)
