.PHONY: test check install uninstall

test:
	python3 -m unittest discover -s tests -v

check: test
	python3 -c 'import ast,pathlib,json; [ast.parse(path.read_text()) for path in (pathlib.Path("bin/pxe-dust"), pathlib.Path("tools/install-system.py"))]; json.loads(pathlib.Path("manifest.json").read_text())'
	git diff --check

# Run as your normal user. Only fixed system utilities are elevated.
# This deliberately does not install packages, enable the widget, or start DHCP.
install:
	python3 -I tools/install-system.py

uninstall:
	/usr/local/bin/pxe-dust uninstall
