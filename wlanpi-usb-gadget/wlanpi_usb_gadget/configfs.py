"""Configfs gadget tree planning, atomic-locked apply, and ordered teardown."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from wlanpi_usb_gadget.events import record_event
from wlanpi_usb_gadget.journal import PRIORITY_ERR, PRIORITY_INFO, PRIORITY_WARNING, log_journal
from wlanpi_usb_gadget.mac import DerivedMacs
from wlanpi_usb_gadget.profiles import (
    MANUFACTURER,
    PRODUCT,
    PRODUCT_ID,
    VENDOR_ID,
    ProfileSpec,
)

GADGET_NAME = "wlanpi-usb-gadget"
LEGACY_GADGET_NAME = "wlanpi"
LEGACY_GADGET_MODULES = frozenset({"g_ether", "g_ncm", "g_cdc", "g_serial", "g_multi", "g_ffs"})
DEFAULT_CONFIGFS_ROOT = Path("/sys/kernel/config/usb_gadget")
DEFAULT_LOCK_PATH = Path("/run/wlanpi-usb-gadget/configfs.lock")
DEFAULT_HASH_PATH = Path("/run/wlanpi-usb-gadget/gadget.hash")
DEFAULT_FIRST_BIND_MARKER = Path("/var/lib/wlanpi-usb-gadget/first_bind_done")
DEFAULT_SYS_CLASS_NET = Path("/sys/class/net")
DEFAULT_UDC_CLASS_DIR = Path("/sys/class/udc")


class ConfigfsError(RuntimeError):
    """Raised when any configfs write or teardown step fails."""


class ForeignGadgetError(ConfigfsError):
    """Raised when another gadget already owns the UDC."""


@dataclass(frozen=True)
class PlannedGadget:
    """Deterministic representation of the configfs gadget tree."""

    profile_name: str
    attrs: tuple[tuple[str, str], ...]
    dirs: tuple[str, ...]
    symlinks: tuple[tuple[str, str], ...]
    tree_hash: str


def write_attr(path: Path, value: str) -> None:
    """Write a single configfs attribute, raising ConfigfsError on any failure."""
    try:
        path.write_text(f"{value}\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigfsError(f"Failed writing configfs attribute {path}: {exc}") from exc


@contextmanager
def configfs_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[None]:
    """Serialize configfs mutations across CLI/monitor invocations (7a.2)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def plan_gadget_tree(
    profile: ProfileSpec,
    macs: DerivedMacs,
    serial_number: str,
) -> PlannedGadget:
    """Build a deterministic plan of directories, attributes, and symlinks."""
    dirs: list[str] = [
        "",
        "strings/0x409",
    ]
    attrs: list[tuple[str, str]] = [
        ("idVendor", VENDOR_ID),
        ("idProduct", PRODUCT_ID),
        ("bcdDevice", profile.bcd_device),
        ("bcdUSB", "0x0200"),
        ("strings/0x409/serialnumber", serial_number),
        ("strings/0x409/manufacturer", MANUFACTURER),
        ("strings/0x409/product", PRODUCT),
    ]
    if profile.b_device_class is not None:
        attrs.extend(
            [
                ("bDeviceClass", profile.b_device_class),
                ("bDeviceSubClass", "0x00"),
                ("bDeviceProtocol", "0x00"),
            ]
        )
    symlinks: list[tuple[str, str]] = []

    if profile.uses_os_desc:
        attrs.extend(
            [
                ("os_desc/use", "1"),
                ("os_desc/b_vendor_code", profile.os_desc_b_vendor_code),
                ("os_desc/qw_sign", profile.os_desc_qw_sign),
            ]
        )

    for cfg in profile.configs:
        cfg_dir = f"configs/c.{cfg.index}"
        dirs.append(cfg_dir)
        dirs.append(f"{cfg_dir}/strings/0x409")
        attrs.extend(
            [
                (f"{cfg_dir}/MaxPower", str(cfg.max_power_ma)),
                (f"{cfg_dir}/bmAttributes", "0x80"),
                (f"{cfg_dir}/strings/0x409/configuration", cfg.description),
            ]
        )
        if cfg.os_desc_target and profile.uses_os_desc:
            symlinks.append((f"os_desc/c.{cfg.index}", cfg_dir))

        for fn in cfg.functions:
            fn_dir = f"functions/{fn.dir_name}"
            dirs.append(fn_dir)
            if fn.kind in ("rndis", "ecm", "ncm"):
                attrs.extend(
                    [
                        (f"{fn_dir}/host_addr", macs.host),
                        (f"{fn_dir}/dev_addr", macs.device),
                    ]
                )
                if fn.ifname:
                    attrs.append((f"{fn_dir}/ifname", fn.ifname))
                if fn.os_desc_compatible_id:
                    attrs.append(
                        (f"{fn_dir}/os_desc/interface.{fn.kind}/compatible_id", fn.os_desc_compatible_id)
                    )
                if fn.os_desc_sub_compatible_id:
                    attrs.append(
                        (
                            f"{fn_dir}/os_desc/interface.{fn.kind}/sub_compatible_id",
                            fn.os_desc_sub_compatible_id,
                        )
                    )
            symlinks.append((f"{cfg_dir}/{fn.dir_name}", fn_dir))

    payload = json.dumps(
        {
            "profile": profile.name,
            "dirs": dirs,
            "attrs": attrs,
            "symlinks": symlinks,
        },
        sort_keys=True,
    ).encode("utf-8")
    tree_hash = hashlib.sha256(payload).hexdigest()
    return PlannedGadget(
        profile_name=profile.name,
        attrs=tuple(attrs),
        dirs=tuple(dirs),
        symlinks=tuple(symlinks),
        tree_hash=tree_hash,
    )


