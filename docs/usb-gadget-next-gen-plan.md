---
title: wlanpi-usb-gadget implementation plan (next-generation USB gadget for WLAN Pi Go)
date: 2026-11-15
status: draft for review
supersedes: nothing yet; intended to replace wlanpi-usb-ethernet on the Go image
inputs: docs/usb-ethernet-architecture-audit.md (static audit of wlanpi-usb-ethernet 1.5.4)
revised: 2026-11-15, folded in on-device evidence from a WLAN Pi Go (wlanpi-2f3) and a pid.codes lookup; see section 13
revised-2: 2026-11-15, added the Debian bookworm to trixie migration analysis (section 7a); trixie is the floor
revised-3: 2026-11-16, folded in a second batch of on-device evidence (E6 to E12, section 13): fallback addressing revised, gadget-mode enablement on the Go reopened, journal treated as volatile
note: no live gadget or host-OS testing has been done yet; host-behaviour claims marked "verify" are Phase 0 work
---

# wlanpi-usb-gadget implementation plan

## 1. Goal and scope

Build a new, standalone package, `wlanpi-usb-gadget`, that gives the WLAN Pi Go a USB network link to Windows, macOS, iOS/iPadOS, Android and Linux hosts, and that can be diagnosed and recovered by someone holding only a laptop and a USB cable.

In scope:

- configfs gadget creation and teardown
- addressing and DHCP on the gadget link
- link health monitoring and bounded recovery
- diagnostics (CLI, journal, status page, USB serial console)
- Debian packaging and CI

Out of scope:

- Any change to `wlanpi-usb-ethernet`. Nothing is imported, sourced or copied from it. The audit is used only as a list of failure modes to design against.
- Other gadget functions (mass storage, HID) beyond the optional serial console.
- Devices other than the WLAN Pi Go. The code detects the controller at runtime, but only the Go is a test target.

Audit IDs (A1, C1, I1, X1 and so on) refer to `docs/usb-ethernet-architecture-audit.md`.

## 2. Key design decisions

| # | Decision | Addresses |
|---|----------|-----------|
| D1 | Python 3.13 standard library only, one code base for setup, monitor and CLI. No third-party packages, so no `dh_virtualenv`. Trixie's `python3` is 3.13; bookworm's 3.11 is not a target (7a). | maintainability of a 1,750-line bash state machine |
| D2 | Three units: a oneshot **setup** unit that owns the gadget, a long-running **monitor** that never rebuilds the gadget on start, and a socket-activated **status page**. | A1, A7, I12 |
| D3 | Event-driven health: UDC `state` via `poll()`, carrier and neighbour events via rtnetlink. No ping, no cached host IP. | A2, C9, C11 |
| D4 | One bridge carries the address and DHCP server, and **the bridge itself is named `usb0`** so existing name-based consumers (the avahi allow-list, see 3.3) keep working unchanged. Function netdevs get explicit names through the configfs `ifname` attribute (`usbncm0`, `usbecm0`, `usbrndis0`) and are enslaved to it. | A3, A4, A9 |
| D5 | MACs derived by hashing the board serial with a role label, validated before use, never left to the kernel's random fallback. | C1, C2 |
| D6 | Ordered configfs teardown in `ExecStop`, with every configfs write checked. | C3, C4, A7 |
| D7 | Recovery is a ladder with a token-bucket rate limit and a hold state. The monitor ships in observe-only mode first; active recovery is added only for failure modes Phase 0 reproduces. | I1, I2, X7 |
| D8 | All tunables in `/etc/wlanpi-usb-gadget/config`. networkd files are generated from it into `/run/systemd/network`, so there is one source of truth for the address. | A8, C8, I8 |
| D9 | Journal logging with structured fields, plus a **mandatory** bounded event history on disk. The Go's journal appears to be volatile (E6), so the event history is the only record that survives a reboot. No free-running log file. | I3 |
| D10 | Monotonic time everywhere (`time.monotonic()`). | C5 |
| D11 | Compositions are named **profiles**. A profile change is a trial that auto-reverts to the last known-good profile if no host comes online (dead-man switch). | new requirement |
| D12 | `dwc2` is a loadable module on the Go (confirmed, section 13), so the setup unit builds the configfs tree first and then loads `dwc2`. Whether the package also ships a `dwc2` blacklist is decided by one Phase 0 test. Either way, USB host mode must not depend on the gadget service. No special handling for a built-in `dwc2`: the tool detects it, logs it, and carries on. | A5, A6 |
| D13 | Package upgrades restart the monitor but never re-enumerate the gadget. A new composition applies at next boot or on an explicit `apply`. | I7, and upgrades run over this link |
| D14 | **Debian 13 (trixie) is the floor.** Bookworm is not supported, and the package dependencies make that explicit. Reasoning in section 7a. | new requirement |
| D15 | **Fallback reachability is IPv4, not IPv6 mDNS.** The image's avahi has `use-ipv6=no` (E9), so the bridge carries two fixed fallback addresses: `169.254.42.1/16` (reachable by hosts that self-assigned, and published by avahi over IPv4 mDNS) and `fe80::1` (name-free path for hosts that do not self-assign). Avahi's configuration is not changed. See 3.3. | X4 |
| D16 | **On the Go, this package owns gadget-mode enablement in `config.txt`**, through a marked block, because wlanpi-common does nothing for the Go (E10). On every other board it verifies and warns only. Conditional on the Phase 0 checks in 7. | A5, I4, I5 |

The target composition (NCM-first) is deliberately **not** in this table. It is a decision point with a test gate; see section 5.

## 3. Component architecture

```mermaid
flowchart TB
    subgraph host[Host: Windows / macOS / iOS / Android / Linux]
        hnet[USB network adapter]
        hser[USB serial port, where supported]
    end

    subgraph pi[WLAN Pi Go]
        cfg[/etc/wlanpi-usb-gadget/config/]
        subgraph setup[wlanpi-usb-gadget-setup.service, oneshot]
            apply[apply: build configfs tree, bind UDC]
            gen[generate /run/systemd/network files]
            td[teardown: ordered unbind and removal, ExecStop]
        end
        subgraph kernel[Kernel]
            cfs[configfs gadget: wlanpi-usb-gadget]
            udc[dwc2 UDC]
            nd[function netdevs: usbncm0 / usbecm0 / usbrndis0]
            tty[ttyGS0]
        end
        br[bridge named usb0: IPv4 + DHCP server, fallback 169.254.42.1 and fe80::1]
        nwd[systemd-networkd]
        subgraph mon[wlanpi-usb-gadget-monitor.service, Type=notify]
            ev[event sources: UDC state poll, rtnetlink link + neigh]
            sm[state machine]
            rec[recovery ladder + rate limiter + dead-man]
        end
        st[/run/wlanpi-usb-gadget/state.json/]
        hist[/var/lib/wlanpi-usb-gadget/events.jsonl/]
        jr[(journald)]
        cli[wlanpi-usb-gadget CLI: status, report, apply, teardown, profile, selftest]
        http[status page, socket-activated, bound to usb0]
        getty[serial-getty@ttyGS0]
        avahi[avahi: wlanpi.local]
    end

    cfg --> apply
    cfg --> gen
    apply --> cfs --> udc
    cfs --> nd --> br
    cfs --> tty --> getty
    gen --> nwd --> br
    udc <--> hnet
    tty <--> hser
    udc --> ev
    nd --> ev
    ev --> sm --> rec
    rec -->|soft reconnect, rebind, re-apply| apply
    sm --> st
    sm --> hist
    sm --> jr
    st --> cli
    hist --> cli
    st --> http
    br --> avahi
    br --> http
```

### 3.1 Setup unit

`wlanpi-usb-gadget-setup.service`: `Type=oneshot`, `RemainAfterExit=yes`, `DefaultDependencies=no`, ordered `After=local-fs.target sys-kernel-config.mount systemd-modules-load.service` and `Before=systemd-networkd.service network-pre.target`, with `Wants=network-pre.target`.

- `ExecStart=wlanpi-usb-gadget apply`
- `ExecStop=wlanpi-usb-gadget teardown`

`apply` does the following, in order, and fails loudly at the first error:

