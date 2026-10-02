"""Exercise an ISO's real ipconfig in private network/mount namespaces.

Usage: python3 tests/initramfs_network_smoke.py /absolute/path/to/ipconfig
The executable must be extracted from the reviewed ISO. No host network changes,
sudo, or boot services are used. The test router allocates only on private veths.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

SCRIPT = str(Path(__file__).resolve())


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, timeout=15, **kwargs)


def client(binary, mode):
    # Mount fresh sysfs so discovery sees this network namespace's interfaces.
    run('/usr/bin/mount', '-t', 'sysfs', '-o', 'nosuid,nodev,noexec', 'sysfs', '/sys')
    # Keep the executable open across /tmp isolation; ipconfig writes net-*.conf.
    fd = os.open(binary, os.O_RDONLY)
    try:
        run('/usr/bin/mount', '-t', 'tmpfs', '-o', 'nosuid,nodev', 'tmpfs', '/tmp')
        run('/usr/bin/ip', 'link', 'set', 'client0', 'up')
        arg = 'ip=dhcp' if mode == 'discovery' else 'ip=:::::client0:dhcp'
        result = subprocess.run([f'/proc/self/fd/{fd}', '-t', '3', arg],
                                pass_fds=(fd,), text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10)
        print(result.stdout, end='')
        if mode == 'discovery':
            assert result.returncode != 0 and 'SIOCGIFFLAGS' in result.stdout, \
                'This ipconfig does not reproduce the affected discovery path'
        else:
            assert result.returncode == 0 and 'SIOCGIFFLAGS' not in result.stdout
            config = Path('/tmp/net-client0.conf').read_text()
            values = dict(entry.split('=', 1) for entry in shlex.split(config))
            assert values['DEVICE'] == 'client0' and values['IPV4ADDR'].startswith('192.0.2.')
    finally:
        os.close(fd)


def inside(binary, original_netns):
    assert os.readlink('/proc/self/ns/net') != original_netns, 'Refusing host network'
    assert os.geteuid() == 0
    run('/usr/bin/ip', 'link', 'add', 'server0', 'type', 'veth', 'peer', 'name', 'client0')
    run('/usr/bin/ip', 'address', 'add', '192.0.2.1/24', 'dev', 'server0')
    run('/usr/bin/ip', 'link', 'set', 'server0', 'up')
    run('/usr/bin/ip', 'link', 'set', 'lo', 'up')
    with tempfile.TemporaryDirectory(prefix='pxe-dust-initrd-network-') as temporary:
        path = Path(temporary)
        config = path / 'dnsmasq.conf'
        config.write_text('port=0\ninterface=server0\nexcept-interface=lo\nbind-interfaces\n'
                          'dhcp-range=192.0.2.20,192.0.2.30,255.255.255.0,1h\n'
                          'dhcp-broadcast\n'
                          f'dhcp-leasefile={path}/leases\npid-file={path}/pid\n'
                          'dhcp-option=3,192.0.2.1\ndhcp-option=6,192.0.2.1\nlog-dhcp\n')
        with (path / 'log').open('w+b') as log:
            server = subprocess.Popen(['/usr/bin/dnsmasq', '--no-daemon',
                                       '--conf-file=' + str(config)], stdout=log, stderr=log)
            holder = None
            try:
                time.sleep(0.3)
                assert server.poll() is None, 'Test DHCP server failed to start'
                holder = subprocess.Popen(['/usr/bin/unshare', '--net',
                                           '/usr/bin/sleep', '30'])
                deadline = time.monotonic() + 3
                while os.readlink(f'/proc/{holder.pid}/ns/net') == os.readlink('/proc/self/ns/net'):
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                run('/usr/bin/ip', 'link', 'set', 'client0', 'netns', str(holder.pid))
                for mode in ('discovery', 'explicit'):
                    run('/usr/bin/nsenter', '-t', str(holder.pid), '-n',
                        '/usr/bin/unshare', '--mount', sys.executable, SCRIPT,
                        '--client', binary, mode)
                print('Real ISO ipconfig: discovery failure reproduced; explicit boot NIC obtains a DHCP lease.')
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read(12000).decode(errors='replace'), file=sys.stderr)
                raise
            finally:
                for process in (holder, server):
                    if process is not None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--client':
        client(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == '--inside':
        inside(sys.argv[2], sys.argv[3])
    else:
        if len(sys.argv) != 2:
            raise SystemExit(__doc__)
        binary = str(Path(sys.argv[1]).resolve(strict=True))
        result = subprocess.run(['/usr/bin/unshare', '--user', '--map-root-user', '--net', '--mount',
                                 sys.executable, SCRIPT, '--inside', binary,
                                 os.readlink('/proc/self/ns/net')], timeout=30)
        raise SystemExit(result.returncode)