def find_foreign_gadget(
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    own_name: str = GADGET_NAME,
    udc_name: str | None = None,
) -> str | None:
    """Return the name of a foreign gadget bound to UDC, if any."""
    if not configfs_root.exists():
        return None
    for entry in sorted(configfs_root.iterdir()):
        if not entry.is_dir() or entry.name == own_name:
            continue
        udc_file = entry / "UDC"
        if udc_file.exists():
            bound = udc_file.read_text(encoding="utf-8", errors="ignore").strip()
            if bound and (udc_name is None or bound == udc_name):
                return entry.name
    return None


def read_bound_udc(gadget_dir: Path) -> str:
    """Return the bound UDC name for gadget_dir, or empty string if unbound."""
    udc_file = gadget_dir / "UDC"
    if not udc_file.exists():
        return ""
    try:
        return udc_file.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _teardown_unlocked(gadget_dir: Path, *, is_fake: bool = False) -> None:
    """Internal teardown helper when configfs_lock is already held."""
    def _clean_leaf_dir(d: Path) -> None:
        if is_fake and d.exists():
            for child in list(d.iterdir()):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    _clean_leaf_dir(child)
        d.rmdir()

    # 2. Remove function symlinks from configs
    configs_dir = gadget_dir / "configs"
    if configs_dir.exists():
        for cfg_dir in sorted(configs_dir.iterdir()):
            if cfg_dir.is_dir():
                for item in sorted(cfg_dir.iterdir()):
                    if item.is_symlink():
                        item.unlink()

    # 3. Remove os_desc config symlinks
    os_desc_dir = gadget_dir / "os_desc"
    if os_desc_dir.exists():
        for item in sorted(os_desc_dir.iterdir()):
            if item.is_symlink():
                item.unlink()

    # 4. Remove config strings, then configs
    if configs_dir.exists():
        for cfg_dir in sorted(configs_dir.iterdir()):
            if cfg_dir.is_dir():
                strings_dir = cfg_dir / "strings"
                if strings_dir.exists():
                    for lang_dir in sorted(strings_dir.iterdir()):
                        if lang_dir.is_dir():
                            _clean_leaf_dir(lang_dir)
                    if is_fake:
                        strings_dir.rmdir()
                _clean_leaf_dir(cfg_dir)
        if is_fake:
            configs_dir.rmdir()

    # 5. Remove functions
    functions_dir = gadget_dir / "functions"
    if functions_dir.exists():
        for fn_dir in sorted(functions_dir.iterdir()):
            if fn_dir.is_dir():
                _clean_leaf_dir(fn_dir)
        if is_fake:
            functions_dir.rmdir()

    # 6. Remove gadget strings
    gadget_strings = gadget_dir / "strings"
    if gadget_strings.exists():
        for lang_dir in sorted(gadget_strings.iterdir()):
            if lang_dir.is_dir():
                _clean_leaf_dir(lang_dir)
        if is_fake:
            gadget_strings.rmdir()

    if is_fake and os_desc_dir.exists():
        _clean_leaf_dir(os_desc_dir)

    # 7. Remove gadget directory itself
    _clean_leaf_dir(gadget_dir)


