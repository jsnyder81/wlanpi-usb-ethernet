#!/usr/bin/env bats

@test "debian maintainer scripts pass shellcheck" {
  run shellcheck "${BATS_TEST_DIRNAME}/../../debian/postinst" \
                 "${BATS_TEST_DIRNAME}/../../debian/prerm" \
                 "${BATS_TEST_DIRNAME}/../../debian/postrm"
  [ "$status" -eq 0 ]
}

@test "debian/control enforces trixie floor and Conflicts/Replaces" {
  grep -q "python3 (>= 3.13)" "${BATS_TEST_DIRNAME}/../../debian/control"
  grep -q "systemd (>= 257~)" "${BATS_TEST_DIRNAME}/../../debian/control"
  grep -q "Conflicts: wlanpi-usb-ethernet" "${BATS_TEST_DIRNAME}/../../debian/control"
  grep -q "Replaces: wlanpi-usb-ethernet" "${BATS_TEST_DIRNAME}/../../debian/control"
}

@test "legacy composite gadget modules are blacklisted in /usr/lib/modprobe.d" {
  local conf="${BATS_TEST_DIRNAME}/../../modprobe/wlanpi-usb-gadget.conf"
  [ -f "$conf" ]
  grep -q "modprobe/wlanpi-usb-gadget.conf /usr/lib/modprobe.d/" "${BATS_TEST_DIRNAME}/../../debian/install"
  for mod in g_ether g_ncm g_cdc g_serial; do
    grep -q "^blacklist ${mod}$" "$conf"
    grep -q "^install ${mod} /bin/false$" "$conf"
  done
}
