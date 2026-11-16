# wlanpi-usb-gadget

Next-generation USB Ethernet and serial gadget manager for WLAN Pi Go (Debian 13 trixie, Python 3.13+ stdlib only).

## Architecture Overview

- **Setup unit (`wlanpi-usb-gadget-setup.service`)**: `Type=oneshot`, `RemainAfterExit=yes`. Builds the configfs gadget tree, generates `/run/systemd/network/50-wlanpi-usb-gadget-*`, loads `dwc2`, and binds the UDC. `ExecStop` performs ordered configfs teardown.
- **Monitor unit (`wlanpi-usb-gadget-monitor.service`)**: `Type=notify-reload`, event-driven via `poll()` on UDC `state` and `NETLINK_ROUTE` (link + neighbour). Never tears down a healthy gadget on start or restart.
- **Bridge `usb0`**: A single bridge named `usb0` carries `198.18.42.1/24` + DHCP server (no router, no DNS), plus secondary fallback addresses `169.254.42.1/16` and `fe80::1/64`. Function netdevs (`usbncm0`, `usbecm0`, `usbrndis0`) are enslaved to `usb0`. Naming the bridge `usb0` preserves compatibility with `avahi-daemon.conf` (`allow-interfaces`) and `wlanpi-common` without modifying system files.

## CLI

```sh
wlanpi-usb-gadget status [--json]
wlanpi-usb-gadget selftest
wlanpi-usb-gadget events [--limit 50]
wlanpi-usb-gadget report > report.tgz
wlanpi-usb-gadget config check
wlanpi-usb-gadget profile list|set <legacy|ncm|ncm-ecm>|commit|revert
wlanpi-usb-gadget bootcfg ensure|remove|check
```

## Field Recovery Runbook (Laptop + USB Cable Only)

1. Plug in, wait 30 s, try `ssh wlanpi@198.18.42.1`.
2. Fallback addressing: `ssh wlanpi@wlanpi.local` or `ssh wlanpi@169.254.42.1` (Windows/macOS/iOS), or `ssh wlanpi@fe80::1%<interface>` (Linux/Android).
3. Browser status page: `http://wlanpi.local:8088/` or `http://169.254.42.1:8088/` (download `/report.tgz`).
4. USB serial console: `/dev/ttyGS0` (`COMx` on Windows, `/dev/cu.usbmodem*` on macOS, `/dev/ttyACM*` on Linux), then run `wlanpi-usb-gadget status`, `wlanpi-usb-gadget selftest`, and `wlanpi-usb-gadget events`.
5. **Replug knock**: unplug and replug the USB cable 3 times within 20 s to revert to `SAFE_PROFILE` and clear `HOLD`.
6. Out-of-band: connect over Bluetooth PAN or Wi-Fi and run `wlanpi-usb-gadget profile revert`.

## Useful Journal & Networkd Queries

```sh
journalctl -u 'wlanpi-usb-gadget-*' -b
journalctl --list-invocation -u wlanpi-usb-gadget-setup.service
journalctl GADGET_EVENT=recovery
journalctl -u wlanpi-usb-gadget-monitor -o json
networkctl status usb0
```

## Host-Side Capture Guidance

- **Windows**: `pnputil /enum-devices /connected`, `Get-NetAdapter`, `ipconfig /all`
- **macOS**: `system_profiler SPUSBDataType`, `ifconfig`, `networksetup -listallhardwareports`
- **Linux**: `lsusb -v -d 1209:`, `dmesg | tail -50`, `ip -d link`