def teardown_gadget(
    gadget_name: str = GADGET_NAME,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    hash_path: Path = DEFAULT_HASH_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> bool:
    """Ordered configfs teardown (safe on partial trees). Returns True if gadget existed."""
    is_fake = configfs_root != DEFAULT_CONFIGFS_ROOT
    with configfs_lock(lock_path):
        gadget_dir = configfs_root / gadget_name
        if not gadget_dir.exists():
            if hash_path.exists():
                hash_path.unlink(missing_ok=True)
            return False

        # 1. Unbind UDC first
        udc_file = gadget_dir / "UDC"
        if udc_file.exists():
            current = read_bound_udc(gadget_dir)
            if current:
                write_attr(udc_file, "")

        _teardown_unlocked(gadget_dir, is_fake=is_fake)

        if hash_path.exists():
            hash_path.unlink(missing_ok=True)
        return True


def discover_member_ifnames(
    *,
    gadget_name: str = GADGET_NAME,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    sys_class_net: Path = DEFAULT_SYS_CLASS_NET,
    bridge_name: str = "usb0",
) -> list[str]:
    """Discover actual netdev names for Ethernet functions (works when ifname is EINVAL)."""
    found: list[str] = []
    gadget_dir = configfs_root / gadget_name

    # 1. Read functions/<kind>.<inst>/ifname from configfs (kernel populates actual name)
    functions_dir = gadget_dir / "functions"
    if functions_dir.exists():
        for fn_dir in sorted(functions_dir.iterdir()):
            if not fn_dir.is_dir():
                continue
            kind = fn_dir.name.split(".", 1)[0]
            if kind not in ("rndis", "ecm", "ncm"):
                continue
            ifname_file = fn_dir / "ifname"
            if ifname_file.exists():
                try:
                    val = ifname_file.read_text(encoding="utf-8", errors="ignore").strip()
                    if val and val != bridge_name and val not in found:
                        found.append(val)
                except OSError:
                    pass

    # 2. Cross-check /sys/class/net/*/device for gadget netdevs (e.g. usb0/usb1/usbncm0)
    if sys_class_net.exists():
        for iface_dir in sorted(sys_class_net.iterdir()):
            name = iface_dir.name
            if name == bridge_name or (iface_dir / "bridge").exists():
                continue
            dev_link = iface_dir / "device"
            if not dev_link.exists():
                continue
            try:
                resolved = str(dev_link.resolve())
            except OSError:
                continue
            is_gadget_dev = False
            uevent = dev_link / "uevent"
            if uevent.exists():
                try:
                    utext = uevent.read_text(encoding="utf-8", errors="ignore")
                    if "gadget" in utext or "g_ether" in utext:
                        is_gadget_dev = True
                except OSError:
                    pass
            if "gadget" in resolved or "udc" in resolved:
                is_gadget_dev = True
            if is_gadget_dev and name not in found:
                found.append(name)

    return found


def read_udc_holder(udc_name: str, udc_class_dir: Path = DEFAULT_UDC_CLASS_DIR) -> str:
    """Return the driver/gadget name currently holding `udc_name`, or empty string."""
    udc_dir = udc_class_dir / udc_name
    for attr in ("current_drivers", "function"):
        p = udc_dir / attr
        if p.exists():
            try:
                val = p.read_text(encoding="utf-8", errors="ignore").strip()
                if val:
                    return val.splitlines()[0].strip()
            except OSError:
                pass
    drv = udc_dir / "device" / "driver"
    if drv.is_symlink():
        try:
            return drv.resolve().name
        except OSError:
            pass
    return ""


def _reclaim_legacy_udc(
    udc_name: str,
    holder: str,
    *,
    udc_class_dir: Path = DEFAULT_UDC_CLASS_DIR,
) -> None:
    """Unbind a legacy composite gadget module (e.g. g_ether) from the UDC and rmmod it."""
    udc_attr = udc_class_dir / udc_name / "UDC"
    if udc_attr.exists():
        write_attr(udc_attr, "")
    subprocess.run(["rmmod", holder], capture_output=True, text=True, check=False)
    log_journal(
        f"Reclaimed UDC '{udc_name}' from legacy gadget module '{holder}'",
        priority=PRIORITY_WARNING,
        GADGET_EVENT="udc_reclaimed",
    )
    record_event("udc_reclaimed", udc_state="busy", detail=f"reclaimed {udc_name} from {holder}")


def check_and_clear_foreign_gadget(
    *,
    gadget_name: str = GADGET_NAME,
    udc_name: str | None = None,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    first_bind_marker: Path = DEFAULT_FIRST_BIND_MARKER,
) -> None:
    """Check for foreign configfs gadgets; tear down legacy 'wlanpi' once on fresh install."""
    is_fake = configfs_root != DEFAULT_CONFIGFS_ROOT
    foreign = find_foreign_gadget(configfs_root=configfs_root, own_name=gadget_name, udc_name=udc_name)
    if not foreign:
        return
    if foreign == LEGACY_GADGET_NAME and not first_bind_marker.exists():
        legacy_dir = configfs_root / LEGACY_GADGET_NAME
        udc_file = legacy_dir / "UDC"
        if udc_file.exists() and read_bound_udc(legacy_dir):
            write_attr(udc_file, "")
        _teardown_unlocked(legacy_dir, is_fake=is_fake)
        log_journal(
            f"Tore down leftover legacy gadget '{LEGACY_GADGET_NAME}' on first install",
            priority=PRIORITY_INFO,
            GADGET_EVENT="legacy_gadget_teardown",
        )
        record_event("legacy_gadget_teardown", detail=f"removed {LEGACY_GADGET_NAME}")
        return
    raise ForeignGadgetError(
        f"Another gadget '{foreign}' is already bound"
        + (f" to UDC '{udc_name}'" if udc_name else "")
        + "; refusing to proceed"
    )


def build_gadget_tree(
    plan: PlannedGadget,
    *,
    force: bool = False,
    gadget_name: str = GADGET_NAME,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    hash_path: Path = DEFAULT_HASH_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    first_bind_marker: Path = DEFAULT_FIRST_BIND_MARKER,
) -> str:
    """Build the complete configfs gadget tree BEFORE loading dwc2 or binding UDC.

    Returns 'unchanged' if already bound with matching hash (and not force), else 'built'.
    """
    is_fake = configfs_root != DEFAULT_CONFIGFS_ROOT
    with configfs_lock(lock_path):
        check_and_clear_foreign_gadget(
            gadget_name=gadget_name,
            configfs_root=configfs_root,
            first_bind_marker=first_bind_marker,
        )

        gadget_dir = configfs_root / gadget_name
        if (
            not force
            and gadget_dir.exists()
            and hash_path.exists()
            and hash_path.read_text(encoding="utf-8").strip() == plan.tree_hash
            and read_bound_udc(gadget_dir)
        ):
            return "unchanged"

        # Clean any existing/partial tree before building
        if gadget_dir.exists():
            udc_file = gadget_dir / "UDC"
            if udc_file.exists() and read_bound_udc(gadget_dir):
                write_attr(udc_file, "")
            _teardown_unlocked(gadget_dir, is_fake=is_fake)

        for rel_dir in plan.dirs:
            target_dir = gadget_dir / rel_dir if rel_dir else gadget_dir
            target_dir.mkdir(parents=True, exist_ok=True)

        for rel_attr, value in plan.attrs:
            attr_path = gadget_dir / rel_attr
            attr_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                write_attr(attr_path, value)
            except ConfigfsError as exc:
                cause = exc.__cause__
                if rel_attr.endswith("/ifname") and isinstance(cause, OSError) and cause.errno == errno.EINVAL:
                    log_journal(
                        f"Kernel rejected {rel_attr}={value!r} (EINVAL); using kernel-assigned name",
                        priority=PRIORITY_WARNING,
                        GADGET_EVENT="ifname_unsupported",
                    )
                    record_event("ifname_unsupported", profile=plan.profile_name, detail=f"{rel_attr}={value}")
                    continue
                raise

        for link_rel, target_rel in plan.symlinks:
            link_path = gadget_dir / link_rel
            target_path = gadget_dir / target_rel
            link_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(target_path, link_path)
            except OSError as exc:
                raise ConfigfsError(f"Failed creating symlink {link_path} -> {target_path}: {exc}") from exc

        return "built"


def bind_gadget_udc(
    plan: PlannedGadget,
    udc_name: str,
    *,
    gadget_name: str = GADGET_NAME,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    hash_path: Path = DEFAULT_HASH_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    first_bind_marker: Path = DEFAULT_FIRST_BIND_MARKER,
    udc_class_dir: Path = DEFAULT_UDC_CLASS_DIR,
) -> str:
    """Bind the already-built configfs gadget to `udc_name`, reclaiming legacy g_ether on EBUSY."""
    with configfs_lock(lock_path):
        gadget_dir = configfs_root / gadget_name
        udc_file = gadget_dir / "UDC"
        try:
            write_attr(udc_file, udc_name)
        except ConfigfsError as exc:
            cause = exc.__cause__
            if not (isinstance(cause, OSError) and cause.errno == errno.EBUSY):
                raise
            holder = read_udc_holder(udc_name, udc_class_dir=udc_class_dir)
            if holder in LEGACY_GADGET_MODULES:
                _reclaim_legacy_udc(udc_name, holder, udc_class_dir=udc_class_dir)
                write_attr(udc_file, udc_name)
            else:
                desc = holder or "unknown holder"
                log_journal(
                    f"UDC '{udc_name}' busy (held by {desc}); refusing to disturb",
                    priority=PRIORITY_ERR,
                    GADGET_EVENT="udc_busy",
                )
                record_event("udc_busy", udc_state="busy", detail=f"{udc_name} held by {desc}")
                raise ForeignGadgetError(
                    f"UDC '{udc_name}' is busy (held by {desc}); refusing to proceed"
                ) from exc

        hash_path.parent.mkdir(parents=True, exist_ok=True)
        hash_path.write_text(f"{plan.tree_hash}\n", encoding="utf-8")
        if not first_bind_marker.exists():
            first_bind_marker.parent.mkdir(parents=True, exist_ok=True)
            first_bind_marker.write_text("1\n", encoding="utf-8")
        return "applied"


def apply_gadget(
    plan: PlannedGadget,
    udc_name: str,
    *,
    force: bool = False,
    gadget_name: str = GADGET_NAME,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    hash_path: Path = DEFAULT_HASH_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
    first_bind_marker: Path = DEFAULT_FIRST_BIND_MARKER,
    udc_class_dir: Path = DEFAULT_UDC_CLASS_DIR,
    before_bind: Callable[[], None] | None = None,
) -> str:
    """Build configfs tree, run optional `before_bind` hook (e.g. load dwc2), then bind UDC."""
    status = build_gadget_tree(
        plan,
        force=force,
        gadget_name=gadget_name,
        configfs_root=configfs_root,
        hash_path=hash_path,
        lock_path=lock_path,
        first_bind_marker=first_bind_marker,
    )
    if status == "unchanged":
        return "unchanged"
    if before_bind is not None:
        before_bind()
    return bind_gadget_udc(
        plan,
        udc_name,
        gadget_name=gadget_name,
        configfs_root=configfs_root,
        hash_path=hash_path,
        lock_path=lock_path,
        first_bind_marker=first_bind_marker,
        udc_class_dir=udc_class_dir,
    )
