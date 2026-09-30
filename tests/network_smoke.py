"""Exercise real proxy DHCP and firewall cleanup in an unprivileged net namespace.

Usage: python3 tests/network_smoke.py /usr/bin/dnsmasq
No packets or firewall changes reach the host network. No sudo is used.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('dust', str(ROOT / 'bin/pxe-dust'))
spec = importlib.util.spec_from_loader(loader.name, loader)
dust = importlib.util.module_from_spec(spec)
loader.exec_module(dust)


def options(raw):
    result = {}
    cursor = 240
    while cursor < len(raw):
        key = raw[cursor]; cursor += 1
        if key == 255:
            break
        if key == 0:
            continue
        size = raw[cursor]; cursor += 1
        result[key] = raw[cursor:cursor + size]; cursor += size
    return result


def discover(sock, mac, arch=None, ipxe=False, request=False):
    xid = os.urandom(4)
    packet = bytearray(240)
    packet[0:4] = bytes((1, 1, 6, 0))
    packet[4:8] = xid
    packet[10:12] = b'\x00\x00' if request else b'\x80\x00'
    if request:
        packet[12:16] = socket.inet_aton('192.0.2.20')
    packet[28:34] = mac
    packet[236:240] = b'\x63\x82\x53\x63'
    packet += bytes((53, 1, 3 if request else 1))
    if arch is not None:
        vendor = f'PXEClient:Arch:{arch:05}:UNDI:003016'.encode()
        packet += bytes((60, len(vendor))) + vendor
        packet += b'\x5d\x02' + struct.pack('!H', arch)
    if ipxe:
        packet += b'\x4d\x05\x04iPXE'
        packet += b'\xaf\x01\x01'
    packet += b'\x37\x04\x3c\x2b\x42\x43\xff'
    packet += bytes(max(0, 300 - len(packet)))
    sock.sendto(packet, ('192.0.2.10', 4011) if request else ('255.255.255.255', 67))
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        try:
            raw, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        if len(raw) >= 240 and raw[4:8] == xid and raw[0] == 2:
            return raw
    return None


def inside(binary, host_netns):
    assert os.readlink('/proc/self/ns/net') != host_netns, 'Refusing host network'
    assert os.geteuid() == 0
    dust.ip('link', 'add', 'server0', 'type', 'veth', 'peer', 'name', 'client0')
    for name, address in (('server0', '192.0.2.10'), ('client0', '192.0.2.20')):
        dust.ip('address', 'add', address + '/24', 'dev', name)
        dust.ip('link', 'set', name, 'up')
    dust.ip('link', 'set', 'lo', 'up')
    session = {'mode': 'lan', 'interface': 'server0', 'server': '192.0.2.10',
               'network': '192.0.2.0/24', 'firewall_chain': 'PXDUST_001122334455'}
    # Preserve a pre-existing administrator rule, including across partial setup.
    dust.bounded(['/usr/bin/iptables', '-P', 'INPUT', 'DROP'])
    dust.bounded(['/usr/bin/iptables', '-A', 'INPUT', '-i', 'lo', '-j', 'ACCEPT'])
    baseline = dust.bounded(['/usr/bin/iptables', '-S'])
    dust.firewall_install(session)
    assert b'PXDUST_' in dust.bounded(['/usr/bin/iptables', '-S'])
    dust.firewall_cleanup(session)
    dust.firewall_cleanup(session)
    assert baseline == dust.bounded(['/usr/bin/iptables', '-S'])
    dust.bounded(['/usr/bin/iptables', '-N', session['firewall_chain']])
    dust.firewall_cleanup(session)
    assert baseline == dust.bounded(['/usr/bin/iptables', '-S'])
    print('Real iptables round-trip, retry and partial-start cleanup passed.')

    dust.firewall_install(session)
    with tempfile.TemporaryDirectory(prefix='pxe-dust-network-') as temporary:
        directory = Path(temporary)
        (directory / 'ipxe.efi').write_bytes(b'test bootloader')
        config = dust.dnsmasq_config(session).decode()
        config = config.replace('/var/lib/pxe-dust/image/tftp', temporary)
        config = config.replace('/run/pxe-dust/dnsmasq.pid', str(directory / 'dnsmasq.pid'))
        config_path = directory / 'dnsmasq.conf'
        config_path.write_text(config)
        with (directory / 'log').open('w+b') as log:
            proc = subprocess.Popen([binary, '--no-daemon',
                                     '--conf-file=' + str(config_path)], stdout=log, stderr=log)
            try:
                time.sleep(0.4)
                assert proc.poll() is None, 'dnsmasq failed to start'
                client_ns = subprocess.Popen(['/usr/bin/unshare', '--net', '/usr/bin/sleep', '30'])
                try:
                    deadline = time.monotonic() + 3
                    while os.readlink(f'/proc/{client_ns.pid}/ns/net') == os.readlink('/proc/self/ns/net'):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    dust.ip('link', 'set', 'client0', 'netns', str(client_ns.pid))
                    subprocess.run(['/usr/bin/nsenter', '-t', str(client_ns.pid), '-n',
                                    sys.executable, str(Path(__file__).resolve()), '--client'], check=True, timeout=15)
                finally:
                    client_ns.terminate(); client_ns.wait(timeout=3)
                print('Real proxy DHCP: normal clients ignored; EFI 7/9 and iPXE bootfiles correct; no address/router/DNS leases.')
            except BaseException:
                log.flush(); log.seek(0)
                print(log.read(12000).decode(errors='replace'), file=sys.stderr)
                raise
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                dust.firewall_cleanup(session)
                assert baseline == dust.bounded(['/usr/bin/iptables', '-S'])



def client():
    dust.ip('address', 'add', '192.0.2.20/24', 'dev', 'client0')
    dust.ip('link', 'set', 'client0', 'up')
    row = json.loads(dust.ip('-j', 'link', 'show', 'dev', 'client0'))[0]
    mac = bytes.fromhex(row['address'].replace(':', ''))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'client0\0')
        sock.bind(('0.0.0.0', 68))
        sock.settimeout(0.2)
        assert discover(sock, mac) is None, 'Proxy must not answer normal DHCP clients'
        for arch in (7, 9):
            for ipxe in (False, True):
                raw = discover(sock, mac, arch, ipxe)
                assert raw is not None, f'No proxy offer for arch={arch}, ipxe={ipxe}'
                assert raw[16:20] == bytes(4), 'Proxy must never allocate an address'
                opts = options(raw)
                assert opts[53] == b'\x02', opts
                assert not any(key in opts for key in (3, 6, 51)), opts
                raw = discover(sock, mac, arch, ipxe, request=True)
                assert raw is not None, f'No boot-server ACK for arch={arch}, ipxe={ipxe}'
                assert options(raw)[53] == b'\x05'
                bootfile = raw[108:236].split(b'\0')[0]
                expected = b'http://192.0.2.10:8080/boot.ipxe' if ipxe else b'ipxe.efi'
                assert bootfile == expected, (bootfile, opts)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tftp:
        tftp.settimeout(2)
        tftp.sendto(b'\x00\x01ipxe.efi\x00octet\x00', ('192.0.2.10', 69))
        data, peer = tftp.recvfrom(1024)
        assert 40000 <= peer[1] <= 40031, peer
        assert data == b'\x00\x03\x00\x01test bootloader', data
        tftp.sendto(b'\x00\x04\x00\x01', peer)
    print('TFTP initial request and transfer succeed through the scoped rules with INPUT DROP.')


if __name__ == '__main__':
    if sys.argv[1:] == ['--client']:
        client()
    elif len(sys.argv) == 4 and sys.argv[1] == '--inside':
        inside(sys.argv[2], sys.argv[3])
    else:
        binary = str(Path(sys.argv[1] if len(sys.argv) > 1 else '/usr/bin/dnsmasq').resolve())
        result = subprocess.run(['/usr/bin/unshare', '--user', '--map-root-user', '--net',
                                 sys.executable, str(Path(__file__).resolve()), '--inside', binary,
                                 os.readlink('/proc/self/ns/net')], timeout=40)
        raise SystemExit(result.returncode)
