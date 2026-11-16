"""USB gadget composition profiles (data-driven)."""

from __future__ import annotations

from dataclasses import dataclass

VENDOR_ID = "0x1209"
PRODUCT_ID = "0x2042"
MANUFACTURER = "WLAN Pi"
PRODUCT = "WLAN Pi Go"


@dataclass(frozen=True)
class FunctionSpec:
    """A single configfs function instance."""

    kind: str  # "rndis", "ecm", "ncm", "acm"
    instance: str  # e.g. "usb0", "usb1", "gs0"
    ifname: str | None = None  # e.g. "usbrndis0", "usbecm0", "usbncm0"
    os_desc_compatible_id: str | None = None
    os_desc_sub_compatible_id: str | None = None

    @property
    def dir_name(self) -> str:
        return f"{self.kind}.{self.instance}"


@dataclass(frozen=True)
class ConfigSpec:
    """A single USB configuration (c.N) with its own function instances."""

    index: int
    description: str
    max_power_ma: int
    functions: tuple[FunctionSpec, ...]
    os_desc_target: bool = False


@dataclass(frozen=True)
class ProfileSpec:
    """Complete USB gadget composition profile."""

    name: str
    bcd_device: str
    summary: str
    configs: tuple[ConfigSpec, ...]
    os_desc_qw_sign: str = "MSFT100"
    os_desc_b_vendor_code: str = "0xcd"
    uses_os_desc: bool = True


def get_profile(name: str, *, serial_console: bool = True) -> ProfileSpec:
    """Return the ProfileSpec for `name`, optionally including CDC-ACM serial."""
    if name == "legacy":
        c1_funcs: list[FunctionSpec] = [
            FunctionSpec(
                kind="rndis",
                instance="usb0",
                ifname="usbrndis0",
                os_desc_compatible_id="RNDIS",
                os_desc_sub_compatible_id="5162001",
            )
        ]
        c2_funcs: list[FunctionSpec] = [
            FunctionSpec(kind="ecm", instance="usb0", ifname="usbecm0"),
        ]
        if serial_console:
            c2_funcs.append(FunctionSpec(kind="acm", instance="gs0"))
        return ProfileSpec(
            name="legacy",
            bcd_device="0x0100",
            summary="Config 1 (OS desc): RNDIS; Config 2: CDC-ECM"
            + (" + ACM serial" if serial_console else ""),
            configs=(
                ConfigSpec(
                    index=1,
                    description="RNDIS",
                    max_power_ma=500,
                    functions=tuple(c1_funcs),
                    os_desc_target=True,
                ),
                ConfigSpec(
                    index=2,
                    description="CDC ECM",
                    max_power_ma=500,
                    functions=tuple(c2_funcs),
                    os_desc_target=False,
                ),
            ),
            uses_os_desc=True,
        )

    if name == "ncm":
        c1_funcs = [
            FunctionSpec(
                kind="ncm",
                instance="usb0",
                ifname="usbncm0",
                os_desc_compatible_id="WINNCM",
            )
        ]
        if serial_console:
            c1_funcs.append(FunctionSpec(kind="acm", instance="gs0"))
        return ProfileSpec(
            name="ncm",
            bcd_device="0x0200",
            summary="Config 1 (OS desc WINNCM): CDC-NCM"
            + (" + ACM serial" if serial_console else ""),
            configs=(
                ConfigSpec(
                    index=1,
                    description="CDC NCM",
                    max_power_ma=500,
                    functions=tuple(c1_funcs),
                    os_desc_target=True,
                ),
            ),
            uses_os_desc=True,
        )

    if name == "ncm-ecm":
        # Note (plan 7a.2): never link one function instance into two configurations.
        # Each configuration gets its own distinct function instances.
        c1_funcs = [
            FunctionSpec(
                kind="ncm",
                instance="usb0",
                ifname="usbncm0",
                os_desc_compatible_id="WINNCM",
            )
        ]
        c2_funcs = [
            FunctionSpec(kind="ecm", instance="usb0", ifname="usbecm0"),
        ]
        if serial_console:
            c1_funcs.append(FunctionSpec(kind="acm", instance="gs0"))
            c2_funcs.append(FunctionSpec(kind="acm", instance="gs1"))
        return ProfileSpec(
            name="ncm-ecm",
            bcd_device="0x0300",
            summary="Config 1 (OS desc WINNCM): CDC-NCM; Config 2: CDC-ECM"
            + (" + ACM serial" if serial_console else ""),
            configs=(
                ConfigSpec(
                    index=1,
                    description="CDC NCM",
                    max_power_ma=500,
                    functions=tuple(c1_funcs),
                    os_desc_target=True,
                ),
                ConfigSpec(
                    index=2,
                    description="CDC ECM",
                    max_power_ma=500,
                    functions=tuple(c2_funcs),
                    os_desc_target=False,
                ),
            ),
            uses_os_desc=True,
        )

    raise ValueError(f"Unknown profile {name!r}")


def list_profiles(*, serial_console: bool = True) -> list[ProfileSpec]:
    """Return all supported profiles in display order."""
    return [get_profile(name, serial_console=serial_console) for name in ("legacy", "ncm", "ncm-ecm")]
