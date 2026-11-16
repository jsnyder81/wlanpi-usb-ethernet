#!/bin/sh
set -eu

# Loopback integration test using Linux kernel dummy_hcd + configfs.
# Skips cleanly in environments without root, configfs, or dummy_hcd.

if [ "$(id -u)" -ne 0 ]; then
    echo "SKIP: dummy_hcd integration test requires root"
    exit 0
fi

if [ ! -d /sys/kernel/config/usb_gadget ]; then
    modprobe libcomposite 2>/dev/null || true
fi

if [ ! -d /sys/kernel/config/usb_gadget ]; then
    echo "SKIP: configfs usb_gadget not available"
    exit 0
fi

if ! modprobe dummy_hcd 2>/dev/null; then
    echo "SKIP: dummy_hcd kernel module not available"
    exit 0
fi

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
PKG_ROOT="$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd)"
export PYTHONPATH="${PKG_ROOT}"

cleanup() {
    python3 -m wlanpi_usb_gadget teardown >/dev/null 2>&1 || true
}
trap cleanup EXIT

python3 -m wlanpi_usb_gadget apply --force
test -d /sys/kernel/config/usb_gadget/wlanpi-usb-gadget
test -n "$(cat /sys/kernel/config/usb_gadget/wlanpi-usb-gadget/UDC)"
python3 -m wlanpi_usb_gadget status --json >/dev/null
python3 -m wlanpi_usb_gadget teardown
test ! -d /sys/kernel/config/usb_gadget/wlanpi-usb-gadget
echo "PASS: dummy_hcd loopback apply + teardown succeeded"
