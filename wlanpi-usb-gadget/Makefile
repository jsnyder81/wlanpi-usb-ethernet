.PHONY: test lint bats integration build-deb clean

test:
	python3 -m unittest discover -s tests/unit -v

lint:
	shellcheck bin/wlanpi-usb-gadget debian/postinst debian/postrm debian/prerm tests/integration/test_dummy_hcd.sh

bats:
	@command -v bats >/dev/null 2>&1 || { echo "bats not installed; skipping bats suite"; exit 0; }
	bats tests/unit/*.bats tests/maintscripts/*.bats

integration:
	./tests/integration/test_dummy_hcd.sh

build-deb:
	dpkg-buildpackage -us -uc -b

clean:
	rm -rf .pytest_cache __pycache__ wlanpi_usb_gadget/__pycache__ tests/__pycache__ tests/unit/__pycache__ debian/wlanpi-usb-gadget debian/.debhelper debian/debhelper-build-stamp debian/files debian/*.substvars debian/*.log