1. Load config and resolve the active profile.
2. Detect the controller (`/sys/bus/platform/drivers/dwc2/*.usb`) and its `dr_mode`. In host mode, log one line and exit 0 with state `HOST_MODE`.
3. Generate `50-wlanpi-usb-gadget-bridge.netdev`, `50-wlanpi-usb-gadget-bridge.network` and one `50-wlanpi-usb-gadget-member-*.network` per function into `/run/systemd/network`. The `50-` prefix sorts ahead of a leftover `usb0.network` from the old package, so the generated file wins.
4. Compute a hash of the desired configfs tree. If a gadget with the same hash is already bound, do nothing. This makes `apply` idempotent and safe to call from the monitor or on upgrade.
5. Refuse to proceed if another gadget is bound to the UDC (for example the old package's `usb_gadget/wlanpi`), and say which one.
6. Build the tree through a single `write_attr()` helper that raises on any failed write.
7. `modprobe dwc2` (a no-op if already loaded or built-in; which case applied is logged), wait for the UDC, then bind.

`teardown` is the documented reverse order: unbind UDC, remove function symlinks from configs, remove the `os_desc` link, remove config strings, configs, functions, gadget strings, then the gadget directory. It is safe to run on a partially built tree.

### 3.2 Monitor

`wlanpi-usb-gadget-monitor.service`: `Type=notify`, `WatchdogSec=30`, `Restart=on-failure`, `RestartSec=5`, `StartLimitIntervalSec=300`, `StartLimitBurst=5`. It uses `Wants=` and `After=` on the setup unit, not `Requires=`, so it still runs and reports when setup failed.

On start it only reads current state. It never tears down or rebuilds a healthy gadget.

Event sources, all standard library:

- `select.poll()` on `/sys/class/udc/<udc>/state` (the kernel calls `sysfs_notify` on it; verify in Phase 0).
- `AF_NETLINK` / `NETLINK_ROUTE` socket subscribed to link and neighbour groups, for carrier changes on the function netdevs and neighbour entries on `usb0`.
- A slow timer (30 s) as a safety net that re-reads everything, and feeds the systemd watchdog.

State machine:

| State | Meaning | Entered when |
|-------|---------|--------------|
| `NO_UDC` | No UDC present | controller missing or driver not loaded |
| `HOST_MODE` | Controller is in host mode | `dr_mode=host` |
| `UNBOUND` | Gadget not bound | setup failed or was stopped |
| `DETACHED` | Bound, no host | UDC state `not attached` |
| `ENUMERATING` | Host present, not configured | UDC state `powered` / `default` / `addressed` |
| `CONFIGURED_NO_LINK` | Host set a configuration, no carrier | UDC `configured`, all function netdevs down |
| `LINK_UP` | Carrier on one function | netlink carrier up |
| `ONLINE` | Host is using the link | neighbour entry for the host appears on `usb0` |
| `SUSPENDED` | Host suspended the bus | UDC state `suspended` |
| `RECOVERING` | A recovery step is in progress | see below |
| `HOLD` | Recovery budget exhausted, hands off | rate limiter empty |

Recovery ladder (each step is tried once per incident, in order):

1. Wait. Most transitions resolve themselves.
2. Soft reconnect (`soft_connect` on the UDC).
3. UDC unbind and rebind.
4. Full `teardown` + `apply`.

**Kernel caveat for steps 3 and 4 (see 7a.2).** The `u_ether` net_device lifecycle was reworked and partly reverted upstream during 2026, and a syzbot-reported crash sits on exactly the path step 3 uses (`register_netdevice` called from `ncm_bind` on a UDC write). Until M0a shows otherwise on the Go kernel, treat a bind that failed partway as leaving the function instance unusable: do not retry the bind on the same instance, go straight to step 4, which removes and recreates the function directories. Step 3 is only used when the previous bind succeeded.

Rules that prevent reset storms:

- **No recovery in `DETACHED`, `SUSPENDED`, `HOST_MODE` or `NO_UDC`.** An unplugged or sleeping host is not a fault (I2).
- **No recovery because a specific interface is down while another function has carrier** (I1).
- **Token bucket:** at most 3 recovery actions per 10 minutes, with back-off of 5 s, 15 s, 60 s. When empty, enter `HOLD`.
- **`HOLD` is left only by a physical event** (UDC goes to `not attached` and back), a config change, or an explicit `wlanpi-usb-gadget recover`.
- `MONITOR_RECOVERY=off|on` in config. The default is `off` until section 8 milestone M4.

### 3.3 Addressing

- The bridge, named `usb0`, gets a fixed MAC, `ConfigureWithoutCarrier=yes`, STP off, and the configured IPv4 address with networkd's DHCP server (no router, no DNS emitted, so the host never routes through the Pi).
- Function netdevs are named by configfs `ifname` (`usbncm0`, `usbecm0`, `usbrndis0`) and matched by name in generated member files.
- A generated `50-wlanpi-usb-gadget.link` matches the member names and sets `NamePolicy=keep` and `MACAddressPolicy=none`, so udev on either systemd version neither renames the members nor replaces the MACs this package derived (7a.1).
- The generated `.network` files use only settings that exist with the same meaning in systemd 257. They do not set `KeepConfiguration=` or `IPForward=`, both of which changed between 252 and 257 (7a.1).
- Fallback when the host's DHCP client fails (X4): two fixed secondary addresses on the bridge, described under "Fallback reachability" below.

#### Fallback reachability

History of this item, because it has changed twice:

- The first draft added a fixed `169.254.42.1` secondary address and IPv6 link-local with mDNS.
- E3 (the old project disabled IPv4 link-local because its DHCP server cannot assign link-local addresses) led to dropping the 169.254 address and relying on IPv6 link-local plus mDNS.
- E9 shows that path cannot work as designed: the image's `avahi-daemon.conf` has `use-ipv6=no`, so avahi never answers mDNS over IPv6.

E3's rationale is about 169.254 as the **primary** addressing scheme, where the DHCP server would have to hand such addresses out. A fixed **secondary** address on the Pi does not involve the DHCP server at all, so it is compatible with that rationale.

| Option | What it means | Pros | Cons |
|--------|---------------|------|------|
| (i) Package sets `use-ipv6=yes` in avahi | Global avahi change | Restores the IPv6 mDNS design | Changes mDNS behaviour on every interface of the image (eth0, wlan, pan0); edits a file this package does not own, which the old package got wrong twice; needs image-maintainer sign-off. Not something a gadget package should do unilaterally. |
| (ii) Package runs its own minimal mDNS responder on the bridge | New code answering for the hostname over IPv6 on `usb0` only | No change to avahi; scoped to the gadget link | New network-facing code to write, secure and maintain; must track avahi's hostname, including conflict renames; two responders for one name is a novel arrangement. |
| (iii) Fixed IPv4 link-local secondary address on the bridge | `169.254.42.1/16` alongside the primary address | No new code, no avahi change. IPv4 mDNS is link-scoped multicast, so it works with `use-ipv6=no`, and avahi publishes the extra address because `usb0` is already in its allow-list. Also gives a fixed address that needs no name resolution. | Only helps hosts that self-assign 169.254 when DHCP fails. Expected: Windows, macOS and iOS do; Linux desktops and Android generally do not (verify in S8). A statically configured 169.254 address skips the RFC 3927 claim procedure, acceptable on a two-node link. Adds a `169.254.0.0/16` on-link route on the Pi. |

**Recommendation: (iii), plus a fixed IPv6 link-local address for the hosts (iii) does not cover.**

- `169.254.42.1/16` as a secondary address on `usb0`, with a high route metric so it never competes with link-local routes on other interfaces.
- `fe80::1/64` as a fixed link-local address on `usb0`. IPv6 link-local needs no DHCP, no avahi and no self-assignment policy on the host, so a Linux or Android host can always reach `fe80::1%<interface>` even though no name resolves to it.
- Avahi is left exactly as shipped. Option (i) is raised with the image maintainers as a possible image-level change (Q14); if they adopt it, IPv6 mDNS starts working with no change here. Option (ii) is not pursued.
- `FALLBACK_LINKLOCAL=no` removes both addresses.

What a host gets when its DHCP client fails:

| Host | Self-assigns 169.254? (verify) | Path to the Pi |
|------|-------------------------------|----------------|
| Windows, macOS, iOS | Yes | `<hostname>.local` by IPv4 mDNS, or `169.254.42.1` directly |
| Linux, Android | Usually not | `fe80::1%<interface>`; or the USB serial console |


#### Interface naming and the avahi allow-list

On the Go, `/etc/avahi/avahi-daemon.conf` contains `allow-interfaces=usb0, usb1, eth0, wlan0, wlan1, pan0`. Avahi only answers mDNS on interfaces in that list. A bridge with any other name (the earlier draft used `br-usb`) would silently lose `wlanpi.local` on the gadget link, which is the main fallback path in section 4.

| Option | What it means | Pros | Cons |
|--------|---------------|------|------|
| (a) New bridge name, package manages avahi config | Add the bridge to `allow-interfaces` | Clean, descriptive name | **Permanently rejected (E8):** the image's avahi has no include or drop-in mechanism, so this would mean editing a file owned by the image with `sed`; a single key cannot carry per-package markers; the old package got this wrong twice (changelog 1.5.2, 1.5.3). |
| (b1) No bridge; address `usb0` and `usb1` directly | Keep today's two-interface layout | Zero avahi change | Reintroduces A3/A4: duplicate address, two DHCP servers, per-function monitoring |
| (b2) **Bridge named `usb0`**, members renamed via `ifname` | The addressed interface keeps the name every consumer already knows | Zero avahi change; keeps the single-interface design; other `usb0` consumers (Q12) keep working | A bridge called `usb0` is surprising, so `status` and the README must say so; needs the `ifname` check in M0a |

**Recommendation: (b2), now with stronger evidence (E7).** The name `usb0` is load-bearing beyond avahi: `/opt/wlanpi-common/wlanpi-config-at-startup.sh` reads `ip -s link show usb0` RX counters (in `otg_link_active()`, on M4+ boards) to decide host/OTG switching, and `/usr/bin/wlanpi-stats` also references `usb0`/`usb1`. So this package must present an interface named `usb0` on every board it is ever installed on, and that interface's RX counters must reflect real gadget traffic. A bridge's counters do count frames received from its members, but that must be confirmed against both consumers in M0a. If M0a shows `ifname` or a bridge named `usb0` misbehaves, fall back to (b1) for 1.0. Option (a) is closed.

`selftest` checks that the addressed interface is in avahi's allow-list (or that no allow-list exists) and reports avahi's `use-ipv6` setting for information, and warns with the exact fix if not. It never edits the file.

### 3.4 MAC derivation

`mac(role) = sha256("wlanpi-usb-gadget:" + board_serial + ":" + role)[0:6]`, with the locally-administered bit set and the multicast bit cleared. Roles are `host`, `device` and `bridge`.

- `board_serial` comes from `/sys/firmware/devicetree/base/serial-number`, falling back to `/etc/machine-id`.
- The result is checked against a strict `xx:xx:xx:xx:xx:xx` pattern before it is written.
- The same `host` MAC is used for every network function, so the host keeps one DHCP lease whichever function it picks.
- If no stable source exists, `apply` fails rather than accepting a random MAC.

### 3.5 Configuration

`/etc/wlanpi-usb-gadget/config`, `KEY=value`, shipped with every key commented out and defaults in code. It is read by the Python code directly and also usable as an `EnvironmentFile=`.

| Key | Default | Purpose |
|-----|---------|---------|
| `PROFILE` | decided in section 5 | Active composition |
| `SAFE_PROFILE` | decided in section 5 | Dead-man fallback |
| `ADDRESS` | `198.18.42.1/24` | Bridge address; DHCP pool derived from it. No other component pins this subnet (E11). `config check` warns if it overlaps the Bluetooth PAN subnet `198.18.43.0/24`; that is a convenience, not an image constraint |
| `BRIDGE_NAME` | `usb0` | Name of the addressed interface. Effectively fixed: avahi's allow-list, wlanpi-common and `wlanpi-stats` all read `usb0` (E7). Changing it is unsupported. |
| `FALLBACK_LINKLOCAL` | `yes` | Adds `169.254.42.1/16` and `fe80::1/64` to the bridge |
| `SERIAL_CONSOLE` | `yes` | Add the CDC-ACM function and getty |
| `STATUS_PAGE` | `yes` | Enable the HTTP status socket |
| `MONITOR_RECOVERY` | `off` | Observe-only or active recovery |
| `RESET_BUDGET` / `RESET_WINDOW_SEC` | `3` / `600` | Token bucket |
| `TRIAL_TIMEOUT_SEC` | `300` | Dead-man window for a new profile |
| `LOG_LEVEL` | `info` | `debug` adds per-event detail |

`wlanpi-usb-gadget config check` validates the file and prints the effective values.

## 4. Debuggability

The Go has no display and no serial console. Every failure mode below must be diagnosable through the USB cable, or through the device's other radios.

### 4.1 Logging strategy

- **Journal only.** Both units log to journald over the native protocol (a datagram to `/run/systemd/journal/socket`, standard library only), so each entry carries fields: `GADGET_EVENT`, `GADGET_STATE`, `GADGET_PREV_STATE`, `GADGET_PROFILE`, `GADGET_UDC_STATE`, `GADGET_IFACE`, `GADGET_INCIDENT`.
- **State transitions at `info`, one line each.** Steady state is silent. Nothing is logged on a timer.
- **Recovery actions at `warning`** with the reason, the ladder step and the remaining budget. Entering `HOLD` is `err`.
- **Useful queries**, documented in the README:
  - `journalctl -u 'wlanpi-usb-gadget-*' -b`
  - `journalctl GADGET_EVENT=recovery`
  - `journalctl -u wlanpi-usb-gadget-monitor -o json`
- **Event history on disk (mandatory).** The last 200 transitions, recoveries and setup failures (with the error text) are kept in `/var/lib/wlanpi-usb-gadget/events.jsonl`, rewritten atomically, capped in size. The Go's journal appears to be volatile (E6: `/var/log/journal` exists but is empty), so after a reboot or power loss this file is the only record. Nothing in the field-recovery story may depend on `journalctl -b -1`. The setup unit declares `RequiresMountsFor=/var/lib/wlanpi-usb-gadget` so it can record a failure even in early boot.

### 4.2 `wlanpi-usb-gadget` CLI

| Command | Output |
|---------|--------|
| `status` | Human-readable summary (below). `--json` for machines. Exit code 0 online, 1 degraded, 2 broken. |
| `selftest` | Checks with pass/fail and a fix hint for each: controller present and in peripheral mode, `dtoverlay` as expected, `libcomposite` loadable, configfs mounted, no foreign gadget bound, config valid, MACs valid, networkd files present, bridge up, DHCP server running, addressed interface present in avahi's `allow-interfaces` and avahi IPv6 enabled, boot configuration for gadget mode (on the Go: this package's marked block present and a reboot not pending; on other boards: warn only), fallback addresses present on the bridge, journal persistent or volatile (informational), `dwc2` module or built-in, leftover `blacklist dwc2` from the old package. |
| `report` | A tar.gz to stdout: `status --json`, event history, journal for both units for the current boot (and the previous boot only if the journal is persistent), configfs tree dump, `ip -d link` / `addr` / `neigh`, `networkctl status`, kernel log lines for dwc2 and gadget, effective config. No secrets. |
| `events` | Event history, newest first. |
| `apply` / `teardown` | Used by the setup unit; `apply --force` rebuilds. |
| `profile list` / `profile set <name>` / `profile commit` / `profile revert` | Profile trial handling (4.5). |
| `recover` | Leave `HOLD` and run the ladder once. |
| `config check` | Validate and print effective config. |

