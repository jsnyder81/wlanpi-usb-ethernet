"""Configfs gadget tree planning, atomic-locked apply, and ordered teardown."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from wlanpi_usb_gadget.mac import DerivedMacs
from wlanpi_usb_gadget.profiles import (
    MANUFACTURER,
    PRODUCT,
    PRODUCT_ID,
    VENDOR_ID,
    ProfileSpec,
)

GADGET_NAME = "wlanpi-usb-gadget"
DEFAULT_CONFIGFS_ROOT = Path("/sys/kernel/config/usb_gadget")
DEFAULT_LOCK_PATH = Path("/run/wlanpi-usb-gadget/configfs.lock")
DEFAULT_HASH_PATH = Path("/run/wlanpi-usb-gadget/gadget.hash")


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


def apply_gadget(
    plan: PlannedGadget,
    udc_name: str,
    *,
    force: bool = False,
    gadget_name: str = GADGET_NAME,
    configfs_root: Path = DEFAULT_CONFIGFS_ROOT,
    hash_path: Path = DEFAULT_HASH_PATH,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> str:
    """Apply a PlannedGadget to configfs and bind UDC. Returns 'unchanged' or 'applied'."""
    with configfs_lock(lock_path):
        foreign = find_foreign_gadget(configfs_root=configfs_root, own_name=gadget_name, udc_name=udc_name)
        if foreign:
            raise ForeignGadgetError(
                f"Another gadget '{foreign}' is already bound to UDC '{udc_name}'; refusing to proceed"
            )

        gadget_dir = configfs_root / gadget_name
        if (
            not force
            and gadget_dir.exists()
            and hash_path.exists()
            and hash_path.read_text(encoding="utf-8").strip() == plan.tree_hash
            and read_bound_udc(gadget_dir) == udc_name
        ):
            return "unchanged"

        # Clean any existing/partial tree before building
        if gadget_dir.exists():
            # Inline teardown without re-acquiring lock
            udc_file = gadget_dir / "UDC"
            if udc_file.exists() and read_bound_udc(gadget_dir):
                write_attr(udc_file, "")
            _teardown_unlocked(gadget_dir, is_fake=(configfs_root != DEFAULT_CONFIGFS_ROOT))

        for rel_dir in plan.dirs:
            target_dir = gadget_dir / rel_dir if rel_dir else gadget_dir
            target_dir.mkdir(parents=True, exist_ok=True)

        for rel_attr, value in plan.attrs:
            attr_path = gadget_dir / rel_attr
            attr_path.parent.mkdir(parents=True, exist_ok=True)
            write_attr(attr_path, value)

        for link_rel, target_rel in plan.symlinks:
            link_path = gadget_dir / link_rel
            target_path = gadget_dir / target_rel
            link_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(target_path, link_path)
            except OSError as exc:
                raise ConfigfsError(f"Failed creating symlink {link_path} -> {target_path}: {exc}") from exc

        write_attr(gadget_dir / "UDC", udc_name)

        hash_path.parent.mkdir(parents=True, exist_ok=True)
        hash_path.write_text(f"{plan.tree_hash}\n", encoding="utf-8")
        return "applied"
