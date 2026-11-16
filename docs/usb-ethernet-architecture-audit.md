---
title: WLAN Pi USB Ethernet gadget architecture audit
date: 2026-11-15
branch: jsnyder81/elkhorn at cd12f0b
package: wlanpi-usb-ethernet 1.5.4
note: read-only static audit; no hardware or host-OS testing performed
---

# wlanpi-usb-ethernet architectural audit

No repo files were changed by the audit itself. Paths are relative to the repo root. `gadget.sh` means `usr/bin/usb-ethernet-gadget.sh`.

**How to read the confidence labels**

- **Confirmed**: reproduced with a local test or follows directly from the code.
- **Code-reasoned**: follows from the code plus kernel/host behaviour I know but could not run here. No hardware, configfs or host OS was available.
- **Verify on hardware**: plausible and worth a test before acting.

## Summary

The package works by building a two-configuration configfs gadget (ECM in `c.1`, RNDIS in `c.2`) and then running a 1,750-line bash poll loop that guesses host health from ping/ARP and rebuilds the gadget when it thinks the link is dead.

The biggest stability risks are not in gadget creation but in the watchdog and packaging around it:

1. **Host-swap reset loop.** After any ECM host has connected once, plugging into a Windows (RNDIS) host without rebooting resets the gadget every few seconds, indefinitely.
2. **Reset every ~50 s while unplugged**, with a log line every 2 s to a file that has no logrotate.
3. **`postinst` adds a duplicate `dtoverlay=dwc2` on every install or upgrade** (broken awk range, confirmed).
4. **MAC strings are malformed** and only work because the kernel parser is lenient; the lowest serial byte is dropped.
5. **Linux hosts very likely land on RNDIS (usb1), not ECM (usb0)** as the README says, so they are unmonitored and depend on a protocol Linux is trying to retire.
6. **USB host mode depends on this service**, because the package blacklists `dwc2` and only this script loads it.

## Flaws identified

### Architecture

| # | Flaw | Evidence | Confidence |
|---|------|----------|------------|
| A1 | Setup and watchdog are one process. Any restart of the monitor (crash, `systemctl restart`, config edit) re-runs `setup_gadget` → `cleanup_gadget`, forcing a USB re-enumeration on a healthy host link. | `gadget.sh:1747-1748`, `1159`; unit is `Type=simple`, `Restart=always` (`usb-ethernet-gadget.service:10-13`) | Confirmed |
| A2 | Health is inferred by polling L3 (ping + ARP of a cached IP) rather than from USB/link events. Hosts that drop ICMP (Windows firewall on an "unidentified network", macOS stealth mode) can only be judged by ARP state, which is why usb1 was left unmonitored (commit `5e08cf4`). | `gadget.sh:780-852`, `85` | Confirmed |
| A3 | Monitoring is asymmetric: only `usb0` is watched, but the reset it triggers tears down both functions. A host on usb1 is invisible to the watchdog yet can be reset by usb0's state. | `gadget.sh:85`, `1404-1414`, `1254-1262` | Confirmed |
| A4 | Two interfaces carry the same address and DHCP server (`198.18.42.1/24` on usb0 and usb1). It works only because one has carrier at a time. | `etc/systemd/network/usb0.network:5`, `usb1.network:5` | Confirmed |
| A5 | `dwc2` is blacklisted globally and loaded only by this script. In host mode (`dr_mode=host`) nothing else loads it, so USB host depends on this gadget service running and reaching line 1176. If `libcomposite` fails, the script exits at 1163 first. | `etc/modprobe.d/wlanpi-usb-gadget.conf:9`, `gadget.sh:1163`, `1176` | Code-reasoned |
| A6 | The "load dwc2 after configfs" fix only applies when the kernel builds dwc2 as a module. With a built-in dwc2 the blacklist and `modprobe` are no-ops, and nothing checks or logs which case applies outside the optional boot-timing log. | `wlanpi-usb-gadget.conf:1-9`, `gadget.sh:1176-1181`, `362-367` | Confirmed |
| A7 | No teardown path. There is no `ExecStop` or trap, so stopping the service or removing the package leaves the gadget bound and the state file stale until reboot. | `usb-ethernet-gadget.service`, `debian/postrm:18` | Confirmed |
| A8 | Configuration lives in the script body in `/usr/bin`. Every tuning change is lost on upgrade, and the README tells users to `sed` the installed script. | `gadget.sh:85-164`, `README.md:49`, `189` | Confirmed |
| A9 | Interface names are assumed (`usb0` = ECM, `usb1` = RNDIS) from bind order. Any other `usb%d` netdev present first shifts them, and the `.network` files, `INTERFACES` and `get_interface_type` all key on the name. | `gadget.sh:313-320`, `1032`, `1042` | Code-reasoned |