`status` prints:

- Package version, active profile, trial or committed, safe profile.
- State machine state, time in state, previous state.
- UDC name, UDC state, speed, `dr_mode`, dwc2 module or built-in.
- configfs summary: VID/PID/bcdDevice, each configuration and its functions, which is the OS-descriptor config, bound or not.
- Per function netdev: name, carrier, MAC, rx/tx packets.
- Bridge: addresses, DHCP leases (hostname, address), neighbours.
- **Host guess** with its evidence, labelled best-effort: which function the host activated, DHCP hostname, enumeration speed. Example: `Windows (likely): activated NCM in OS-descriptor config; hostname DESKTOP-...`.
- Last 5 recoveries with reason and result; reset budget remaining.
- Selftest failures, if any.

### 4.3 Diagnostics over the USB link when the network is broken

Layered so that each works when the one above it does not:

1. **Normal path:** DHCP address, `ssh wlanpi@198.18.42.1`.
2. **Fallback addressing:** `ssh wlanpi@<hostname>.local` over IPv4 mDNS, or `ssh wlanpi@169.254.42.1`, for hosts that self-assigned a 169.254 address; `ssh wlanpi@fe80::1%<interface>` for hosts that did not. Covers a broken DHCP exchange. IPv6 mDNS is not available because the image's avahi has `use-ipv6=no` (3.3). The mDNS part depends on avahi answering on the gadget link, which is why the addressed interface keeps the name `usb0`.
3. **Status page:** `http://wlanpi.local:<port>/` or `http://169.254.42.1:<port>/` (browsers handle the IPv6 zone syntax poorly, so the 169.254 address is the documented one) served by a socket-activated standard-library HTTP handler bound to `usb0` only. Read-only. Routes: `/` (status as HTML), `/status.json`, `/events.json`, `/report.tgz`. Lets a user with only a browser grab a report.
4. **USB serial console:** a CDC-ACM function in the same configuration as the network function gives `/dev/ttyGS0`, with `serial-getty@ttyGS0` started by a udev rule. Works with no IP at all. Expected host support (verify in Phase 0):

   | Host | Serial device | Tool |
   |------|---------------|------|
   | Windows 10/11 | `COMx` (in-box `usbser.sys`) | PuTTY, Windows Terminal |
   | macOS | `/dev/cu.usbmodem*` | `screen`, `tio` |
   | Linux | `/dev/ttyACM*` | `screen`, `tio`, `picocom` |
   | iOS/iPadOS, Android | none in practice | use layers 1 to 3 |

   The console requires a normal login. It is the same trust boundary as SSH over the same cable.
5. **Out of band:** the Go's existing Bluetooth PAN and Wi-Fi paths are unaffected by this package and remain a last resort. The README points to them.

### 4.4 Crash and loop backstops

- **Monitor crash:** systemd restarts it; it re-reads state and does not touch the gadget. After 5 crashes in 5 minutes it stays down and the gadget keeps working unmonitored. `status` reports the monitor as failed.
- **Monitor hang:** `WatchdogSec=30` kills and restarts it.
- **Setup failure:** logged with the failing configfs path; state `UNBOUND`; the monitor and CLI still work for diagnosis over other paths.
- **Recovery loop:** token bucket and `HOLD` (3.2).
- **Bad composition:** dead-man switch (4.5).

### 4.5 Dead-man switch for composition changes

- `profile set <name>` records the profile as a **trial** and applies it.
- A trial is **committed** automatically once the state machine has been `ONLINE` for 30 s, or manually with `profile commit`.
- The trial clock runs only while a host is physically present (`ENUMERATING`, `CONFIGURED_NO_LINK`, `LINK_UP`). Time spent `DETACHED` does not count, so an unplugged device never reverts by itself.
- If the trial clock reaches `TRIAL_TIMEOUT_SEC` without commit, the setup tool reverts to the last committed profile, logs at `err`, and records the event.
- If the committed profile itself cannot be applied, `SAFE_PROFILE` is used.
- Trial state is stored in `/var/lib/wlanpi-usb-gadget/profile.json`, so a reboot mid-trial resumes the trial rather than committing it.

### 4.6 Field recovery with only a laptop and a USB cable

Documented as a numbered runbook in the README:

1. Plug in, wait 30 s, try `ssh wlanpi@198.18.42.1`.
2. Try `ssh wlanpi@<hostname>.local`, then `ssh wlanpi@169.254.42.1` (Windows, macOS, iOS), then `ssh wlanpi@fe80::1%<interface>` (Linux, Android).
3. Open `http://wlanpi.local:<port>/` and download the report.
4. Open the USB serial port, log in, run `wlanpi-usb-gadget status` and `selftest`.
5. **Replug knock:** unplug and replug the cable 3 times within 20 s. The monitor counts attach events and, on the third, reverts to `SAFE_PROFILE` and clears `HOLD`. This is the only recovery that needs no working data path at all. It is proposed, not settled (Q5).
6. Use Bluetooth PAN or Wi-Fi, then `wlanpi-usb-gadget profile revert`.
7. Reflash.

Because the journal is likely volatile, steps 3 and 4 show history from the on-disk event file, not from `journalctl`. After a power cycle, `wlanpi-usb-gadget events` is the place to look.

### 4.7 Host-side capture guidance

The README lists what to collect from the host and attach to a bug report:

- **Windows:** `pnputil /enum-devices /connected`, Device Manager driver and status code for the adapter, `Get-NetAdapter`, `ipconfig /all`.
- **macOS:** `system_profiler SPUSBDataType`, `ifconfig`, `networksetup -listallhardwareports`.
- **Linux:** `lsusb -v -d 1209:`, `dmesg | tail -50`, `ip -d link`.
- **Any:** optional USB trace (`usbmon`, USBPcap) for enumeration failures.

## 5. Decision point: target composition

This is not decided. The proposal is NCM-first, gated on Phase 0 results.

### 5.1 Constraints that shape the options

