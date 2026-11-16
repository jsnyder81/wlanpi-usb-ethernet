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
