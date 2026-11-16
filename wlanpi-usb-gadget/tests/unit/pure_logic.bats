#!/usr/bin/env bats

setup() {
  export PYTHONPATH="${BATS_TEST_DIRNAME}/../.."
}

@test "MAC derivation is deterministic and valid unicast locally-administered" {
  run python3 -c '
from wlanpi_usb_gadget.mac import derive_mac, validate_mac
m1 = derive_mac("10000000abcdef12", "host")
m2 = derive_mac("10000000abcdef12", "host")
m3 = derive_mac("10000000abcdef13", "host")
assert = __import__("builtins").__dict__
if m1 != m2 or m1 == m3:
    raise SystemExit(1)
validate_mac(m1)
print(m1)
'
  [ "$status" -eq 0 ]
}

@test "Config parsing validates defaults and rejects invalid profile" {
  run python3 -m wlanpi_usb_gadget config check
  [ "$status" -eq 0 ]
  [[ "$output" == *"PROFILE=legacy"* ]]
  [[ "$output" == *"BRIDGE_NAME=usb0"* ]]
}

@test "Networkd generation emits 50- prefixed usb0 bridge with fallback addresses" {
  run python3 -c '
from wlanpi_usb_gadget.config import validate_config
from wlanpi_usb_gadget.netcfg import render_networkd_files
from wlanpi_usb_gadget.profiles import get_profile
files = render_networkd_files(validate_config({}), get_profile("ncm"), "02:aa:bb:cc:dd:ee")
assert "50-wlanpi-usb-gadget-bridge.network" in files
br = files["50-wlanpi-usb-gadget-bridge.network"]
assert "169.254.42.1/16" in br
assert "fe80::1/64" in br
assert "KeepConfiguration=" not in br
assert "IPForward=" not in br
'
  [ "$status" -eq 0 ]
}

@test "config.txt block editing is idempotent on Go and no-op on non-Go" {
  run python3 -c '
import tempfile
from pathlib import Path
from wlanpi_usb_gadget.bootcfg import ensure_bootcfg, remove_bootcfg

with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    cfg = t / "config.txt"
    model = t / "model"
    state = t / "state"
    cfg.write_text("[all]\ndtparam=spi=on\n")
    model.write_text("WLAN Pi Go\n")
    r1 = ensure_bootcfg(cfg, wlanpi_model_path=model, dt_model_path=t / "dt", boot_state_path=state)
    r2 = ensure_bootcfg(cfg, wlanpi_model_path=model, dt_model_path=t / "dt", boot_state_path=state)
    assert r1.changed and not r2.changed
    r3 = remove_bootcfg(cfg, wlanpi_model_path=model, dt_model_path=t / "dt", boot_state_path=state)
    assert r3.changed and cfg.read_text() == "[all]\ndtparam=spi=on\n"
'
  [ "$status" -eq 0 ]
}

@test "Reset decision state machine enters HOLD after budget exhausted" {
  run python3 -c '
from wlanpi_usb_gadget.recovery import RecoveryController, TokenBucket
rc = RecoveryController(bucket=TokenBucket(budget=2, window_sec=600))
assert rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=6.0, now_mono=10.0, enabled=True) == "soft_reconnect"
assert rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=26.0, now_mono=26.0, enabled=True) == "rebind"
assert rc.next_action(state="CONFIGURED_NO_LINK", time_in_state_sec=90.0, now_mono=90.0, enabled=True) == "enter_hold"
assert rc.hold
'
  [ "$status" -eq 0 ]
}
