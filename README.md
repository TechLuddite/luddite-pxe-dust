# Luddite PXE Dust

Turn an Omarchy computer into a temporary Ethernet installation station.
A Quattro bar panel prepares a verified Omarchy ISO and starts or stops a
UEFI x86-64 PXE server. Targets run the normal interactive installer.

**Version 0.2 is experimental.** One Ethernet or Wi-Fi host interface is enough:
the default **Existing LAN** mode keeps the host connected and uses proxy DHCP
alongside your router. The original **Dedicated network** mode is also available.
A complete installation on physical hardware is still required. Unattended disk
selection, BIOS boot, ARM, Wi-Fi client PXE, internet sharing and Secure Boot
are not included.

A Wi-Fi host and wired UEFI desktop on an existing LAN have successfully booted
Omarchy 4.0.4 into the full interactive installer. Installation to disk was
deliberately deferred. See [observed hardware results](docs/ACCEPTANCE.md).

## How it works

```text
Omarchy panel → authenticated helper → systemd service
                                          │
               existing LAN (proxy DHCP) OR dedicated link (DHCP)
                                          │
                    TFTP → iPXE → HTTP → Omarchy installer
```

| Mode | Host connection | Address assignment | Host firewall |
| --- | --- | --- | --- |
| Existing LAN (default) | Stays connected on its current Ethernet or Wi-Fi interface | Existing router/DHCP server | Temporary rules scoped to the chosen interface and LAN |
| Dedicated network | Selected disconnected adapter moves to an isolated namespace | Luddite PXE Dust supplies a private pool | Unchanged |

**Existing LAN:** select the connected adapter. It must have exactly one usable
IPv4 address. Luddite PXE Dust leaves NetworkManager, addresses, routes and DNS alone.
Dnsmasq provides only PXE boot information; it neither allocates addresses nor
answers ordinary DHCP clients. HTTP binds to the adapter's current IPv4 address,
and the iPXE script is generated for that address each session. If the address,
subnet or adapter identity changes, boot services stop rather than advertise
stale URLs. Restart to use the new address.

Temporary IPv4 iptables INPUT rules permit DHCP discovery on the selected
interface, and TFTP, PXE port 4011 and HTTP from its subnet to the host's selected
address. The session uses its own uniquely named chain; stop/recovery removes
only that chain and its jump. Existing rules and firewall configuration files
are preserved. This integrates with the usual UFW/iptables INPUT rules;
additional native nftables chains, restrictive OUTPUT policies, firewalld or
network ACLs may require separate administrator configuration. A firewall
reload during a deployment can remove these temporary rules.

**Dedicated network:** the selected adapter must be disconnected, down, and
have no addresses or routes. Luddite PXE Dust temporarily removes it from NetworkManager
management, moves it into a network namespace, and assigns `192.168.173.1/24`.
DHCP supplies `192.168.173.20–200`, with no router or DNS server. Stop returns
the adapter in the down state and restores its prior management flag.

Both modes use the distribution's iPXE EFI binary and HTTP port 8080. A systemd
stop hook retries cleanup after a crash. The service is not enabled at boot.
The boot script passes the active iPXE NIC's MAC as `BOOTIF`; the ISO's early
network hook uses it to select the same Linux interface for DHCP.
Reloading or removing the widget does not stop an active deployment;
`pxe-dust stop` remains available independently of the widget.

## Install

Requirements: Omarchy Quattro, Python 3, systemd, NetworkManager, polkit,
iproute2, libarchive (`bsdtar`), dnsmasq, and ipxe. Existing LAN mode also requires
iptables. A physical Ethernet adapter or a connected Wi-Fi adapter on the same
LAN is sufficient for the host. Targets need UEFI IPv4 network boot on the same
LAN/broadcast domain. A second host adapter is only useful for the optional
dedicated mode.

Install the missing distribution packages explicitly:

```sh
omarchy pkg add dnsmasq ipxe
```

From this reviewed checkout, install the root-owned helper and system units:

```sh
make check
make install
```

`make install` uses `sudo install` to copy files; it does not run checkout code
as root, install dependencies, enable a service, or start a network. Privileged
runtime actions execute only `/usr/local/libexec/pxe-dust`, with an administrator
authentication prompt. There is no passwordless privilege rule.

For local development, copy this repository into
`~/.config/omarchy/plugins/techluddite.pxe-dust`, then run:

```sh
omarchy-shell shell rescanPlugins
omarchy plugin enable techluddite.pxe-dust
```

After publication, the same repository can be installed through
`omarchy plugin add`. The widget still requires the separate system setup.

## Prepare an image