### Coding

| # | Flaw | Evidence | Confidence |
|---|------|----------|------------|
| C1 | MAC strings are malformed. A serial of `10000000abcdef12` yields `0200:00:ab:cd:ef:12`. The kernel parser reads six bytes and ignores the tail, so the real MAC is `02:00:00:ab:cd:ef`: the lowest (most unique) serial byte is dropped and two bytes are constant zeros. | `gadget.sh:1003-1009` | Confirmed (string); code-reasoned (kernel parse) |
| C2 | MAC generation failure is not detected. `read ... <<< "$(generate_mac_addresses)" \|\| {...}` tests `read`, which returns 0 on empty input. Empty MACs are then written, the kernel picks random ones, and every reset presents a new MAC to the host (new adapter on Windows, new DHCP lease from a pool of 11). | `gadget.sh:1075` | Confirmed (`read` rc=0) |
| C3 | No `set -u`/`pipefail`, and no configfs write is checked. A failed `echo` or `ln -s` leaves a partial gadget that still gets bound. | `gadget.sh:1012-1058`, `1069-1083` | Confirmed |
| C4 | `cleanup_gadget` uses `rm -rf` on configfs with errors discarded. configfs needs symlinks unlinked and directories `rmdir`'d in order; attribute files cannot be unlinked. Whether the tree is really removed is unverified, and if it is not, "reset" silently becomes unbind/rebind with stale config. | `gadget.sh:992-999` | Verify on hardware |
| C5 | Wall-clock time is used for elapsed-time logic on a device with no RTC, where the README recommends `date -s` from the host. A backward jump makes `elapsed < 1` true and defers ping/no-IP reset decisions until the clock catches up. | `gadget.sh:549`, `893-899`, `1550`; `README.md:283` | Confirmed |
| C6 | `bc` is used unconditionally but is not a dependency (the header calls it optional). | `gadget.sh:1648`, `42`; `debian/control:10` | Confirmed |
| C7 | Hardcoded BCM2711 address `fe980000.usb` for the DSTS debugfs read and DWC2 state, while `dwc2_host_mode` correctly globs `*.usb`. On other SoCs, or when debugfs is not mounted this early, the DSTS signal is silently `?`. | `gadget.sh:382`, `441` vs `1123` | Confirmed |
| C8 | `USB_SUBNET` is hardcoded and not updated by `usb_update_address.sh`. After an address change, `grep -c "$USB_SUBNET"` is always 0, so the "ARP has entries but no host IP" failure path never fires. The dots are also unescaped regex. | `gadget.sh:92`, `1430`; `usr/bin/usb_update_address.sh:102-103` | Confirmed |
| C9 | The cached host IP is never invalidated except by a reset. Swapping to a host that gets a different lease costs three failed checks and a gadget reset in the middle of that host's DHCP. | `gadget.sh:720-738`, `1473-1517` | Code-reasoned |
| C10 | Dead or misleading code: `host_ip == "Address"` checks left over from `arp` parsing; `date +%s.%N \| cut -d. -f1`; duplicated serial extraction; `reset_gadget` re-implements `wait_for_udc`. | `gadget.sh:1423`, `1451`, `260`, `1076`, `1268-1275` | Confirmed |
| C11 | Documented timing is wrong. "3 checks × 2 s ≈ 6 s" ignores up to 3 × 1 s of ping timeouts per check, so real time is nearer 15 s. | `gadget.sh:110-111`, `146`, `785-786` | Confirmed |
| C12 | `usb_update_address.sh`: octets with leading zeros (`08`) raise a bash arithmetic error and pass validation; the script edits dpkg conffiles in place; it reboots instead of reloading networkd; messages cite RFC 5735 for a range defined by RFC 2544. | `usb_update_address.sh:45-50`, `102-103`, `118-120`, `30` | Confirmed |

