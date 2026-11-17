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

  # Case 1: rc state -> removes conffile directly without nested dpkg --purge
  printf "# old\nblacklist dwc2\n" > "${tmp}/legacy.conf"
  cat > "${tmp}/bin/dpkg-query" <<'EOF'
#!/bin/sh
echo "deinstall ok config-files"
EOF
  cat > "${tmp}/bin/dpkg" <<'EOF'
#!/bin/sh
echo "ERROR: nested dpkg invocation is forbidden inside postinst" >&2
exit 2
EOF
  chmod +x "${tmp}/bin/dpkg-query" "${tmp}/bin/dpkg"

  run env PATH="${tmp}/bin:$PATH" WLANPI_LEGACY_MODPROBE_CONFFILE="${tmp}/legacy.conf" sh -c ". ${tmp}/postinst_lib.sh && cleanup_legacy_modprobe_conffile"
  [ "$status" -eq 0 ]
  [ ! -e "${tmp}/legacy.conf" ]
  [[ "$output" != *"purging"* ]]

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

@test "postinst cleans residual legacy networkd conffiles safely" {
  local tmp netdir
  tmp="$(mktemp -d)"
  netdir="${tmp}/network"
  mkdir -p "${tmp}/bin" "$netdir"

  sed '/^case "\$1" in/,$d' "${BATS_TEST_DIRNAME}/../../debian/postinst" > "${tmp}/postinst_lib.sh"

  cat > "${netdir}/usb0.network" <<'EOF'
[Match]
Name=usb0

[Network]
Address=198.18.42.1/24
DHCPServer=yes
LinkLocalAddressing=no
IPv6AcceptRA=no

[Link]
RequiredForOnline=no

[DHCPServer]
PoolOffset=9
PoolSize=11
EmitDNS=no
EmitRouter=no
EOF

  cat > "${netdir}/usb1.network" <<'EOF'
[Match]
Name=usb1

[Network]
Address=198.18.42.1/24
DHCPServer=yes
LinkLocalAddressing=no
IPv6AcceptRA=no

[Link]
RequiredForOnline=no

[DHCPServer]
PoolOffset=9
PoolSize=11
EmitDNS=no
EmitRouter=no
EOF

  # Case 1: rc state -> removes conffiles directly without nested dpkg --purge
  cat > "${tmp}/bin/dpkg-query" <<'EOF'
#!/bin/sh
echo "deinstall ok config-files"
EOF
  cat > "${tmp}/bin/dpkg" <<'EOF'
#!/bin/sh
echo "ERROR: nested dpkg invocation is forbidden inside postinst" >&2
exit 2
EOF
  chmod +x "${tmp}/bin/dpkg-query" "${tmp}/bin/dpkg"

  run env PATH="${tmp}/bin:$PATH" WLANPI_LEGACY_NETWORKD_DIR="$netdir" sh -c ". ${tmp}/postinst_lib.sh && cleanup_legacy_networkd_conffiles"
  [ "$status" -eq 0 ]
  [ ! -e "${netdir}/usb0.network" ]
  [ ! -e "${netdir}/usb1.network" ]
  [[ "$output" != *"purging"* ]]

  # Restore files for Case 2
  cat > "${netdir}/usb0.network" <<'EOF'
[Match]
Name=usb0

[Network]
Address=198.18.42.1/24
DHCPServer=yes
LinkLocalAddressing=no
IPv6AcceptRA=no

[Link]
RequiredForOnline=no

[DHCPServer]
PoolOffset=9
PoolSize=11
EmitDNS=no
EmitRouter=no
EOF
  sed 's/Name=usb0/Name=usb1/' "${netdir}/usb0.network" > "${netdir}/usb1.network"

  # Case 2: package absent + matching signature -> deletes orphaned files
  cat > "${tmp}/bin/dpkg-query" <<'EOF'
#!/bin/sh
echo "unknown ok not-installed"
EOF
  chmod +x "${tmp}/bin/dpkg-query"

  run env PATH="${tmp}/bin:$PATH" WLANPI_LEGACY_NETWORKD_DIR="$netdir" sh -c ". ${tmp}/postinst_lib.sh && cleanup_legacy_networkd_conffiles"
  [ "$status" -eq 0 ]
  [ ! -e "${netdir}/usb0.network" ]
  [ ! -e "${netdir}/usb1.network" ]

  # Case 3: user-modified usb0.network -> untouched
  cat > "${netdir}/usb0.network" <<'EOF'
[Match]
Name=usb0

[Network]
Address=10.99.0.1/24
DHCPServer=yes
EOF
  run env PATH="${tmp}/bin:$PATH" WLANPI_LEGACY_NETWORKD_DIR="$netdir" sh -c ". ${tmp}/postinst_lib.sh && cleanup_legacy_networkd_conffiles"
  [ "$status" -eq 0 ]
  [ -f "${netdir}/usb0.network" ]

  rm -rf "$tmp"
}

@test "postinst schedules deferred reapply on upgrade only" {
  local tmp flag systemd_dir
  tmp="$(mktemp -d)"
  flag="${tmp}/run/reapply-pending"
  systemd_dir="${tmp}/systemd"
  mkdir -p "$systemd_dir"

  sed '/^case "\$1" in/,$d' "${BATS_TEST_DIRNAME}/../../debian/postinst" > "${tmp}/postinst_lib.sh"

  # Fresh install ($2 empty): no reapply flag
  run env DPKG_ROOT="" WLANPI_REAPPLY_FLAG="$flag" WLANPI_SYSTEMD_RUN_DIR="$systemd_dir" \
    sh -c ". ${tmp}/postinst_lib.sh && schedule_upgrade_reapply ''"
  [ "$status" -eq 0 ]
  [ ! -e "$flag" ]

  # Upgrade inside DPKG_ROOT chroot: no reapply flag
  run env DPKG_ROOT="/mnt/root" WLANPI_REAPPLY_FLAG="$flag" WLANPI_SYSTEMD_RUN_DIR="$systemd_dir" \
    sh -c ". ${tmp}/postinst_lib.sh && schedule_upgrade_reapply '0.5.4'"
  [ "$status" -eq 0 ]
  [ ! -e "$flag" ]

  # Live upgrade (0.5.4 -> newer) with systemd running: creates reapply flag
  run env DPKG_ROOT="" WLANPI_REAPPLY_FLAG="$flag" WLANPI_SYSTEMD_RUN_DIR="$systemd_dir" \
    sh -c ". ${tmp}/postinst_lib.sh && schedule_upgrade_reapply '0.5.4'"
  [ "$status" -eq 0 ]
  [ -f "$flag" ]

  rm -rf "$tmp"
}
