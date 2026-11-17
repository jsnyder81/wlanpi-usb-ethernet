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
  local conf="${BATS_TEST_DIRNAME}/../../modprobe/wlanpi-usb-gadget-legacy-disable.conf"
  [ -f "$conf" ]
  grep -q "modprobe/wlanpi-usb-gadget-legacy-disable.conf /usr/lib/modprobe.d/" "${BATS_TEST_DIRNAME}/../../debian/install"
  for mod in g_ether g_ncm g_cdc g_serial; do
    grep -q "^blacklist ${mod}$" "$conf"
    grep -q "^install ${mod} /bin/false$" "$conf"
  done
}

@test "postinst cleans residual legacy modprobe conffile safely" {
  local tmp
  tmp="$(mktemp -d)"
  mkdir -p "${tmp}/bin"

  # Prepare a testable copy of postinst without running live system actions
  sed '/^case "\$1" in/,$d' "${BATS_TEST_DIRNAME}/../../debian/postinst" > "${tmp}/postinst_lib.sh"

  # Case 1: rc state -> calls dpkg --purge wlanpi-usb-ethernet
  printf "# old\nblacklist dwc2\n" > "${tmp}/legacy.conf"
  cat > "${tmp}/bin/dpkg-query" <<'EOF'
#!/bin/sh
echo "deinstall ok config-files"
EOF
  cat > "${tmp}/bin/dpkg" <<EOF
#!/bin/sh
echo "\$*" > "${tmp}/dpkg.called"
EOF
  chmod +x "${tmp}/bin/dpkg-query" "${tmp}/bin/dpkg"

  run env PATH="${tmp}/bin:$PATH" WLANPI_LEGACY_MODPROBE_CONFFILE="${tmp}/legacy.conf" sh -c ". ${tmp}/postinst_lib.sh && cleanup_legacy_modprobe_conffile"
  [ "$status" -eq 0 ]
  grep -q -- "--purge wlanpi-usb-ethernet" "${tmp}/dpkg.called"

  # Case 2: package absent + matching signature -> deletes orphaned file
  cat > "${tmp}/bin/dpkg-query" <<'EOF'
#!/bin/sh
echo "unknown ok not-installed"
EOF
  chmod +x "${tmp}/bin/dpkg-query"
  run env PATH="${tmp}/bin:$PATH" WLANPI_LEGACY_MODPROBE_CONFFILE="${tmp}/legacy.conf" sh -c ". ${tmp}/postinst_lib.sh && cleanup_legacy_modprobe_conffile"
  [ "$status" -eq 0 ]
  [ ! -e "${tmp}/legacy.conf" ]

  # Case 3: non-matching user file -> untouched
  printf "blacklist dwc2\noptions custom_mod param=1\n" > "${tmp}/legacy.conf"
  run env PATH="${tmp}/bin:$PATH" WLANPI_LEGACY_MODPROBE_CONFFILE="${tmp}/legacy.conf" sh -c ". ${tmp}/postinst_lib.sh && cleanup_legacy_modprobe_conffile"
  [ "$status" -eq 0 ]
  [ -f "${tmp}/legacy.conf" ]

  rm -rf "$tmp"
}