### Implementation and packaging

| # | Flaw | Evidence | Confidence |
|---|------|----------|------------|
| I1 | **Host-swap reset loop.** `connected_once` is set once and never cleared (the state file lives in `/var/run` until reboot). When usb0 is down and `connected_once` is true, the loop sets `needs_reset` on every pass. Sequence: connect a Mac/iPhone, then move to Windows. USB state is `configured`, usb0 has no carrier, so the gadget is reset every few seconds and the RNDIS link never stabilises. | `gadget.sh:1404-1414`, `637-643`, `1539-1542` | Code-reasoned; verify on hardware |
| I2 | **Perpetual reset while unplugged.** The not-attached path ignores connection history, so a unit on a charger or power bank rebuilds the gadget every ~50 s forever. | `gadget.sh:927-936`, `1360-1371` | Confirmed |
| I3 | **Unbounded logging.** While not attached, `log_message` writes a line every 2 s to both `/var/log/usb-ethernet-gadget.log` and syslog (~43,000 lines a day each). The package ships no logrotate config. | `gadget.sh:933`, `209-213`; `debian/install` | Confirmed |
| I4 | **`postinst` duplicates `dtoverlay=dwc2` on every run.** `awk '/^\[cm4\]/,/^\[/'` ends its range on the `[cm4]` line itself, so it prints only that line, the grep never matches, and a new line is appended after `[cm4]` on each install or upgrade. | `debian/postinst:31-33` | Confirmed (awk test) |
| I5 | `postinst` comments out `otg_mode=1` in every section and adds a `[cm4]` header; `postrm` restores neither. `postrm` deletes every bare `dtoverlay=dwc2` line in the file, including ones the image or user placed outside `[cm4]`. | `debian/postinst:21`, `40`; `debian/postrm:25` | Confirmed |
| I6 | `postrm` removes a `multi-user.target.wants` symlink, but the unit is `WantedBy=sysinit.target`. If `systemctl disable` fails on the already-deleted unit, a dangling symlink remains. Stop/disable belongs in `prerm`. | `debian/postrm:18-20`; `usb-ethernet-gadget.service:18` | Confirmed (path mismatch) |
| I7 | Upgrades do not take effect until reboot. `postinst` runs `systemctl start`, not restart, so the old script keeps running. | `debian/postinst:91` | Confirmed |
| I8 | The unit and `.network` files are installed by hand into `/etc/systemd/{system,network}`, bypassing `dh_installsystemd`. Everything under `/etc` becomes a dpkg conffile, so a user who ran `usb_update_address.sh` gets a conffile prompt on upgrade. | `debian/install:1-4`; `debian/rules` | Confirmed |
| I9 | Missing dependencies: `bc`, `systemd` (networkd is required for addressing and DHCP), and the README uses `arp` (net-tools). | `debian/control:10`; `README.md:154` | Confirmed |
| I10 | README drift: says `DEBUG_MODE` defaults to `"true"` (the script has `"false"`); says Linux hosts use ECM (see X1). | `README.md:109`; `gadget.sh:158` | Confirmed |
| I11 | CI lints only `*.sh` at warning severity. It currently passes, and the maintainer scripts are covered by the executable-bit rule, but none of C1–C3 or I4 is something shellcheck catches, and there are no tests. | `.github/workflows/lint.yml:39-43` | Confirmed |
| I12 | Unit ordering is nominal. `Before=network-pre.target` on a `Type=simple` service only orders the fork, not gadget readiness, and there is no `Wants=network-pre.target`. | `usb-ethernet-gadget.service:3-4`, `10` | Confirmed |

