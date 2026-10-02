# Contributing

Luddite PXE Dust is an Omarchy Quattro bar widget with a Python system helper.
Read `README.md` for installation and networking behavior, and
`docs/ACCEPTANCE.md` for observed hardware results and remaining checks.

## Implementation constraints

- Existing LAN mode accepts a connected physical Ethernet or Wi-Fi host
  interface and supplies proxy DHCP only. The existing router allocates leases.
  Wi-Fi hosts require an access point that bridges wired targets to the same LAN.
- Dedicated mode accepts only a disconnected, unaddressed physical Ethernet
  adapter. Never repurpose the host's active connection or a Wi-Fi adapter.
- Keep `BOOTIF=01-${netX/mac:hexhyp}` in the iPXE kernel arguments. The Omarchy
  4.0.4 early DHCP client fails interface discovery with plain `ip=dhcp`; the
  ISO hook uses BOOTIF's MAC to choose an explicit Linux interface.
- Panel defaults come from widget settings `isoPath`, `sha256`, and
  `networkMode`. Keep paths and network identifiers out of source defaults.
- Privileged execution uses the root-owned `/usr/local/libexec/pxe-dust`.
  Install reviewed files with the system install utility; do not execute
  checkout code as root. Update the installation receipt with helper changes.
- Do not start or enable serving as a side effect of installation, widget
  loading, or tests. Leave a user-stopped service stopped. Preserve an active
  session's mode/interface when an authorized update needs a restart.
- LAN cleanup removes only the session's owned firewall chain and jump. Leave
  NetworkManager, addresses, routes, DNS, and unrelated firewall rules alone.
- The installed widget is under `~/.config/omarchy/plugins/techluddite.pxe-dust`.
  Never edit packaged `/usr/share/omarchy` files. An explicit
  `omarchy restart shell` may be needed to discard cached QML after an update.

## Verification and current evidence

Use `make check` for unit tests, syntax checks, and whitespace checks.
`python3 tests/qml_smoke.py` checks the actual Quattro components and mode
selection. `python3 tests/network_smoke.py /usr/bin/dnsmasq` exercises proxy DHCP,
TFTP, and firewall cleanup in private namespaces. The optional
`tests/initramfs_network_smoke.py` checks the affected ISO's real DHCP client;
see the README for extraction and usage. These tests do not need host services.

On 2026-10-02 UTC, a Wi-Fi host served Omarchy 4.0.4 to a wired UEFI x86-64
desktop on its existing LAN. The user reached the full interactive installer
after the BOOTIF fix, then stopped serving through the widget and authenticated
successfully. The stop was independently verified: inactive service, no PXE
listeners, no session/namespace, and no `PXDUST_` firewall entries. A complete
disk installation and boot from the installed disk remain pending on a spare PC.
Do not describe this result as a completed installation or a fully passed
hardware acceptance matrix. Full DHCP on the existing LAN is not implemented;
the successful boot did not require it.
