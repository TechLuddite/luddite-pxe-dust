.PHONY: test check install uninstall

test:
	python3 -m unittest discover -s tests -v

check: test
	python3 -c 'import ast,pathlib,json; ast.parse(pathlib.Path("bin/pxe-dust").read_text()); json.loads(pathlib.Path("manifest.json").read_text())'
	git diff --check

# Run as your normal user. Only the system install utility is elevated.
# This deliberately does not install packages, enable the widget, or start DHCP.
install:
	@if systemctl is-active --quiet pxe-dust.service; then echo "Stop Luddite PXE Dust before installing an update." >&2; exit 1; fi
	sudo install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/bin
	sudo install -o root -g root -m 0755 bin/pxe-dust /usr/local/libexec/pxe-dust
	sudo install -o root -g root -m 0755 bin/pxe-dust /usr/local/bin/pxe-dust
	sudo install -o root -g root -m 0644 system/pxe-dust.service /etc/systemd/system/pxe-dust.service
	sudo install -o root -g root -m 0644 system/org.techluddite.pxe-dust.policy /usr/share/polkit-1/actions/org.techluddite.pxe-dust.policy
	sudo install -d -o root -g root -m 0755 /var/lib/pxe-dust
	@receipt=$$(mktemp); trap 'rm -f -- "$$receipt"' EXIT; \
	python3 -I bin/pxe-dust receipt > "$$receipt" && \
	sudo install -o root -g root -m 0644 "$$receipt" /var/lib/pxe-dust/installation.json
	sudo systemctl daemon-reload

uninstall:
	/usr/local/bin/pxe-dust uninstall