### Cross-host-OS

| # | Concern | Evidence | Confidence |
|---|---------|----------|------------|
| X1 | **Linux hosts probably use RNDIS, not ECM.** The kernel composite driver serves the OS-descriptor config first, so `os_desc → c.2` makes RNDIS configuration index 0 (this is also why Windows works). Linux's `usb_choose_configuration` takes a first-config RNDIS when `rndis_host` is available, which is nearly every distro. So Linux lands on unmonitored usb1 and on a protocol upstream has repeatedly proposed removing. | `gadget.sh:1057`, `1078-1081`; `README.md:9` | Code-reasoned; verify with `lsusb -v` / `ip link` on a Linux host |
| X2 | Windows depends on RNDIS, which Microsoft treats as legacy. Windows 11 has an in-box NCM driver, and Windows 10 can bind it via the `WINNCM` compatible ID. | `gadget.sh:1039-1050` | Code-reasoned |
| X3 | ECM is the slowest option for macOS/iOS; both support NCM. | `gadget.sh:1029-1037` | Code-reasoned |
| X4 | No fallback when DHCP fails. `LinkLocalAddressing=no` (deliberate, commit `5422fe0`) means a host that self-assigns 169.254.x.x cannot reach the Pi at all. | `usb0.network:7` | Confirmed |
| X5 | Windows caches driver binding per VID/PID/bcdDevice. Any future change to the gadget composition must bump `bcdDevice` or the PID, or existing Windows hosts keep the old binding. | `gadget.sh:1070-1072` | Code-reasoned |
| X6 | `0x1209:0x2042` is a pid.codes VID. I did not check that PID 0x2042 is registered to WLAN Pi. | `gadget.sh:1070-1071` | Verify |
| X7 | The post-wake "turbo" path (threshold of 1 failed check) is armed after every reset, not only after a real wake, so one bad check within 15 s of a reset triggers another reset. This is tuned for iOS and applied to all hosts. | `gadget.sh:889-923`, `1550` | Code-reasoned |

## Phased recommendations

### Phase 1: quick wins and correctness fixes

| # | Fixes | What is wrong today | Why it matters across hosts | Proposed fix |
|---|-------|--------------------|-----------------------------|--------------|
| 1.1 | I1 | usb0 down + `connected_once` → reset every loop | Breaks Windows (and Linux-on-RNDIS) after any ECM host was used in the same boot | Only reset for "interface down" when usb0 previously had carrier in the current USB session; clear `connected_once` on reset and on a not-attached → configured transition. Skip usb0 checks entirely when usb1 has carrier. |
| 1.2 | I2, I3 | Reset every ~50 s when unplugged; log line every 2 s | Flash wear and disk fill on every unit left on a charger; a host plugged in mid-rebuild sees a flapping device | Do not reset in `not attached` unless the link was connected and then lost; log the wait once (or every Nth check). Ship `debian/wlanpi-usb-ethernet.logrotate`, or log to the journal only. |
| 1.3 | I4, I5 | awk range bug appends `dtoverlay=dwc2` each upgrade; `postrm` over-deletes and does not restore `otg_mode` | Corrupts boot config over time; removal can leave USB host broken | Replace the range with a section-aware awk (flag set on `[cm4]`, cleared on the next `[`). Mark the lines the package adds with a trailing comment and have `postrm` remove only marked lines and restore the `otg_mode` line it commented. |
| 1.4 | C1, C2 | Malformed MAC string, lowest serial byte dropped, failure undetected | Stable, unique MACs are what Windows/macOS/NetworkManager key adapter identity and DHCP leases on | Build the MAC from the last 5 serial bytes (`02:xx:xx:xx:xx:xx` / `06:…`), validate with a regex, check `generate_mac_addresses`' exit status directly, and fall back to a hash of `/etc/machine-id` instead of kernel-random. |
| 1.5 | C6, I9 | `bc` and `systemd` not in `Depends`; README uses `arp` | Silent log garbage; README commands fail | Replace `bc` with `$SECONDS` or `/proc/uptime` arithmetic; add `systemd` to `Depends`; change README to `ip neigh show dev usb0`. |
| 1.6 | C5 | Wall-clock elapsed time | Clock set from the host (as the README advises) can freeze reset decisions | Use bash `$SECONDS` (monotonic for the process) for all elapsed calculations. |
| 1.7 | I6, I7 | Wrong symlink path in `postrm`; no restart on upgrade | Stale enablement after removal; fixes do not apply until reboot | Move stop/disable to `prerm`; fix the path to `sysinit.target.wants`; decide deliberately whether upgrade restarts (see 2.1, which makes a restart non-disruptive). |
| 1.8 | I10, C11, X1 | README defaults and host mapping are wrong | Users debug the wrong interface | Correct `DEBUG_MODE` default, the real reset timings, and the per-OS interface table once X1 is verified on a Linux host. |
| 1.9 | C8, C12 | `USB_SUBNET` drifts from the `.network` files; weak octet validation | Address change silently disables one failure path | Derive the subnet from `ip -4 addr show` at runtime; force base 10 (`10#$octet`) in validation. |
| 1.10 | C10, C7 | Dead `"Address"` checks; hardcoded `fe980000.usb` | Noise; DSTS detection silently off on other SoCs | Delete dead code; resolve the controller with the same `*.usb` glob `dwc2_host_mode` uses. |