Download an Omarchy ISO and its published SHA-256 from the
[official download](https://omarchy.org). The
[ISO project](https://github.com/omacom/omarchy-iso) documents checksum and
signature verification. Verify its signature separately if you need publisher
authentication; a user-supplied checksum alone does not establish provenance.

In the panel, enter the absolute ISO path and the 64-character published digest,
then choose **Verify and prepare image**. Or use:

```sh
pxe-dust prepare /absolute/path/to/omarchy.iso YOUR_64_CHARACTER_SHA256
```

Preparation streams and checks a private copy before reading the archive, then
uses unprivileged `bsdtar` to extract only a matching kernel/initramfs pair, the
complete live squashfs, and available checksum/signature files. It supports the
`linux` and `linux-t2` ISO layouts under `arch/`. The bundled package mirror
remains inside the live filesystem. CMS rootfs verification is requested when
the ISO provides its signature. The downloaded ISO is never modified.

The cache holds one prepared release. Replacing it requires a stopped service.
A wrong checksum leaves the previous cache intact. Allow disk space for the
ISO copy, extracted assets, and any previous cache during preparation. The
input and live root are capped at 12 GiB, kernel/initramfs at 512 MiB each.

Arch's HTTP boot copies the live filesystem into target RAM; allow sufficient
RAM for the full squashfs plus the running installer. Start validation on a
16 GiB target. This is not a guarantee that every future ISO will fit.

## Serve

For an Ethernet host, leave it connected to your router or switch. A Wi-Fi
host can use its existing connection if the access point bridges Wi-Fi and
the wired target network into the same broadcast domain. Guest networks,
client isolation, VLAN separation, or DHCP/PXE filtering can prevent discovery
or transfers. This does not add Wi-Fi network boot to target firmware.

In the panel, keep **Mode: Existing LAN**, select the adapter, acknowledge
**Serve the installer on this LAN**, and click **Start serving**. Your router
continues supplying addresses and internet access to the host and targets.

```sh
pxe-dust status
pxe-dust start enp1s0 --lan
journalctl -u pxe-dust -f
pxe-dust stop
```

For a separate installation cable or switch, choose **Mode: Dedicated network**,
disconnect that adapter in the network settings, and acknowledge the dedicated
network. The equivalent command is:

```sh
pxe-dust start enp1s0 --dedicated
```

Existing LAN mode accepts a physical Ethernet or Wi-Fi adapter with one
usable IPv4 address. Dedicated mode accepts only an unaddressed, down physical
Ethernet adapter. Neither mode accepts virtual adapters or bridge members.
The CLI requires one explicit mode and never guesses.

Choose UEFI IPv4 network boot on the target. Secure Boot must be disabled for
this initial implementation. The normal installer chooses the target disk,
user, and other installation settings. Boot-service readiness is not an
installation-complete signal. This release reports DHCP activity in the
journal; it does not claim per-client installation progress.

Boot services are unauthenticated and serve public installer assets. Only use
them on a LAN you administer. Use Existing LAN mode alongside your router;
never attach Dedicated network mode's address server to that LAN. Avoid running
multiple PXE servers on the same segment. The service exposes no credentials,
installation configuration or remote-control API. Only machines deliberately
booted from the network enter the installer.

## Troubleshooting

- **Missing prerequisites:** install the named distribution packages and run
  `make install` for the system helper.
- **Dedicated adapter still active:** disconnect it; do not select your working LAN
  connection. If it still has its administrative `UP` flag, bring only that
  adapter down with `sudo ip link set dev INTERFACE down`. USB Ethernet is a
  convenient dedicated adapter.
- **No unique LAN IPv4 address:** connect the selected network adapter and obtain an
  address from the existing router first. Multiple IPv4 addresses are currently
  rejected rather than choosing one silently.
- **Address changed:** the service stops and removes its firewall rules. Restart
  to advertise the new address. A router DHCP reservation can keep it stable.
- **Proxy discovery fails:** clients must share the broadcast domain; DHCP
  snooping, VLAN boundaries, other PXE servers and firmware compatibility can
  affect discovery. Some networks require administrator changes.
- **Port conflict:** stop any other service already using the selected address's
  PXE/TFTP/HTTP ports; Luddite PXE Dust does not stop unrelated services.
- **Failed startup:** inspect `journalctl -u pxe-dust -b`. The helper checks that
  HTTP and dnsmasq remain running before reporting readiness.
- **Incomplete cleanup:** stop the service, reconnect the original adapter if
  it was unplugged, then run `pxe-dust recover`. It refuses to restore management
  to a replacement adapter with a different identity. In LAN mode, recovery
  retries removal of the session firewall rules without changing the adapter.
- **Interrupted image replacement:** inspect `/var/lib/pxe-dust/previous-image`.
  It is deliberately retained rather than silently deleted on the next attempt.
- **No boot:** check UEFI IPv4 PXE, Secure Boot, cable/link, and target RAM. Real
  compatibility with other firmware and disk installation still need validation.
- **Linux reports `SIOCGIFFLAGS: No such device`:** ensure the served
  `/boot.ipxe` contains `BOOTIF=01-${netX/mac:hexhyp}`. Restart an active service
  after updating its helper. This handoff avoids the Omarchy 4.0.4 early DHCP
  client's failing interface-discovery path. In Existing LAN mode, Linux still
  obtains its address from the router.

## Files and network access

Installed files:

- `/usr/local/libexec/pxe-dust` — root-owned runtime helper.
- `/usr/local/bin/pxe-dust` — identical CLI copy.
- `/etc/systemd/system/pxe-dust.service` — on-demand service.
- `/usr/share/polkit-1/actions/org.techluddite.pxe-dust.policy` — admin auth policy.

Runtime data:

- `/var/lib/pxe-dust/` — root-owned image cache, operation lock, requested
  interface, installation receipt and recovery journal. Image metadata includes
  its digest and size.
- `/run/pxe-dust/` — generated dnsmasq configuration, removed by systemd.
- `/run/netns/pxe-dust` — dedicated mode only; removed on cleanup.
- `PXDUST_<random suffix>` — LAN mode's temporary IPv4 iptables chain and INPUT
  jump, identified in the recovery journal and removed on cleanup.
- The system journal contains service errors and DHCP client identifiers under
  the machine's existing retention policy.

There are no runtime outbound download endpoints. On the selected network,
the service exposes DHCP/proxy DHCP UDP 67, PXE UDP 4011 in LAN mode, TFTP UDP 69
and transfer ports 40000–40031, and HTTP TCP 8080. HTTP allows only `/boot.ipxe`, `/vmlinuz`, `/initramfs.img`, and known
`/arch/x86_64/airootfs.*` assets, with bounded concurrency and transfer time.
Directory listings, arbitrary file access, and writes are not available.

## Removing

Stop and uninstall the system component **before** removing the widget:

```sh
pxe-dust uninstall
omarchy plugin remove techluddite.pxe-dust
```

The uninstaller stops the service, retries interface/firewall cleanup, and checks the
installed files against the installation receipt before removing them. It
refuses modified files. The cached image and recovery/request metadata in
`/var/lib/pxe-dust` are retained; distribution packages and journal entries are
also retained. It removes the service, helper copies, and polkit policy, and
reloads systemd. No sudoers rules, persistent firewall rules, or boot-enabled
services are created.

## Development and validation

```sh
make check
python3 tests/qml_smoke.py
python3 tests/network_smoke.py /usr/bin/dnsmasq
```

The Python suite covers archive selection, mismatched images, process output
limits and deadlines, HTTP ranges and traversal, and failure cleanup. The QML
smoke check loads the real Omarchy components in a separate hidden shell.
The network smoke check uses unprivileged user/network namespaces and virtual
Ethernet peers: it exercises real proxy DHCP for architectures 7/9, iPXE boot
selection, TFTP and temporary iptables cleanup. It changes no host interfaces or
firewall rules, uses no sudo, and requires Linux unprivileged user namespaces,
`unshare`, `nsenter`, `ip`, `iptables`, and an explicit dnsmasq binary. It does not
boot firmware or run an Omarchy installation.
For Omarchy 4.0.4, extract its initramfs with `lsinitcpio --extract` in a temporary
directory, then run `python3 tests/initramfs_network_smoke.py /absolute/path/to/usr/bin/ipconfig`.
This optional regression check runs the ISO's real DHCP client against dnsmasq
on private virtual interfaces: it reproduces the discovery error and verifies
that an explicit boot interface obtains a lease. It uses private mount
namespaces for sysfs and DHCP output files and never starts a host service.
See [hardware acceptance](docs/ACCEPTANCE.md) for the deployment checks still
needed before calling this production-ready.

Boot parameters follow the upstream
[Arch HTTP hook](https://gitlab.archlinux.org/archlinux/mkinitcpio/mkinitcpio-archiso/-/blob/master/hooks/archiso_pxe_http).
The network-boot hooks are included in the
[Omarchy ISO configuration](https://github.com/omacom/omarchy-iso/blob/quattro/configs/airootfs/etc/mkinitcpio.conf.d/archiso.conf).

Proxy behavior follows the upstream [dnsmasq manual](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html).