- Windows uses the first configuration the device presents and does not fall back to another.
- The Linux gadget stack presents the OS-descriptor configuration first, regardless of its `c.N` name.
- Linux hosts take a first-configuration RNDIS when `rndis_host` is available (audit X1, verify).
- macOS and iOS have no RNDIS driver and select a configuration they can drive (verify).
- Windows caches driver binding per VID/PID/bcdDevice (X5). `1209:2042` is registered to WLAN Pi (section 13) and is reused, so **each profile gets its own `bcdDevice`**. Whether a composite NCM + CDC-ACM device should have its own PID under pid.codes policy is Q1.
- The Go runs kernel 6.19. A kernel that new strengthens the case for NCM-first and against any design that relies on hosts, or the wider ecosystem, keeping RNDIS alive.
- Windows NCM: in-box driver, bound through the `WINNCM` compatible ID on Windows 10 1903 and later and by class on Windows 11 (verify, including that the target kernel's `f_ncm` exposes `os_desc`).

### 5.2 Options

| Option | Layout | Windows 10 1903+/11 | Older Windows | macOS / iOS | Linux | Android | Main cost |
|--------|--------|--------------------|---------------|-------------|-------|---------|-----------|
| A `ncm` | One config: NCM (`WINNCM`) + ACM | NCM | no network | NCM | NCM | NCM where the kernel has it | Drops hosts without NCM |
| B `ncm-ecm` | Config 1: NCM (`WINNCM`) + ACM. Config 2: ECM + ACM | NCM | no network | NCM, ECM if needed | NCM | NCM | Second config may never be chosen; more to test |
| C `legacy` | Config 1 (OS desc): RNDIS. Config 2: ECM | RNDIS | RNDIS | ECM | RNDIS, probably | ECM or RNDIS | Keeps Windows and Linux on RNDIS; no serial console unless verified |
| D adaptive | Start on A or B; if a host reaches `CONFIGURED_NO_LINK` and stays there, switch to C for that attach session | NCM | RNDIS after one re-enumeration | NCM | NCM | either | Most complex; one deliberate re-enumeration; must not become a reset storm |

### 5.3 Recommendation

- Implement profiles `ncm`, `ncm-ecm` and `legacy` as data (a small table in code), all on `1209:2042`, each with its own `bcdDevice`.
- Ship early milestones with `PROFILE=legacy` and `SAFE_PROFILE=legacy`, because that layout matches what is known to work in the field.
- Switch the default to `ncm` or `ncm-ecm` only when the section 9 matrix passes for it. `SAFE_PROFILE` stays `legacy` until a full release cycle has passed without fallback events.
- Treat option D as a later experiment, off by default, bounded to one switch per attach.

### 5.4 Questions the matrix must answer before the default changes

- Does Windows 10 (1903+) bind NCM from `WINNCM` with the serial function present?
- Does Windows 11 bind NCM and the serial port in the same composite device?
- Does iOS/iPadOS accept a composite NCM + ACM device, or must the serial function be dropped for it?
- Does macOS pick config 2 (ECM) when it cannot drive config 1? Is config 2 ever reached on any host?
- Throughput of NCM versus ECM versus RNDIS on the Go (iperf3 both directions).
- Sleep/wake behaviour of each profile on each host, especially iOS.

## 6. Repository layout

New repository `WLAN-Pi/wlanpi-usb-gadget`, BSD 3-Clause, single `main` branch to start.

```
wlanpi-usb-gadget/
├── wlanpi_usb_gadget/
│   ├── __main__.py          # CLI entry (argparse)
│   ├── config.py            # parse and validate /etc/wlanpi-usb-gadget/config
│   ├── profiles.py          # composition table: functions, configs, PIDs, OS descriptors
│   ├── configfs.py          # tree planner, write_attr, apply, ordered teardown, tree hash
│   ├── mac.py               # derivation and validation
│   ├── netcfg.py            # generate /run/systemd/network files
│   ├── udc.py               # controller detection, dr_mode, state poll, soft_connect, bind
│   ├── netlink.py           # minimal rtnetlink listener (link, neigh)
│   ├── statemachine.py      # states, transitions, pure and clock-injected
│   ├── recovery.py          # ladder, token bucket, hold, replug knock
│   ├── deadman.py           # profile trial, commit, revert
│   ├── monitor.py           # event loop, sd_notify, watchdog
│   ├── journal.py           # native journald protocol
│   ├── events.py            # bounded on-disk history
│   ├── status.py            # status, selftest, host guess
│   ├── report.py            # report bundle
│   └── httpd.py             # socket-activated status page
├── systemd/
│   ├── wlanpi-usb-gadget-setup.service
│   ├── wlanpi-usb-gadget-monitor.service
│   ├── wlanpi-usb-gadget-status.socket
│   └── wlanpi-usb-gadget-status@.service
├── udev/60-wlanpi-usb-gadget.rules      # start serial-getty@ttyGS0 when it appears
├── etc/wlanpi-usb-gadget/config
├── tests/
│   ├── unit/                # pytest
│   ├── maintscripts/        # bats: install, upgrade, remove, purge in a container
│   └── integration/         # dummy_hcd loopback and on-device suites
├── debian/
├── docs/                    # host matrix results, runbook, design notes
├── pyproject.toml           # metadata, ruff, pytest config
├── tox.ini                  # py313
├── README.md
├── OSS.md                   # states: no third-party runtime dependencies
└── LICENSE
```

## 7. Debian packaging

Target: Debian 13 (trixie), system Python 3.13, `Architecture: all`. Trixie is the minimum supported release; see section 7a for what that rests on.

**Why no `dh_virtualenv`:** the WLAN Pi convention uses `dh_virtualenv` to isolate third-party Python dependencies. This package has none. A virtualenv would add build time, package size and an interpreter path to break, for no isolation benefit. It also must start in early boot, where the fewer moving parts the better. If a third-party dependency is ever added, this decision is revisited rather than worked around with `pip`.

**Install paths**

| Path | Contents |
|------|----------|
| `/opt/wlanpi-usb-gadget/wlanpi_usb_gadget/` | Python package (keeps the org's `/opt/wlanpi-{app}` convention; expect the usual lintian warning) |
| `/usr/bin/wlanpi-usb-gadget` | Small launcher |
| `/usr/lib/systemd/system/` | Units, installed by `dh_installsystemd`. The package ships nothing under the aliased `/lib`, `/bin` or `/sbin` paths (7a.4). |
| `/usr/lib/udev/rules.d/` | udev rule |
| `/etc/wlanpi-usb-gadget/config` | The only conffile |
| `/run/systemd/network/` | Generated networkd files (runtime) |
| `/run/wlanpi-usb-gadget/` | Runtime state |
| `/var/lib/wlanpi-usb-gadget/` | Event history, profile trial state |

Nothing is installed under `/etc/systemd`, and there is no `/var/log` directory and no logrotate file because there is no log file.

**`debian/control` (sketch)**

```
Source: wlanpi-usb-gadget
Section: net
Priority: optional
Build-Depends: debhelper-compat (= 13), python3, python3-pytest <!nocheck>
Standards-Version: 4.6.2

Package: wlanpi-usb-gadget
Architecture: all
Depends: ${misc:Depends}, python3 (>= 3.13), systemd (>= 257~), kmod, iproute2
Recommends: avahi-daemon, wlanpi-common
Conflicts: wlanpi-usb-ethernet
Replaces: wlanpi-usb-ethernet
```

- `Conflicts` because both packages would drive the same UDC. `Replaces` so apt removes the old package in the same transaction.
- No `Provides: wlanpi-usb-ethernet` unless something depends on the old name (Q3).
- The Phase 0 diagnostic-only build (0.1.x) omits `Conflicts`, since it never touches the gadget and is meant to be installed next to the old package to collect data.

**Coexistence and migration handling, owned by this package**

- The old package's conffiles survive `remove` (as opposed to `purge`), including `/etc/modprobe.d/wlanpi-usb-gadget.conf` (`blacklist dwc2`) and `usb0.network` / `usb1.network`. `selftest` detects them and prints the purge command. The new package never deletes another package's files.
- `apply` loads `dwc2` explicitly, so a leftover blacklist does not break the new package.
- The bridge is named `usb0`, so a leftover `/etc/systemd/network/usb0.network` from the old package would match it. The generated files use a `50-` prefix and therefore take precedence; `selftest` still flags the leftover. A leftover `usb1.network` matches nothing.
- `/etc/avahi/avahi-daemon.conf` is not touched on install, upgrade or removal.
- `apply` refuses to run while a foreign gadget is bound.

**Maintainer scripts**

- `dh_installsystemd --name=...` for each unit. The setup unit is installed with `--no-stop-on-upgrade --no-restart-after-upgrade`; the monitor restarts normally.
- `prerm remove`: stop the monitor, then the setup unit (which runs the ordered teardown), then disable. debhelper generates this; no hand-written `systemctl` calls.
- `postrm purge`: remove `/var/lib/wlanpi-usb-gadget`.
- **Gadget-mode enablement in `config.txt`: owned by this package on the Go, verify-and-warn elsewhere.** This reverses the earlier "never edited" position, which rested on E2. A full read of `/opt/wlanpi-common/wlanpi-config-at-startup.sh` (E10) shows it manages R4, M4, M4+ and Pro, and for the "Oscium Go" board only prints "Detected WLAN Pi Go". On the Go, nothing in wlanpi-common manages `config.txt`, `otg_mode` or the `dwc2` overlay.
  - **Why ownership is needed:** on the Go today the only known writer of `dtoverlay=dwc2` is the old package's postinst, and its postrm deletes that line on removal. Installing this package removes the old one (`Conflicts`), so without action the next boot could have no UDC and no USB link, on a device with no console. This is the most serious migration hazard in the plan.
  - **Mechanism:** a `wlanpi-usb-gadget bootcfg ensure | remove | check` command (Python, unit-tested against sample files), called from `postinst` and `postrm`. It writes one marked block (`# BEGIN wlanpi-usb-gadget` / `# END wlanpi-usb-gadget`), never touches unmarked lines, keeps a backup, is idempotent, and never reboots. `status` reports "reboot required" when the block changed since boot.
  - **Ordering:** dpkg removes the conflicting old package (running its postrm) before configuring the new one, so `bootcfg ensure` in the new postinst runs after the old postrm has deleted its line. The M5 migration test asserts the block is present after the swap.
  - **Board scope:** `bootcfg ensure` acts only when the board is the Go (from `/etc/wlanpi-model` when present, otherwise the device-tree model). On R4, M4, M4+ and Pro, wlanpi-common owns the boot configuration (it uses `dtoverlay=dwc2,dr_mode=otg` with an explicit `otg_mode=0` and reboots on mode switches), so there this package only checks and warns.
  - **Block contents are not decided.** They depend on the Phase 0 checks below; the starting assumption is the line the old package writes today (`dtoverlay=dwc2` under `[cm4]`), because that is what works in the field.
  - **Conditional:** if Phase 0 shows peripheral mode on the Go comes from somewhere else (device tree, boot firmware), ownership is dropped again and the package returns to verify-and-warn on all boards.
  - **Relationship to wlanpi-common:** `Recommends` stays, but for a different reason: `/etc/wlanpi-model` is the org's board identifier. It is not a `Depends`, because the package works on the Go without it. Raised with the maintainers as Q2b: should Go enablement move into wlanpi-common later, so one component owns `config.txt` on every board?
- **`dwc2` blacklist, if M0a says it is needed:** shipped as `/usr/lib/modprobe.d/wlanpi-usb-gadget.conf` (not a conffile, removed with the package), together with a small separate unit that loads `dwc2` when the controller is in host mode, so host mode never depends on gadget setup (A5).

**Unit hardening:** both units need root for configfs. Apply `NoNewPrivileges=true`, `ProtectHome=true`, `ProtectSystem=strict` with `ReadWritePaths=` limited to configfs, `/run/wlanpi-usb-gadget`, `/run/systemd/network` and `/var/lib/wlanpi-usb-gadget`. The status page service runs as a dynamic unprivileged user and only reads `/run/wlanpi-usb-gadget/state.json` and the event history.

## 7a. Debian Bookworm -> Trixie migration

The WLAN Pi image is moving from Debian 12 (bookworm) to Debian 13 (trixie). This section records what changes underneath this project and what the plan does about it. Sources are listed in 7a.7. Items I could not confirm from a source are marked "unconfirmed".

### 7a.0 Decision: trixie is the floor, bookworm is dropped

**Recommendation: support trixie only. Do not build, test or publish this package for bookworm.**

| Reason | Detail |
|--------|--------|
| It matches org policy | WLAN Pi's stated target is Debian 13 with Python 3.13 exactly; bookworm and Python 3.11 are maintenance-only, on `debian/<codename>` branches kept by the core team. A brand-new package is not maintenance. |
| Bookworm already has a working answer | Bookworm images keep `wlanpi-usb-ethernet`. Nothing on bookworm needs this package. |
| The image is moving anyway | By the time this package reaches 1.0 (five milestones away), the Go image is trixie. |
| "Best effort" would not be cheap | It doubles the on-device and host matrix, and adds a second Python, a second systemd with different networkd behaviour (7a.1), and an older gadget stack (7a.2). Untested best-effort support on the one link users rely on to reach the device is worse than a clear "not supported". |
| The existing repo already made this call | `wlanpi-usb-ethernet` CI was switched to build for trixie only (commit `e7c5a4b`). |

How the floor is enforced: `Depends: python3 (>= 3.13), systemd (>= 257~)`, changelog distribution `trixie`, CI builds only for trixie, and a CI test that a bookworm install is refused.

If a bookworm build is ever required, it is a `debian/bookworm` branch owned by the core team, with the costs in 7a.3 and 7a.1 accepted explicitly. Nothing in the design forbids it; it is just not planned or tested.

### 7a.1 systemd 252 -> 257

Bookworm ships systemd 252; trixie ships 257.

| Change | Version | Effect on this project | Action |
|--------|---------|------------------------|--------|
| `KeepConfiguration=dhcp` / `dhcp-on-stop` renamed to `dynamic` / `dynamic-on-stop`; "dynamic" now covers DHCPv4, DHCPv6, NDISC and IPv4LL, not just DHCPv4. Restarting networkd keeps dynamic configuration regardless of the setting. | 257 | The gadget bridge has a static address and a DHCP server, and runs no DHCP client, so nothing here is "dynamic" configuration. The change does not alter the link's behaviour. | Do not set `KeepConfiguration=` in generated files. If it is ever needed, use the 257 spelling. |
| `IPForward=` deprecated in favour of `IPv4Forwarding=` / `IPv6Forwarding=` | 256 | The gadget link must not forward (no router is emitted). | Do not emit `IPForward=`. If forwarding is ever wanted, use the new keys. |
| `.netdev` changes are applied on `networkctl reload` where possible | 257 | On 252, changing a bridge `.netdev` needed the link deleted or networkd restarted. On 257, `apply` can regenerate files and reload. | `apply` uses `networkctl reload`; no bookworm fallback path is written. |
| `networkctl edit --runtime` edits `/run/systemd/network`; `networkctl mask` / `unmask`; `cat` and `edit` accept `.netdev` by link | 256, 257 | Useful for field debugging of the generated files. | Mention in the README runbook. |
| DHCP server | 253 to 257 | The release notes I read list only IPv6-only mode support (RFC 8925) in 255, and no changed or removed `[DHCPServer]` options. The options the plan uses (`PoolOffset=`, `PoolSize=`, `EmitDNS=`, `EmitRouter=`) are unchanged. I believe lease persistence across restarts (`PersistLeases=`) arrived in this range, but that is unconfirmed. | M0a check: do leases survive a networkd restart. `status` reads leases defensively and reports "unknown" if the JSON lacks them. |
| `mac` added to the default `AlternativeNamesPolicy=` | 257 | Interfaces gain an `enx...` alternative name. Cosmetic. | `status` and matching use the primary name only. |
| Net naming scheme `v253`: `ID_NET_NAME_PATH` now also set for USB devices not behind PCI | 253 | This targets USB network adapters plugged into a platform USB host controller. A gadget netdev's parent is the gadget device, not a USB device, so it should not apply. I found nothing stating that gadget `usb%d` names are renamed by any scheme, and the Go today shows `usb0`. Unconfirmed for the renamed members on 257. | Generated `.link` file with `NamePolicy=keep` and `MACAddressPolicy=none` for the member names, so the outcome does not depend on the default policy. M0a check. |
| cgroup v1 ignored by default; SysV scripts deprecated; split-`/usr` support removed | 255 to 257 | None. The package has no SysV script and assumes merged `/usr`. | None. |
| `Type=notify-reload`, `OpenFile=`, `RestartSteps=`, `PassFileDescriptorsToExec=`, `PollLimit*=` | 253 to 256 | Available now that trixie is the floor. None is required. | Monitor may use `Type=notify-reload` so `systemctl reload` re-reads config without a restart. Optional. |
| `Type=oneshot`, `RemainAfterExit=`, `ExecStop=`, socket activation | 253 to 257 | No changes found in the release notes for these. The setup and monitor unit design in 3.1 and 3.2 stands as written. | None. |
| `journalctl --list-invocation` / `-I` | 257 | Shows logs for one invocation of a unit, which suits a oneshot setup unit. | Add to the README log queries. |

### 7a.2 Kernel

Debian trixie's own kernel is 6.12. Raspberry Pi OS moved to a trixie base in October 2025 on 6.12 and later updated to 6.18. None of that is what the Go runs: the Go has WLAN Pi's own `6.19.14-v8-wlanpi+` (section 13). So the kernel is not tied to the Debian release, and the gadget-side changes below apply whatever the base image.

| Topic | What changed | Effect on this project | Action |
|-------|--------------|------------------------|--------|
| RNDIS host support on Linux | Greg Kroah-Hartman has carried patches since 2022 to disable all RNDIS drivers; in 2026 the branch was updated to make them depend on `BROKEN`. I found no confirmation that this is merged in mainline. | A Linux host that today picks RNDIS (audit X1) may one day have no RNDIS driver. | Supports NCM-first (section 5). Keep one matrix host without `rndis_host`. |
| `f_rndis` configfs locking, CVE-2026-43342 | A race in the class, subclass and protocol configfs attributes; fixed by adding locking. Local, availability impact only. Fixed in Debian bookworm 6.1.187-1 and trixie 6.12.107-1. | Only root writes configfs here, and `apply` writes from a single process, so exposure is low. Status in the WLAN Pi kernel is unconfirmed. | M0a check. `apply` and `teardown` take a lock file so two invocations never write configfs concurrently. |
| `f_ncm` net_device lifecycle refactor | A December 2025 series moved the NCM net_device from function-instance creation to bind, so the interface existed only while bound. It broke userspace: a function linked into two configurations produced two netdevs, and configfs `ifname` read back `usb%d`. | Directly relevant: this plan names members through `ifname` (D4) and the `ncm-ecm` and `legacy` profiles link functions into configurations. | M0a check on the Go kernel. Never link one function instance into two configurations; each configuration gets its own instance. |
| Reverts and `device_move` fix | A March 2026 series reverted that refactor and instead moves the net_device between the gadget and `/sys/devices/virtual` across bind and unbind. Reported as landed for 7.0. | On kernels with this fix, the NCM netdev persists across UDC unbind with a stable name; on kernels between the two series, it may not. Whether 6.19.14 has the refactor, the reverts, both or neither is unknown. | Q16. The monitor must not assume a member netdev exists while unbound, and must re-resolve members by name after every bind. |
| `u_ether`: "refuse to re-register a failed net_device" | An October 2026 patch tied to a syzbot report of `kernel BUG in register_netdevice`, reached from `ncm_bind` during a configfs UDC write. **I could not read the patch itself** (the list archive refused the fetch), so its exact behaviour and merge status are unconfirmed. | This is the recovery ladder's path. From the title and trace: after a bind that failed during netdev registration, a second bind on the same function instance either crashes (without the patch) or is refused (with it). Neither is a successful recovery. | Recovery change in 3.2: a failed bind is terminal for that function instance; go to full teardown and re-create. M0a check on a reflashable device. |
| Other `u_ether` and NCM fixes | 2026 stable backports include an `f_ncm` fix for 6.1.y and a `u_ether` NULL-pointer fix in `eth_get_drvinfo` on a detached gadget. | `status` and `report` read ethtool-style information from member netdevs, possibly while detached. | Read member state through netlink and sysfs, not ethtool ioctls, and tolerate errors. |
| Kernel series | 6.19 is not an upstream long-term series (6.12 and 6.18 are, per the Debian and Raspberry Pi OS choices above). | Later gadget fixes may not be backported to 6.19.x. | Q16. Record the kernel version in every `report`. |

### 7a.3 Python 3.11 -> 3.13

- Trixie's `python3` is 3.13; bookworm's is 3.11.
- Python 3.13 removed the 19 remaining PEP 594 modules (`cgi`, `cgitb`, `telnetlib`, `pipes`, `crypt`, `nntplib` and others) plus `lib2to3`; 3.12 removed `distutils`, `asyncore`, `asynchat` and `smtpd`.
- **The planned daemon uses none of them.** Its imports are `argparse`, `socket` (including `AF_NETLINK`), `select`, `struct`, `json`, `hashlib`, `http.server`, `tarfile`, `pathlib`, `subprocess`, `time`, `os`, `logging`. One rule follows: the status page must not use `cgi` for query parsing; use `urllib.parse`.
- Cost of also supporting 3.11, if bookworm were kept: no syntax or library features newer than 3.11, a `py311` tox environment, and a second interpreter in every test stage. Small in code, real in test time. Not paid, per 7a.0.
- `tox.ini` lists `py313` only. `pyproject.toml` sets `requires-python = ">=3.13"`.

### 7a.4 Packaging

| Item | Bookworm | Trixie | Action |
|------|----------|--------|--------|
| debhelper compat | 13 available | 13 available; trixie's avahi package builds with `debhelper (>= 13.11.6~)`. Trixie's exact debhelper version is unconfirmed. | Stay on `debhelper-compat (= 13)`, as the org convention says. |
| Unit install path | `/lib/systemd/system` | debhelper in trixie installs units under `/usr/lib/systemd/system`. | Let `dh_installsystemd` choose; never hard-code `/lib/...` in `debian/install`, docs or tests. |
| Merged `/usr` | Transitional | Final: systemd dropped split-`/usr` support in 255, and the trixie release notes carry a dpkg warning about it. | Ship every file under `/usr`, `/etc` or `/opt`. Nothing under `/lib`, `/bin`, `/sbin`. The udev rule goes to `/usr/lib/udev/rules.d`, the optional modprobe file to `/usr/lib/modprobe.d`. |
| Conffiles | `/etc` files are conffiles | Same | Unchanged: the only conffile is `/etc/wlanpi-usb-gadget/config`. Generated networkd files live in `/run`, so dpkg never prompts about them. |
| Avahi drop-in question | n/a | n/a | No dpkg or avahi change in trixie makes option (a) in 3.3 easier. The recommendation (bridge named `usb0`, no avahi edits) stands. |
| Base image | Raspberry Pi OS bookworm era | Raspberry Pi OS has been trixie-based since October 2025 | The Go image's own base and kernel are WLAN Pi's; record them in M0a. |

### 7a.5 Avahi

- Bookworm ships avahi 0.8-10; trixie ships 0.8-16. Both are upstream 0.8 with Debian patches.
- The `allow-interfaces` and `deny-interfaces` options are documented identically for 0.8 across distributions: a comma-separated list, with `deny-interfaces` taking precedence. I found no change in how they are handled between the two Debian revisions, and no documented include-directory or drop-in mechanism. Both points rest on man pages, not on a diff of the Debian patches.
- Consequence: the allow-list finding (E4) carries over to trixie unchanged, and so does the recommendation in 3.3.

### 7a.6 Adjustments to other sections

| Section | Adjustment |
|---------|------------|
| 2 | D1 annotated; D14 added (trixie floor). |
| 3.2 Recovery | Failed bind is terminal for the function instance; step 3 only after a previously successful bind. |
| 3.3 Addressing | Generated `.link` file for member names and MACs; no `KeepConfiguration=` or `IPForward=` in generated files. |
| 4.5 Dead-man | No change in logic. A revert goes through full teardown and re-create, never a rebind of existing instances, for the reason in 7a.2. |
| 7 Packaging | Units under `/usr/lib/systemd/system`; `systemd (>= 257~)` in `Depends`. |
| 8 Milestones | Six M0a checks added (release identification, `ifname` and netdev lifetime, failed-bind rebind, udev naming, DHCP lease persistence, CVE fix status). M5 migration test depends on Q15. |
| 10 CI | Trixie-only builds and tests; a bookworm-refusal test. |
| 11, 12, 13 | New risks, Q15 to Q17, and additions to the unverified list. |

### 7a.7 Sources

- systemd release notes, versions 253 to 257: https://raw.githubusercontent.com/systemd/systemd/v257/NEWS (253 read only in part)
- systemd net naming scheme man page: https://www.freedesktop.org/software/systemd/man/257/systemd.net-naming-scheme.html
- Package versions in bookworm and trixie (systemd, Python): https://michael-prokop.at/blog/2025/07/20/what-to-expect-from-debian-trixie-newintrixie/
- Debian 13 release notes: https://www.debian.org/releases/trixie/release-notes/index.html
- Avahi versions: https://tracker.debian.org/pkg/src:avahi and https://packages.debian.org/source/trixie/avahi
- `avahi-daemon.conf(5)`: https://man.archlinux.org/man/avahi-daemon.conf.5
- Raspberry Pi OS on trixie: https://www.cnx-software.com/2025/10/06/raspberry-pi-os-debian-13-trixie/ and https://www.raspberrypi.com/software/operating-systems/
- RNDIS disablement: https://www.phoronix.com/news/RNDIS-Disable-Linux-2026-Patch
- CVE-2026-43342: https://security-tracker.debian.org/tracker/CVE-2026-43342 and https://nvd.nist.gov/vuln/detail/CVE-2026-43342
- `f_ncm` lifecycle refactor (December 2025): https://ratatoskr.run/linux-usb/2025/12/3348668/t
- Reverts and `device_move` fix (March 2026): https://ratatoskr.run/linux-usb/2026/03/3448096/t
- syzbot report linking the "refuse to re-register a failed net_device" patch: https://syzkaller.appspot.com/bug?extid=85ede281798faf736677
- Python 3.13 removals: https://docs.python.org/3.13/whatsnew/3.13.html

## 8. Phased milestones

Every milestone from M0b onward ends in an installable `.deb` published to the packagecloud dev repo.

### Phase 0: spike and hardware verification

**M0a. Bench verification (no package).** Throwaway scripts on a Go, with real hosts.

| Check | Audit ref | Method |
|-------|-----------|--------|
| Which function does a Linux host activate with the current RNDIS + ECM layout? | X1 | `lsusb -v`, `ip link` on host; carrier on Pi |
| Does the old package enter a reset loop after Mac then Windows without reboot? | I1 | Reproduce, capture its log |
| Does `rm -rf` remove a configfs gadget tree, and does the ordered teardown leave it clean? | C4 | Run both, list the tree |
| Is the dwc2-after-configfs load order still needed for picky hosts? (Module vs built-in is **RESOLVED**: module, section 13.) | A5, A6 | Enumerate on a picky host with and without the blacklist |
| Does a bridge named `usb0` with renamed members work, and does avahi answer on it over IPv4 and IPv6? | D4, 3.3 | Build by hand, `avahi-browse` / `ping wlanpi.local` from each host |
| Which other files on the Go name `usb0`/`usb1` or use interface allow-lists? (**ANSWERED, E7**; two follow-ups below.) | Q12 | Done: `grep -rl -e usb0 -e usb1 /etc /opt/wlanpi-* /usr/lib/wlanpi /usr/bin/wlanpi*` |
| What does `/usr/bin/wlanpi-stats` do with `usb0`/`usb1`, and is a bridge named `usb0` enough for it? | E7 | Read the script; run it with the bridge layout built by hand |
| Do RX counters on a bridge named `usb0` behave as `otg_link_active()` in wlanpi-common expects? | E7 | `ip -s link show usb0` before and after traffic, bridge layout |
| How is peripheral mode enabled on the Go at all? | E10, D16 | `ls /sys/class/udc`; `lsmod \| grep dwc2`; `ls /sys/bus/platform/drivers/dwc2/`; `cat` the controller's `of_node/dr_mode`; `grep -n -e dwc2 -e otg_mode /boot/firmware/config.txt`; `cat /etc/wlanpi-model`; repeat with the old package's `config.txt` line removed and after a reboot |
| Does removing `wlanpi-usb-ethernet` strip the overlay line and lose the UDC on next boot? | E10 | On a reflashable unit: `apt remove`, inspect `config.txt`, reboot, check `/sys/class/udc` |
| Do Windows, macOS, iOS, Linux and Android hosts reach `169.254.42.1` or `fe80::1` when DHCP is off, and does avahi publish the 169.254 address? | D15 | Add both addresses by hand, stop the DHCP server, test from each host; `avahi-resolve -n <hostname>.local` |
| Kernel IPv6 settings, re-run cleanly (the first attempt used a malformed command) | E9 | `sysctl net.ipv6.conf.all.disable_ipv6 net.ipv6.conf.default.disable_ipv6 net.ipv6.conf.all.autoconf` |
| Is the journal persistent on the Go image? (**Partly answered, E6:** likely volatile.) Confirm. | Q7 | `ls -la /var/log/journal /run/log/journal; grep -i storage /etc/systemd/journald.conf /etc/systemd/journald.conf.d/* 2>/dev/null; journalctl --list-boots` |
| Does `poll()` on UDC `state` wake on every transition? | D3 | Small script, compare with 100 ms sampling |
| Does configfs `ifname` work, and do names survive rebind? | D4 | Set, bind, unbind, rebind |
| Does `f_ncm` expose `os_desc` on the Go kernel; does Windows bind NCM via `WINNCM`? | section 5 | Windows 10 and 11 |
| NCM + ACM composite on each host | section 5, 4.3 | All matrix hosts |
| What does iOS actually need after wake? Does a soft reconnect fix it, or nothing at all? | X7 | iPhone and iPad, lock/unlock cycles |
| Python cold-start time at early boot | D1 | `systemd-analyze`, timestamps |
| Which Debian release and systemd version is on the Go under test? Record it with every M0a result. | 7a | `cat /etc/os-release`, `systemctl --version`, `python3 --version` |
| On the Go kernel, what does `functions/ncm.*/ifname` read before bind, does the netdev exist before bind, and does it survive UDC unbind? Same for ECM and RNDIS. | 7a.2, D4 | Read the attribute and `ip link` at each step |
| Does a deliberately failed bind followed by a rebind on the same function instance crash, get refused, or work? | 7a.2, 3.2 | On a scratch device with a serial or netconsole capture; never on a device you cannot reflash |
| Does udev leave `usbncm0` and friends alone, with and without the generated `.link` file? | 7a.1 | `udevadm info`, `udevadm test-builtin net_setup_link` |
| Does networkd's DHCP server keep leases across a networkd restart on this image? | 7a.1 | Restart networkd, compare the host's address |
| Is CVE-2026-43342 (f_rndis configfs locking) fixed in the WLAN Pi kernel? | 7a.2 | Kernel changelog or source |

Exit criteria: a written results page in `docs/` answering each row, and a go/no-go on D1 (Python at early boot), D3 (event sources) and D12 (no blacklist).

**M0b. `0.1.0`: diagnostics only.** `status`, `selftest`, `report`, `events` reading whatever gadget is present, including the old package's. No setup unit, no `Conflicts`.

Exit criteria: installs next to `wlanpi-usb-ethernet` without changing its behaviour; `report` output is enough to answer the M0a questions from a remote user's device.

### Phase 1: gadget core

**M1. `0.2.0`: setup unit, bridge, teardown.** `apply` / `teardown`, `legacy` profile only, MAC derivation, generated networkd files, `usb0`, config file, `Conflicts`/`Replaces`. No monitor.

Exit criteria:

- 200 consecutive `apply` / `teardown` cycles leave an empty configfs gadget directory and no stray netdevs.
- `systemctl stop` tears down cleanly; `apt remove` leaves no gadget, units or generated files.
- Upgrade from `0.2.0` to a rebuilt `0.2.0+1` does not drop an active SSH session over the link.
- MACs are identical across 20 reboots.
- Windows, macOS and Linux hosts get an address through `usb0`.
- On a Go, `bootcfg ensure` writes exactly one marked block, is a no-op on the second run, and `bootcfg remove` restores the file byte for byte. On a non-Go board it changes nothing.
- Replacing an installed `wlanpi-usb-ethernet` with this package and rebooting leaves a working UDC.

### Phase 2: observability

**M2. `0.3.0`: monitor (observe-only), fallback paths.** State machine, event sources, journal fields, event history, full `status`, fallback addressing, mDNS, status page, serial console function and getty.

Exit criteria:

- Every state in 3.2 is reachable and correctly reported in a scripted plug, unplug, sleep, wake, host-swap session on three host OSes.
- No log output in steady state for 24 h plugged in and 24 h on a charger.
- With the DHCP server deliberately stopped, the device is reachable at `169.254.42.1` (and by `<hostname>.local`) from Windows, macOS and iOS, at `fe80::1%<interface>` from Linux, and by the status page and USB serial where supported.
- After a power cycle with no persistent journal, `wlanpi-usb-gadget events` still shows the transitions from before the power cycle.
- mDNS answers on the gadget link with the stock Go `avahi-daemon.conf`, unmodified.
- Killing the monitor 10 times does not interrupt a running `iperf3` over the link.

### Phase 3: compositions and dead-man

**M3. `0.4.0`: profiles, trial/commit/revert, replug knock.** `ncm` and `ncm-ecm` profiles with their own `bcdDevice` values, `profile` commands, dead-man logic.

Exit criteria:

- A deliberately broken profile reverts within `TRIAL_TIMEOUT_SEC` of host-present time and never reverts while unplugged.
- A reboot mid-trial resumes the trial.
- The replug knock restores `SAFE_PROFILE` from a state with no working data path.
- The section 9 matrix has been run for all three profiles and the results are recorded.
- **Decision recorded:** default profile for 1.0.

### Phase 4: bounded recovery

**M4. `0.5.0`: recovery ladder.** Only the triggers that M0a and M2 field data show are needed. `MONITOR_RECOVERY` default is decided here.

Exit criteria:

- Fault injection (forced carrier loss, forced UDC unbind) recovers within the documented time.
- A permanently failing host produces at most `RESET_BUDGET` actions per window, then `HOLD`.
- Mac then Windows host swap without reboot: zero recovery actions (I1 regression test).
- 24 h on a charger: zero recovery actions (I2 regression test).
- iOS lock/unlock x50: link returns every time, with or without recovery as M0a determined.

### Phase 5: release

**M5. `1.0.0`: image integration.** Go image switches from `wlanpi-usb-ethernet` to `wlanpi-usb-gadget`; migration notes; README and runbook final.

Exit criteria:

- In-place migration on a device running the old package, over the USB link itself, ends with a working link after one reboot.
- Full matrix green for the default profile; known gaps listed in the README.
- Two weeks on the packagecloud dev repo with testers and no `HOLD` or dead-man events that are not explained.

## 9. Host-OS test matrix

Run per profile. Record which function the host activated and the result of each cell in `docs/host-matrix.md`.

**Hosts**

| Host | Versions |
|------|----------|
| Windows 11 | current release, x64 and ARM64 (the old package cites Surface Laptop 7 as a picky host) |
| Windows 10 | 22H2; one build older than 1903 if available, to document the failure mode |
| macOS | current and previous major, Apple Silicon and Intel |
| iOS / iPadOS | current major, USB-C iPhone and iPad |
| Android | Pixel (current), one Samsung, one device on Android 9 or older if available |
| Linux | Debian 13, Ubuntu LTS, Fedora current; one kernel built without `rndis_host` |
| ChromeOS | one device, best effort |

**Scenarios**

| # | Scenario | Pass condition |
|---|----------|----------------|
| S1 | Cold plug, device already booted | `ONLINE` within 15 s; host has address; `ssh` works |
| S2 | Host already connected when device boots | same |
| S3 | Unplug and replug x20 | `ONLINE` every time; no recovery actions |
| S4 | Host sleep and wake x20 (lid, screen lock on mobile) | link returns; recovery count as designed |
| S5 | Host swap without device reboot, every ordered pair of OS families | `ONLINE` on the second host; zero recovery actions |
| S6 | 24 h idle connected | no drops, no log growth |
| S7 | 24 h on a charger or power bank | zero resets, zero log lines after the first minute |
| S8 | DHCP disabled on the Pi | reachable at `169.254.42.1` or by IPv4 mDNS on hosts that self-assign; at `fe80::1` on hosts that do not; status page; serial. Record which path each host OS used. |
| S9 | USB hub and USB-A to C adapter in path | as S1 |
| S10 | Throughput, `iperf3` both ways, 60 s | recorded, no stall |
| S11 | Package upgrade over the link | session survives |
| S12 | Serial console | login works where the host supports it; absence does not break networking |
| S13 | Profile trial of a profile the host cannot drive | auto-revert, link returns |

## 10. CI plan

Reuse the shared `WLAN-Pi/gh-workflows` build (sbuild, trixie) and packagecloud deploy workflows used by current repos.

| Stage | Runs on | What |
|-------|---------|------|
| Lint | GitHub-hosted | `ruff check`, `ruff format --check`, `shellcheck` on maintainer scripts and the launcher, `actionlint` |
| Unit | GitHub-hosted, Python 3.13 via `tox` | `pytest`: MAC derivation and validation; config parsing; profile table to configfs plan (golden files); apply and ordered teardown against a fake tree in a temp directory, including partial trees; state machine with an injected clock; token bucket and `HOLD`; dead-man trial logic; networkd file generation; netlink message parsing from captured bytes |
| Package | GitHub-hosted | Build the `.deb`, `lintian`, then `bats` in a trixie container: install, upgrade, remove, purge, reinstall; install over a removed-not-purged `wlanpi-usb-ethernet` fixture; check that nothing is left behind |
| Loopback integration (idea, verify feasibility) | A VM or self-hosted x86 runner with `dummy_hcd` | `modprobe dummy_hcd` gives a software UDC and a host controller on the same machine. Run `apply`, confirm the kernel's own host side enumerates the gadget and a network interface appears, exercise the monitor through unbind and rebind, run teardown. This tests real configfs and real Linux host enumeration without hardware. Depends on the runner kernel shipping `dummy_hcd`. |
| On-device integration | Self-hosted Raspberry Pi (CM4) runner cabled to a Linux host, nightly and on release tags | Install the built `.deb`; 200x apply/teardown; scenarios S1, S3, S5 (Linux only), S7 (shortened), S8, S11; collect `report` as an artifact on failure |
| Manual matrix | Humans, per milestone | Section 9 on Windows, macOS, iOS, Android |

**Release coverage.** All stages build and test for trixie only: the build chroot is trixie, the container for the maintainer-script tests is trixie, `tox` runs `py313` only, and the on-device runner carries the trixie Go image. There is no bookworm job. One extra package-stage test asserts that installing the `.deb` on a bookworm container is refused by its dependencies, so the floor is enforced rather than assumed.

Release follows the org process: `dch` for the changelog, annotated tag, deploy to the packagecloud dev repo, manual promotion.

## 11. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Windows does not bind NCM as expected through `WINNCM`, or not alongside a serial function | Medium | High for NCM-first | Phase 0 test; `legacy` stays the default and safe profile until proven |
| iOS rejects or mishandles a composite device with a serial function | Medium | Medium | `SERIAL_CONSOLE` is per profile and can be off; matrix S12 |
| Python start-up is too slow or unavailable at the point the gadget must be configured | Low | High | Measured in M0a; fallback is a small POSIX shell `apply` with the same profile table generated at build time |
| The dwc2 load-order workaround is still required | Medium | Low | **Partly RESOLVED:** `dwc2` is a module on the Go, so the workaround is available and cheap. If M0a shows it is needed, ship the blacklist plus a separate unit that loads `dwc2` in host mode (A5) |
| UDC `state` poll or `ifname` does not behave as assumed on the Go kernel | Low | Medium | M0a; fallbacks are a 1 s read of `state`, and matching netdevs by parent device instead of name |
| Event-driven monitor misses a failure that ping would have caught | Medium | Medium | M2 ships observe-only and collects field data before recovery is designed |
| Recovery logic reintroduces reset storms | Medium | High | Token bucket, `HOLD`, I1 and I2 regression tests in CI |
| Fixed 169.254 secondary address conflicts with host expectations or with link-local routes on other interfaces | Low | Low | **Reopened (E9):** the address is back as a secondary (D15). High route metric; config switch; matrix S8 |
| mDNS does not answer on the gadget link because of avahi's interface allow-list | High with any new interface name | High: removes the main fallback path | Bridge is named `usb0` (3.3); `selftest` checks the allow-list; M2 exit criterion |
| Other components on the image key on `usb0`/`usb1` in ways not yet found | Low | Medium | **Largely RESOLVED (E7):** consumers found are avahi, wlanpi-common and `wlanpi-stats`; the last two get M0a checks. Keeping the name `usb0` covers all three |
| ~~IPv6 link-local mDNS resolution is unreliable on some host OS~~ | n/a | n/a | **Superseded (E9):** avahi has `use-ipv6=no`, so IPv6 mDNS is not part of the design. Replaced by the next two rows |
| Hosts that do not self-assign 169.254 (Linux, Android) have no name-based fallback | High on those hosts | Medium | Fixed `fe80::1`; USB serial on Linux; documented in the runbook |
| Removing the old package strips `dtoverlay=dwc2` and the Go boots with no UDC | High if unhandled | High: device unreachable over USB | D16: `bootcfg ensure` in postinst, M1 and M5 tests, Phase 0 reproduction |
| This package and a future wlanpi-common both write gadget boot config on the Go | Low today | Medium | Marked block; board-scoped; Q2b |
| Volatile journal loses the evidence of a failure across a reboot | High | Medium | Mandatory on-disk event history, including setup failures (D9) |
| A bridge named `usb0` confuses users or tools that expect a gadget netdev | Low | Low | `status` states it; README; fallback is option (b1) |
| wlanpi-common and this package disagree about the `dwc2` overlay | Low on the Go (wlanpi-common does nothing there, E10); Medium on other boards | High: no gadget | Go: this package owns a marked block. Other boards: warn-only check |
| No PIDs available for per-profile identities | n/a | Low | **RESOLVED:** `1209:2042` is registered to WLAN Pi; profiles differ by `bcdDevice`. Remaining policy question in Q1 |
| Status page and serial console widen the attack surface | Low | Medium | Read-only page bound to `usb0`; console needs login; both can be disabled |
| The WLAN Pi kernel (6.19.x, not an upstream long-term series) carries a half-landed `u_ether`/`f_ncm` lifecycle rework, so `ifname`, netdev lifetime or rebind behave differently from what the plan assumes | Medium | High: affects naming (D4) and recovery steps 3 and 4 | Three M0a checks (7a.2); recovery treats a failed bind as terminal for that function instance; raise with kernel maintainers (Q16) |
| A device is upgraded in place from bookworm to trixie with the old package still installed | Medium | Medium | Old package keeps working until replaced; M5 migration test starts from that state (Q15) |
| Someone installs the package on a bookworm image | Low | Low | `Depends` on `python3 (>= 3.13)` and `systemd (>= 257~)` refuses the install; CI asserts it |
| RNDIS host support disappears from Linux during this project's life | Medium | Medium for the `legacy` profile on Linux hosts only | NCM-first default once the matrix passes; one matrix host without `rndis_host` |
| Two packages installed at once during migration | Medium | High | `Conflicts`, foreign-gadget guard in `apply`, `selftest` leftovers check |

## 12. Open questions

| # | Question | Who can answer |
|---|----------|----------------|
| Q1 | Is `1209:2042` registered to WLAN Pi on pid.codes, and can further PIDs be registered for per-profile identities? **RESOLVED (registration):** https://pid.codes/org/wlanpi lists `0x1209:0x2042` as "WLAN Pi USB Ethernet" (MIT/BSD-3). The new package reuses it and bumps `bcdDevice` per composition (X5). **Still open:** does a composite NCM + CDC-ACM device need its own PID under pid.codes policy? | Project maintainers |
| Q2 | Should the Go image own `dtoverlay=dwc2` in `config.txt`, or must the package manage it? ~~RESOLVED: wlanpi-common touches `config.txt`, so this package verifies and warns only.~~ **CORRECTED (E10):** that held only for R4, M4, M4+ and Pro. For the Go, wlanpi-common does nothing, so this package owns enablement there through a marked block (D16, section 7), subject to the Phase 0 checks. | Image maintainers |
| Q2b | Does wlanpi-common set the `dwc2` overlay for the Go in every mode? **ANSWERED (E10): no, it does nothing for the Go.** `Recommends: wlanpi-common` stays, for board identification only. **Still open:** should Go enablement move into wlanpi-common later so one component owns `config.txt` on every board, and which overlay parameters does the Go need (`dr_mode`, `otg_mode`)? | wlanpi-common maintainers |
| Q3 | Does anything (image manifest, other packages, FPMS, web UI) depend on the name `wlanpi-usb-ethernet`, on `usb0`/`usb1`, or on `/var/run/usb-ethernet-gadget.state`? **ANSWERED (E12, E7):** `apt-cache rdepends wlanpi-usb-ethernet` shows no reverse dependencies, so the rename is safe at the package level and no `Provides` is needed. Interface-name consumers are listed under Q12. Consumers of the old state file were not searched for. | Image and app maintainers |
| Q4 | Is `198.18.42.1/24` fixed by other components, and does the Bluetooth PAN subnet rule (`198.18.43.0/24`) need enforcing here too? **ANSWERED (E11):** a search for `198.18.4` found nothing outside the old package, so the subnet is not pinned. Keep `198.18.42.0/24` as the default for continuity; the PAN overlap check is a config-time warning only. | Maintainers |
| Q5 | Is the replug knock acceptable as a recovery gesture, or is it too easy to trigger by accident (flaky cable)? | Maintainers, after M2 field data |
| Q6 | Why did the old project move away from 169.254 addressing (1.3.0 to 1.4.0) and remove link-local (commit `5422fe0`)? **RESOLVED:** IPv4 link-local was disabled because the gadget's DHCP server cannot assign link-local addresses, so that path never reached the Pi. **Revised by E9:** that rules out 169.254 as the primary scheme only; a fixed 169.254 secondary address is back in the design (D15, 3.3). | Old package maintainers |
| Q7 | Is the journal persistent or volatile on the Go image? **ANSWERED (E15, 2026-11-17): persistent.** `/var/log/journal/<machine-id>/system.journal` is present and survives reboot; earlier E6 was a false read. Keep the on-disk `events.jsonl` history anyway as a compact structured index. | Image maintainers |
| Q8 | Which exact Go hardware revisions and kernel versions must be supported at 1.0? (Running kernel moved to `7.3.0-v8-wlanpi+` as of E13.) | Maintainers |
| Q9 | Is a status page on a new port acceptable, or should it be a route in the existing WLAN Pi web UI? | Web UI maintainers |
| Q10 | Is a self-hosted Raspberry Pi CI runner available to the org? | Maintainers |
| Q11 | Must hosts older than Windows 10 1903 remain supported at all? This decides whether `legacy` can ever stop being the safe profile. | Maintainers |
| Q12 | What else on the Go image names `usb0`/`usb1` or filters by interface? **ANSWERED (E7):** besides the old package's own `.network` files and avahi's allow-list, the real consumers are `/opt/wlanpi-common/wlanpi-config-at-startup.sh` (`otg_link_active()` on M4+) and `/usr/bin/wlanpi-stats` (contents not yet read). `/etc/udev/hwdb.bin` matched but is a binary false positive. **Still open:** what `wlanpi-stats` does with the names (M0a). The search did not cover `/lib/systemd`, `/usr/share` or firewall rules. | Image and app maintainers |
| Q13 | Does the image's avahi build support any include or drop-in mechanism? **ANSWERED (E8): no.** Option (a) in 3.3 is permanently rejected. Closed. | Image maintainers |
| Q15 | **New.** How do fielded Go units move to trixie: reflash, or in-place `apt` upgrade? If in place, the old package rides through the upgrade and the M5 migration test must start from "trixie with `wlanpi-usb-ethernet` installed". | Image maintainers |
| Q16 | **Updated (E13).** Running Go kernel is `7.3.0-v8-wlanpi+`. Upstream `gether_set_ifname()` (`u_ether.c`) rejects any `ifname` without exactly one `%d` (`-EINVAL`) on every u_ether function (`usbrndis%d`, `usbecm%d`, `usbncm%d` are required, not `...0`). **Still open:** which `u_ether`/`f_ncm` lifecycle commits the WLAN Pi kernel carries (the December 2025 refactor, the March 2026 reverts and `device_move` fix, the October 2026 re-register patch), and whether the kernel will track a long-term series or keep following mainline. | Kernel maintainers |
| Q17 | **New.** Is any bookworm-based Go image still going to be published after this package reaches 1.0? If yes, it keeps `wlanpi-usb-ethernet`. | Image maintainers |
| Q14 | Is IPv6 enabled in the image's avahi config, and does anything disable IPv6 on new interfaces? **ANSWERED (E9):** `use-ipv6=no`; kernel IPv6 appears enabled (values to be re-read cleanly in M0a). The fallback design changed as a result (D15, 3.3). **Still open, for image maintainers:** would they consider `use-ipv6=yes` image-wide? Not required by this plan. | Image maintainers |

## 13. Evidence log and what remains unverified

### 13.1 Evidence folded in

E1 to E5 on 2026-11-15, E6 to E12 on 2026-11-16. Gathered on a WLAN Pi Go (`wlanpi-2f3`) and from the pid.codes registry. I did not run these checks myself; they were supplied to me as results.

| # | Evidence | Resolves | Effect on the plan |
|---|----------|----------|--------------------|
| E1 | `zcat /proc/config.gz`: `CONFIG_USB_DWC2=m`, `CONFIG_USB_DWC2_DUAL_ROLE=y`, debug options off. Kernel `6.19.14-v8-wlanpi+` aarch64, `dwc2.ko.xz` in-tree. | Audit A6 (module vs built-in) | D12, `apply` step 7, section 7 blacklist note, M0a row, risk row. Built-in handling reduced to detect and log. Kernel age noted in 5.1. |
| E2 | `/opt/wlanpi-common/wlanpi-config-at-startup.sh` touches `config.txt` (reported by the user; the script itself was not reviewed). **Corrected by E10:** true for other boards, not for the Go. | Q2 | Section 7: verify and warn only, `Recommends: wlanpi-common`, new Q2b. |
| E3 | IPv4 link-local was disabled because the gadget's DHCP server cannot assign link-local addresses. | Q6 | 3.3, 3.5, 4.3, 4.6, M2, S8, risks: 169.254 path removed; IPv6 link-local plus mDNS kept, with the reason stated. **Revised by E9:** 169.254 returns as a secondary address only, which E3's rationale does not rule out. |
| E4 | `grep -r usb0 /etc /usr/lib/wlanpi` found the old package's `usb0.network` and `avahi-daemon.conf: allow-interfaces=usb0, usb1, eth0, wlan0, wlan1, pan0`. | New dependency | D4 and 3.3: bridge is named `usb0` (option b2). Section 7 coexistence, `selftest`, M0a, M2, risks, Q3, Q12 to Q14. |
| E5 | https://pid.codes/org/wlanpi: `0x1209:0x2042` registered to WLAN Pi as "WLAN Pi USB Ethernet" (MIT/BSD-3). | Audit X6, Q1 (registration) | 5.1, 5.3, M3, risk row: reuse the PID, distinguish profiles by `bcdDevice`. |
| E6 | 2026-11-16. `ls /var/log/journal/ && journalctl --disk-usage` printed only the usage line (6.2M). The directory exists but listed empty. Ambiguous: consistent with a volatile journal. | Q7 (partly) | D9 and 4.1: event history mandatory, records setup failures; `report` and runbook no longer assume earlier boots are in the journal; confirming M0a check. |
| E7 | 2026-11-16. `grep -rl -e usb0 -e usb1 /etc /opt/wlanpi-* /usr/lib/wlanpi /usr/bin/wlanpi*` matched the old `.network` files, `avahi-daemon.conf`, `hwdb.bin` (false positive), `wlanpi-config-at-startup.sh` (reads `usb0` RX counters on M4+) and `/usr/bin/wlanpi-stats` (unread). | Q12 | 3.3 recommendation strengthened; `BRIDGE_NAME` effectively fixed; two M0a checks; risk row. |
| E8 | 2026-11-16. No include mechanism found in the image's avahi. | Q13 | Option (a) permanently rejected in 3.3. |
| E9 | 2026-11-16. `avahi-daemon.conf` has `use-ipv6=no` (`publish-a-on-ipv6` commented out). Kernel IPv6 appears enabled, but the sysctl command was malformed, so the values are provisional. | Q14 | D15; 3.3 "Fallback reachability" rewritten; 3.5, 4.2, 4.3, 4.6, M0a, M2, S8, risks. |
| E10 | 2026-11-16. Full read of `wlanpi-config-at-startup.sh`: handles R4, M4, M4+, Pro; for "Oscium Go" it only prints a detection message. Elsewhere it uses `dtoverlay=dwc2,dr_mode=otg` with `otg_mode=0`, reboots on mode switches, and writes `/etc/wlanpi-model`. | Q2b; corrects Q2 and E2 | D16; section 7 boot-configuration bullet rewritten; M0a, M1, risks. |
| E11 | 2026-11-16. A search for `198.18.4` found nothing outside the old package. | Q4 | 3.5 `ADDRESS` note. |
| E12 | 2026-11-16. `apt-cache rdepends wlanpi-usb-ethernet`: no reverse dependencies. | Q3 | Confirms no `Provides` is needed. |
| E13 | 2026-11-17. First Go install (`wlanpi-2f3`, kernel `7.3.0-v8-wlanpi+`, package `0.5.0` over `1.5.4`): `apply` aborted with `EINVAL` writing `functions/rndis.usb0/ifname=usbrndis0`. Upstream `gether_set_ifname()` rejects any `ifname` without exactly one `%d` across all `u_ether` functions (RNDIS was written first, before ECM was reached). | Q16, Q8, D4 | Profiles now write `usbrndis%d`, `usbecm%d`, `usbncm%d`; `ifname` writes keep a best-effort `EINVAL` catch as a safety net, bridge `usb0` is created before UDC bind, and member netdevs are discovered dynamically via configfs/sysfs. |
| E14 | 2026-11-17. Same install: after `wlanpi-usb-ethernet` removal, its configfs gadget `wlanpi` remained bound to `fe980000.usb` (confirming audit A7 on hardware). `bootcfg` worked cleanly on removal (`is_go=True` via `/etc/wlanpi-model`), and old `dtoverlay=dwc2` coexisted with `dr_mode=otg`. | Audit A7, D16 | `apply` tears down a leftover legacy `wlanpi` gadget once on first install (gated by a first-bind marker) instead of refusing migration; other foreign gadgets still fail closed. |
| E15 | 2026-11-17. Same install: `/var/log/journal/<machine-id>/system.journal` is present and survives reboot; Linux hosts rename the gadget netdev to `enx<mac>` via udev predictable naming. | Q7 (corrects E6), §9 host notes | Journal is confirmed **persistent** on the Go image (retain `events.jsonl` as a compact structured index). Note Linux `enx<mac>` host naming for host-matrix testing (irrelevant to device-side naming). |

### 13.2 Still unverified

- **Live gadget tests on the Go:** audit X1 (which function a Linux host activates), I1 (host-swap reset loop in the old package), C4 (`rm -rf` versus ordered configfs teardown).
- **Host binding checks:** Windows 10 and 11 NCM via `WINNCM`, with and without the serial function; macOS and iOS configuration selection and composite handling.
- **Whether the dwc2 load-order workaround is needed** for picky hosts (only its feasibility is settled).
- **The (b2) naming design itself:** configfs `ifname`, a bridge named `usb0`, avahi answering on it over IPv4 and IPv6.
- **`wlanpi-stats`:** what it does with `usb0`/`usb1`, and whether a bridge named `usb0` satisfies it and wlanpi-common's RX-counter check.
- **How peripheral mode is enabled on the Go**, and whether removing the old package loses it (D16 depends on this).
- **The fallback addresses:** which hosts actually self-assign 169.254, whether avahi publishes the secondary address, and `fe80::1` reachability per host OS.
- **Journal persistence on the Go image** (Q7): likely volatile from E6, not confirmed.
- **Kernel IPv6 sysctl values** on the Go: the first reading came from a malformed command.
- **CI:** whether a self-hosted Raspberry Pi runner exists (Q10) and whether `dummy_hcd` is usable on a runner.
- **Avahi include-directory support** (Q13): I found no documentation of one for avahi 0.8, and did not check the image.
- **Trixie items from section 7a:** `ifname` and netdev lifetime on the Go kernel, behaviour of rebind after a failed bind, udev leaving the member names alone, DHCP lease persistence, the CVE-2026-43342 fix status in the WLAN Pi kernel, trixie's exact debhelper version, and how fielded units reach trixie (Q15 to Q17).
- Everything else marked "verify" in sections 3 to 5, and open questions Q5, Q8, Q9, Q10, Q11, Q15 to Q17, plus the open parts of Q1, Q2b, Q12 and Q14.