### Phase 2: structural improvements

| # | Fixes | What is wrong today | Why it matters across hosts | Proposed fix |
|---|-------|--------------------|-----------------------------|--------------|
| 2.1 | A1, A7, I12 | One process does setup and monitoring; no teardown | Monitor restarts drop the host link; stop/remove leaves a bound gadget | Split into `usb-gadget-setup.service` (`Type=oneshot`, `RemainAfterExit=yes`, `ExecStart` = configure + bind, `ExecStop` = ordered unbind and configfs teardown) and `usb-gadget-monitor.service` (`Requires=`/`After=` setup, `Restart=on-failure`). The monitor never rebuilds on start. |
| 2.2 | A4, A3, A9 | Same IP on two interfaces; name-based assumptions; asymmetric monitoring | Whichever function a host picks should behave identically | Bridge both functions into one `br-usb` (a `.netdev` plus one `.network` with the address and DHCP server), and match members by `Driver=g_ether`/function or set `ifname` explicitly in configfs. The monitor then watches one interface regardless of host OS. |
| 2.3 | A8 | Tunables inside `/usr/bin` script | Per-host tuning is lost on upgrade | Read `/etc/default/wlanpi-usb-ethernet` (or `/etc/wlanpi-usb-ethernet.conf`) via `EnvironmentFile=`; keep defaults in the script. |
| 2.4 | I8 | Hand-installed units in `/etc`, hand-rolled enable/start | Conffile prompts; no standard upgrade/removal handling | Ship the unit as `debian/wlanpi-usb-ethernet.usb-ethernet-gadget.service` (installed to `/usr/lib/systemd/system` by `dh_installsystemd`), ship `.network` files in `/usr/lib/systemd/network`, and have `usb_update_address.sh` write a drop-in under `/etc/systemd/network/*.network.d/` and run `networkctl reload` instead of rebooting. |
| 2.5 | C3, C4 | Unchecked configfs writes; `rm -rf` teardown | A half-built gadget enumerates differently per host and is hard to diagnose | Add `set -uo pipefail` and a `write_attr` helper that logs and fails on error; implement the documented teardown order (unbind, remove config symlinks, `os_desc` link, config strings, configs, functions, gadget strings, gadget). |
| 2.6 | A5, A6 | USB host mode depends on the gadget service; module-vs-built-in is unchecked | A failed or disabled gadget service takes USB host down with it | Log at start whether dwc2 is a module or built-in; move the `modprobe dwc2` ahead of any exit path, or load it from a tiny separate unit so host mode does not depend on gadget setup. |
| 2.7 | A1, X7 | Reset = full teardown and rebuild; turbo armed after every reset | Heavier and slower than needed; reset can beget reset | Make the first-line recovery a UDC unbind/rebind (or `soft_connect` toggle) and rebuild only if that fails. Arm the turbo window only on a real suspend → resume. Add a reset back-off (for example 5 s, 15 s, 60 s) with a cap. |
| 2.8 | C9 | Stale cached host IP | Every host swap costs a spurious reset | Clear the cached IP on any not-attached → configured transition and when carrier drops. |
| 2.9 | I11 | No tests | Regressions like I4 and C1 ship unnoticed | Extract pure functions (MAC generation, config.txt editing, reset decision) and cover them with `bats` in CI; add a postinst/postrm round-trip test on a sample `config.txt`. |

