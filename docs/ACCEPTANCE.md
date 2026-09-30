# Hardware acceptance

This checklist is pending. Automated tests do not establish that firmware can
boot a real Omarchy release or that an installation completes.

Run both topologies: first a host with one Ethernet adapter on an existing
router/switch LAN, then a spare USB Ethernet adapter on an isolated installation
switch. Use a target with at least 16 GiB RAM and a disposable target disk.
Record the Omarchy ISO URL and SHA-256, host package versions, target firmware,
adapter model, router/DHCP implementation, and observed result.

## Existing LAN / single-interface host

1. Confirm ordinary host internet access works, and record its addresses, routes,
   DNS and NetworkManager state. Start with `pxe-dust start INTERFACE --lan`.
2. Confirm those values and internet connectivity remain unchanged. Inspect
   DHCP packets: the router alone assigns addresses; Luddite PXE Dust responds only to
   PXE clients with boot information. Test a second ordinary DHCP client too.
3. Confirm the session's iptables chain permits boot traffic only through the
   selected interface, and that HTTP/TFTP destinations are the selected IPv4.
   Verify against the actual host firewall, including UFW if enabled.
4. Boot UEFI architecture 7 and 9 clients, including iPXE's second DHCP round and
   PXE boot-server requests on UDP 4011. Complete the interactive installation.
5. During serving, change the host's IPv4 address or disconnect its adapter.
   Confirm the service stops, removes its firewall rules, and does not restore,
   disconnect or otherwise modify the host's normal connection. Restart and
   confirm the new address appears in the boot script.
6. Stop, force a failed start, and kill the supervisor. Confirm cleanup removes
   only the owned firewall chain/jump, leaving all pre-existing rules intact.
7. Confirm subnet/VLAN boundaries and DHCP snooping constraints are documented
   for the tested network. Firmware compatibility is not established by a
   synthetic DHCP test alone.

## Dedicated network and common installer checks

1. Install prerequisites and the system helper; load the widget. Confirm that
   loading the widget starts no server and modifies no network configuration.
2. Confirm that Start refuses the host's active connection. Disconnect the
   dedicated adapter. If NetworkManager leaves its administrative flag up,
   explicitly bring that adapter down with `sudo ip link set dev INTERFACE down`.
   Confirm it has no addresses/routes before proceeding.
3. Prepare the official ISO. Confirm a wrong checksum preserves an already
   cached image. Retry with the published digest and, separately, verify the
   publisher signature according to upstream instructions.
4. Start serving. Confirm that only the selected adapter moves into
   `/run/netns/pxe-dust`, DHCP is scoped there, and the host's main connection
   continues working. Confirm there is no change to the host firewall.
5. Boot a target through UEFI IPv4 PXE with Secure Boot disabled. Observe its
   DHCP lease, iPXE download, kernel/initramfs download, and complete live-root
   download. Confirm the installer opens and can access the bundled mirror.
6. Perform an interactive installation to the disposable disk. Remove network
   boot from the first boot position and boot the installed system from disk.
   Confirm the Omarchy desktop works. Repeat with a second target/firmware.
7. Boot two clients simultaneously and confirm byte-range probes return one
   byte instead of a second full live-root transfer.
8. Reload the desktop shell during a transfer. Confirm the server continues
   and the newly loaded widget reflects the service state.
9. Stop from the panel and CLI. Confirm all server processes exit, the namespace
   disappears, the adapter returns down, and its original NetworkManager
   management flag is restored. NetworkManager may subsequently reconnect it
   according to the user's pre-existing autoconnect settings.
10. Exercise startup failure, server crash, SIGKILL of the supervisor, and an
    unplugged adapter. Confirm ExecStopPost restores what it can and retains
    the recovery journal on failure. Reconnect the original adapter and recover.
11. Stop, uninstall, and remove the widget in both modes. Confirm the privileged helper,
    service, polkit policy and session firewall rules are gone, and that the cache alone remains under
    `/var/lib/pxe-dust`. Existing distribution packages and journal data remain.

Record boot failures before claiming support for any new ISO layout. A future
release should add a reproducible full-install VM test with OVMF and a pinned
Omarchy ISO, then run this physical-firmware matrix before release.