### Phase 3: longer-term hardening

| # | Fixes | What is wrong today | Why it matters across hosts | Proposed fix |
|---|-------|--------------------|-----------------------------|--------------|
| 3.1 | X1, X2, X3 | ECM + RNDIS only | RNDIS is legacy on Windows and at risk on Linux; ECM is slow on Apple hosts | Evaluate an NCM-first composition: NCM with the `WINNCM` OS-descriptor compatible ID for Windows 10 1903+/11, macOS, iOS and Linux, with RNDIS kept only as a fallback for older Windows. Bump `bcdDevice` when the composition changes (X5). This needs the test matrix in 3.4 before shipping. |
| 3.2 | A2 | Polling ping/ARP heuristics | Per-OS heuristics (iOS turbo, ICMP-blocking hosts) keep growing | Move to event-driven monitoring: carrier changes from `ip monitor link` plus the UDC `state` attribute, with ping only as a secondary signal. At this size a small Python or C daemon is more maintainable than bash. Make active resets opt-in per host class, since Windows already recovers without them. |
| 3.3 | X4 | No fallback when DHCP fails | A host stuck on 169.254.x.x has no path to the Pi | Re-evaluate enabling IPv6 link-local on the gadget link so `wlanpi.local` over mDNS works without DHCP. This reverses commit `5422fe0`, so check why it was removed first. |
| 3.4 | all | No recorded host coverage | Regressions are found by users | Maintain a host matrix (Windows 10, Windows 11, macOS Intel and Apple Silicon, iOS/iPadOS, Android, Linux with and without `rndis_host`) covering cold plug, replug, sleep/wake, host swap without reboot, and upgrade. Record which interface each host selects. |
| 3.5 | A6, C7 | BCM2711-specific paths and `[cm4]` filter | Next hardware revision silently loses features | Detect the controller and model at runtime; make the config.txt section configurable. |
| 3.6 | X6 | VID/PID provenance unchecked | Collisions cause wrong driver binding on hosts | Confirm the pid.codes registration for 0x1209:0x2042. |

## Risk matrix

| Item | Likelihood | Impact | Hosts affected | Effort |
|------|-----------|--------|----------------|--------|
| I1 host-swap reset loop | High when hosts are swapped without reboot | High: no connectivity | Windows, Linux on RNDIS | Small |
| I2/I3 unplugged resets and log growth | Certain on any unit left unplugged | Medium: disk fill, flash wear | All | Small |
| I4 config.txt duplication | Certain on every upgrade | Low now, grows over time | All | Small |
| C1/C2 MAC handling | Certain (malformed); rare (random fallback) | Medium: adapter/lease churn | Windows, macOS most | Small |
| X1 Linux on RNDIS | Likely | Medium: unmonitored, future kernel risk | Linux | Verify, then 2.2 / 3.1 |
| A5 host mode depends on gadget service | Low | High: no USB host | Host-mode units | Small |
| A1 restart drops link | Every restart or upgrade-with-restart | Medium | All | Medium |
| I5 removal leaves config.txt altered | On removal | Medium | n/a (device side) | Small |
| X2 RNDIS legacy on Windows | Low today, rising | High when it lands | Windows | Large |

## What was not verified

- Nothing was run on a WLAN Pi, against configfs, or against a real host OS.
- X1, I1 and C4 rest on kernel and host behaviour reasoned from code; each has a cheap hardware check noted above.
- I did not check the pid.codes registry (no web access was used).
