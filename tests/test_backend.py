import contextlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import tarfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("pxe_dust", str(ROOT / "bin/pxe-dust"))
spec = importlib.util.spec_from_loader(loader.name, loader)
dust = importlib.util.module_from_spec(spec)
loader.exec_module(dust)


class ConfigurationTests(unittest.TestCase):
    def test_reject_interface_injection(self):
        for value in ("lo", "-all", "eth0\nport=53", "eth0;id", "../eth0", "x" * 16):
            with self.subTest(value=value), self.assertRaises(dust.Error):
                dust.interface_name(value)
        self.assertEqual(dust.interface_name("enp0s20u1"), "enp0s20u1")

    def test_match_kernel_and_initramfs(self):
        names = ["arch/boot/x86_64/vmlinuz-linux-t2", "arch/boot/x86_64/initramfs-linux-t2.img",
                 "arch/x86_64/airootfs.sfs", "arch/x86_64/airootfs.sfs.cms.sig", "../../etc/shadow"]
        assets = dust.choose_assets(names)
        self.assertEqual(assets["vmlinuz"], names[0])
        self.assertNotIn("../../etc/shadow", assets.values())
        self.assertIn("airootfs.sfs.cms.sig", assets)

    def test_reject_missing_and_duplicate_assets(self):
        for names in ([], ["x", "x"], ["arch/boot/x86_64/vmlinuz-linux"]):
            with self.assertRaises(dust.Error):
                dust.choose_assets(names)

    def test_ipxe_chain_does_not_loop(self):
        config = dust.dnsmasq_config().decode()
        self.assertIn("dhcp-boot=tag:!ipxe,tag:efi64,ipxe.efi", config)
        self.assertIn("dhcp-boot=tag:ipxe,http://", config)
        self.assertNotIn("dhcp-ignore", config)  # initramfs also needs a DHCP lease
        self.assertIn("dhcp-option=3\n", config)
        self.assertIn("port=0\n", config)

    def test_boot_loads_complete_root_and_signature(self):
        script = dust.boot_script(True).decode()
        self.assertIn("archiso_http_srv=${base}/", script)
        self.assertIn("initrd=initramfs.img", script)
        self.assertIn("BOOTIF=01-${netX/mac:hexhyp}", script)
        self.assertIn("cms_verify=y", script)
        self.assertNotIn("cms_verify=y", dust.boot_script(False).decode())

    def test_explicit_dedicated_acknowledgement(self):
        with self.assertRaisesRegex(dust.Error, "exactly one"):
            dust.start("enp1s0", False)


class ProcessTests(unittest.TestCase):
    def test_stdout_and_stderr_caps(self):
        for fd in (1, 2):
            with self.subTest(fd=fd), self.assertRaisesRegex(dust.Error, "output limit"):
                dust.bounded([sys.executable, "-c", f"import os; os.write({fd}, b'x'*100000)"], limit=100)

    def test_absolute_deadline(self):
        start = time.monotonic()
        with self.assertRaisesRegex(dust.Error, "time limit"):
            dust.bounded([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
        self.assertLess(time.monotonic() - start, 2)

    def test_streaming_output(self):
        target = io.BytesIO()
        dust.bounded([sys.executable, "-c", "print('asset')"], output=target)
        self.assertEqual(target.getvalue(), b"asset\n")

    def test_clean_environment(self):
        with patch.dict(os.environ, {"PYTHONPATH": "/malicious", "LD_PRELOAD": "/malicious"}):
            raw = dust.bounded([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"])
        self.assertNotIn("PYTHONPATH", json.loads(raw))
        self.assertNotIn("LD_PRELOAD", json.loads(raw))


class FileTests(unittest.TestCase):
    def test_prepare_real_archive_and_replace_cache(self):
        # Exercise libarchive's descriptor path and selected-member extraction.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "fixture.iso"
            with tarfile.open(archive, "w") as tar:
                for name, payload in {
                    "arch/boot/x86_64/vmlinuz-linux": b"kernel",
                    "arch/boot/x86_64/initramfs-linux.img": b"initramfs",
                    "arch/x86_64/airootfs.sfs": b"rootfs with bundled mirror",
                    "arch/x86_64/airootfs.sfs.cms.sig": b"signature",
                    "unrelated.txt": b"must not be extracted",
                }.items():
                    item = tarfile.TarInfo(name)
                    item.size = len(payload)
                    tar.addfile(item, io.BytesIO(payload))
            efi = directory / "ipxe.efi"
            efi.write_bytes(b"fixture-efi")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            # This test is unprivileged. Keep bsdtar at the test user's identity.
            account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            real_bounded = dust.bounded
            def unprivileged(*args, **kwargs):
                self.assertIsInstance(kwargs["stdin"], int)
                self.assertNotIn("/proc/self/fd/", " ".join(args[0]))
                kwargs.pop("extra_groups", None)
                return real_bounded(*args, **kwargs)
            with patch.object(dust, "DATA", directory), patch.object(dust, "IPXE", efi), \
                 patch.object(dust, "service_state", return_value="inactive"), \
                 patch.object(dust.pwd, "getpwnam", return_value=account), \
                 patch.object(dust, "bounded", side_effect=unprivileged):
                for _ in range(2):
                    self.assertTrue(dust.prepare(archive, digest)["ok"])
            self.assertEqual((directory / "image/vmlinuz").read_bytes(), b"kernel")
            self.assertEqual((directory / "image/airootfs.sfs").read_bytes(), b"rootfs with bundled mirror")
            self.assertFalse((directory / "image/unrelated.txt").exists())
            self.assertFalse((directory / "previous-image").exists())

    def test_reject_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fifo"
            os.mkfifo(path)
            with self.assertRaises(dust.Error):
                dust.caller_open(path)

    def test_reject_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target"
            target.write_bytes(b"image")
            link = Path(temporary) / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                dust.caller_open(link)

    def test_hash_failure_preserves_old_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.iso"
            source.write_bytes(b"bad download")
            image = directory / "image"
            image.mkdir()
            (image / "sentinel").write_bytes(b"keep me")
            with patch.object(dust, "DATA", directory), patch.object(dust, "service_state", return_value="inactive"):
                with self.assertRaisesRegex(dust.Error, "SHA-256 mismatch"):
                    dust.prepare(source, "0" * 64)
            self.assertEqual((image / "sentinel").read_bytes(), b"keep me")
            self.assertEqual(sorted(x.name for x in directory.iterdir()), ["image", "source.iso"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        (directory / "vmlinuz").write_bytes(b"test-kernel")
        (directory / "metadata.json").write_bytes(b"private")
        (directory / "initramfs.img").symlink_to(directory / "metadata.json")
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        # Threads keep fixture state visible; production uses bounded forking.
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dust.AssetHandler)
        self.server.asset_fd = self.fd
        self.server.handle_error = lambda *_: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        os.close(self.fd)
        self.temporary.cleanup()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, response.read(), response.getheader("Content-Length"))
        connection.close()
        return result

    def test_get_and_head(self):
        self.assertEqual(self.request("/vmlinuz"), (200, b"test-kernel", "11"))
        self.assertEqual(self.request("/vmlinuz", "HEAD"), (200, b"", "11"))

    def test_deny_nonassets_and_traversal(self):
        for path in ("/", "/metadata.json", "/../metadata.json", "/%2e%2e/etc/passwd", "/vmlinuz?x=1"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_post_is_not_a_control_api(self):
        self.assertEqual(self.request("/boot.ipxe", "POST")[0], 501)

    def test_archiso_probe_transfers_only_one_byte(self):
        self.assertEqual(self.request("/vmlinuz", headers={"Range": "bytes=0-0"}), (206, b"t", "1"))
        self.assertEqual(self.request("/vmlinuz", headers={"Range": "bytes=-6"}), (206, b"kernel", "6"))
        for header in ("bytes=99-", "bytes=2-1", "bytes=0-0,2-2", "bytes=-0"):
            with self.subTest(header=header):
                self.assertEqual(self.request("/vmlinuz", headers={"Range": header})[0], 416)

    def test_http_serves_session_specific_boot_script(self):
        self.server.boot_payload = dust.boot_script(True, '192.0.2.10')
        status, payload, _ = self.request('/boot.ipxe')
        self.assertEqual(status, 200)
        self.assertIn(b'http://192.0.2.10:8080', payload)
        self.assertIn(b'cms_verify=y', payload)
        self.assertNotIn(dust.SERVER.encode(), payload)

    def test_symlink_asset_is_not_served(self):
        self.assertEqual(self.request("/initramfs.img")[0], 404)


class LifecycleTests(unittest.TestCase):
    def test_cleanup_restores_both_management_states(self):
        for managed in (False, True):
            session = {"interface": "enp1s0", "managed": managed, "address": "02:00:00:00:00:01"}
            with self.subTest(managed=managed), \
                 patch.object(dust, "read_json", return_value=session), \
                 patch.object(Path, "exists", return_value=False), \
                 patch.object(Path, "unlink") as unlink, \
                 patch.object(dust, "ip", return_value=json.dumps([{"address": session["address"]}]).encode()), \
                 patch.object(dust, "bounded") as command:
                dust.cleanup()
                command.assert_called_once_with(["/usr/bin/nmcli", "device", "set", "enp1s0", "managed", "yes" if managed else "no"])
                unlink.assert_called_once()

    def test_changed_adapter_preserves_recovery_journal(self):
        session = {"interface": "enp1s0", "managed": True, "address": "02:00:00:00:00:01"}
        with patch.object(dust, "read_json", return_value=session), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(Path, "unlink") as unlink, \
             patch.object(dust, "ip", return_value=b'[{"address":"02:00:00:00:00:02"}]'), \
             patch.object(dust, "bounded") as command:
            with self.assertRaisesRegex(dust.Error, "missing or changed"):
                dust.cleanup()
            command.assert_not_called()
            unlink.assert_not_called()

    def test_no_cleanup_without_owned_session(self):
        with patch.object(dust, "read_json", return_value=None), patch.object(dust, "ip") as ip:
            dust.cleanup()
            ip.assert_not_called()

    def test_active_interface_refused_before_mutation(self):
        row = {"link_type": "ether", "flags": ["UP"], "addr_info": []}
        with patch.object(dust, "REQUIRED", ()), patch.object(dust, "IPXE") as ipxe, patch.object(dust, "ip", return_value=json.dumps([row]).encode()):
            ipxe.is_file.return_value = True
            with self.assertRaisesRegex(dust.Error, "Disconnect"):
                dust.preflight("enp1s0")

    def test_service_failure_always_reaps_children_and_cleans(self):
        session = {"interface": "enp1s0", "managed": False, "address": "02:00:00:00:00:01"}
        reads = [{"interface": "enp1s0"}, None]
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(dust, "RUN", Path(temporary)), \
             patch.object(dust.signal, "signal"), \
             patch.object(dust, "read_json", side_effect=reads), \
             patch.object(dust, "preflight", return_value=session), \
             patch.object(dust, "write_json"), patch.object(dust, "bounded"), \
             patch.object(dust, "ip", side_effect=dust.Error("simulated link move failure")), \
             patch.object(dust, "cleanup") as cleanup:
            with self.assertRaisesRegex(dust.Error, "simulated"):
                dust.serve()
            cleanup.assert_called_once()



class DedicatedTransitionTests(unittest.TestCase):
    """Model external connection changes at the dedicated ownership boundary."""

    @contextlib.contextmanager
    def network(self, managed=True):
        self.session = {"mode": "dedicated", "interface": "enp1s0", "ifindex": 3,
                        "address": "02:00:00:00:00:01", "managed": managed,
                        "transition_version": 1, "nm_restore": False, "move_intent": False}
        self.host = {"ifname": "enp1s0", "ifindex": 3, "address": self.session["address"],
                     "link_type": "ether", "flags": [], "addr_info": []}
        self.moved = None
        self.ns = None
        self.managed = managed
        self.routes = {"-4": [], "-6": []}
        self.journal = dict(self.session)
        self.commands = []
        self.after_nm = lambda: None
        self.after_ip = lambda args: None

        def command(argv, **_):
            self.commands.append(tuple(argv))
            if "GENERAL.NM-MANAGED" in argv:
                return b"yes" if self.managed else b"no"
            self.managed = argv[-1] == "yes"
            self.after_nm()
            return b""

        def ip(*args):
            self.commands.append(args)
            prefix = args[:2] == ("-n", dust.NETNS)
            parts = args[2:] if prefix else args
            row = self.moved if prefix else self.host
            if "-j" in parts:
                if "route" in parts:
                    return json.dumps(self.routes[parts[1]]).encode()
                if prefix and parts == ("-j", "link", "show"):
                    rows = [{"ifname": "lo"}] + ([row] if row else [])
                else:
                    rows = [row] if row else []
                return json.dumps(rows).encode()
            if parts == ("netns", "add", dust.NETNS):
                self.ns = [1, 42]
            elif parts == ("netns", "delete", dust.NETNS):
                self.ns = None
            elif parts[:2] == ("link", "set"):
                if "netns" in parts:
                    if prefix:
                        self.host, self.moved = self.moved, None
                    else:
                        self.moved, self.host = self.host, None
                elif "name" in parts:
                    row["ifname"] = parts[-1]
                elif parts[3] != "lo":
                    row["flags"] = ["UP"] if parts[-1] == "up" else []
            elif parts[:2] == ("address", "add"):
                row["addr_info"] = [{"family": "inet", "local": dust.SERVER, "prefixlen": 24}]
            elif parts[:2] == ("address", "flush"):
                row["addr_info"] = []
            self.after_ip(args)
            return b""

        def write(_path, value):
            self.journal = json.loads(json.dumps(value))

        def exists(path):
            return path.name == "device" or (path == Path("/run/netns", dust.NETNS) and self.ns is not None)

        with patch.object(dust, "ip", side_effect=ip), \
             patch.object(dust, "bounded", side_effect=command), \
             patch.object(dust, "write_json", side_effect=write), \
             patch.object(dust, "read_json", side_effect=lambda _: self.journal), \
             patch.object(dust, "namespace_identity", side_effect=lambda: self.ns), \
             patch.object(Path, "exists", autospec=True, side_effect=exists), \
             patch.object(Path, "unlink") as unlink:
            self.unlink = unlink
            yield

    def assert_no_adapter_mutation(self):
        self.assertFalse(any("set" in x or "flush" in x for x in self.commands))

    def test_connection_activated_after_preflight_is_not_repurposed(self):
        with self.network():
            self.host["flags"] = ["UP"]
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.dedicated_takeover(self.session)
            dust.cleanup()
            self.assert_no_adapter_mutation()
            self.unlink.assert_called_once()

    def test_replaced_same_mac_adapter_is_refused_before_nm_mutation(self):
        with self.network():
            self.host["ifindex"] = 99
            with self.assertRaisesRegex(dust.Error, "identity changed"):
                dust.dedicated_takeover(self.session)
            self.assert_no_adapter_mutation()

    def test_management_change_is_refused_before_nm_mutation(self):
        with self.network():
            self.managed = False
            with self.assertRaisesRegex(dust.Error, "management state changed"):
                dust.dedicated_takeover(self.session)
            self.assert_no_adapter_mutation()

    def test_activation_during_nm_intent_journal_does_not_restore_or_move_connection(self):
        with self.network():
            record = dust.write_json.side_effect
            def activate(path, value):
                record(path, value)
                if value.get("nm_restore"):
                    self.host["flags"] = ["UP"]
            dust.write_json.side_effect = activate
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.dedicated_takeover(self.session)
            dust.cleanup()
            self.assert_no_adapter_mutation()
            self.assertTrue(self.managed)
            self.unlink.assert_called_once()

    def test_activation_during_move_intent_journal_prevents_actual_move(self):
        with self.network():
            record = dust.write_json.side_effect
            def activate(path, value):
                record(path, value)
                if value.get("move_intent"):
                    self.host["flags"] = ["UP"]
            dust.write_json.side_effect = activate
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.dedicated_takeover(self.session)
            self.assertIsNotNone(self.host)
            self.assertIsNone(self.moved)
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.cleanup()
            self.assertFalse(any("flush" in x for x in self.commands))

    def test_nm_timeout_after_applying_unmanaged_still_restores_idle_original(self):
        with self.network():
            def timeout():
                raise dust.Error("simulated nmcli timeout")
            self.after_nm = timeout
            with self.assertRaisesRegex(dust.Error, "timeout"):
                dust.dedicated_takeover(self.session)
            self.assertFalse(self.managed)
            self.after_nm = lambda: None
            dust.cleanup()
            self.assertTrue(self.managed)
            self.unlink.assert_called_once()

    def test_adapter_activation_after_unmanaged_preserves_connection_and_recovery(self):
        with self.network():
            self.after_nm = lambda: self.host.update(flags=["UP"], addr_info=[{"local": "192.0.2.5"}])
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.dedicated_takeover(self.session)
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.cleanup()
            self.assertFalse(any("netns" in x or "flush" in x for x in self.commands))
            self.assertFalse(self.managed)
            self.unlink.assert_not_called()
            self.host.update(flags=[], addr_info=[])
            self.after_nm = lambda: None
            dust.cleanup()
            self.assertTrue(self.managed)
            self.unlink.assert_called_once()

    def test_routes_added_after_unmanaged_prevent_move(self):
        for family in ("-4", "-6"):
            with self.subTest(family=family), self.network():
                self.after_nm = lambda: self.routes[family].append({"dst": "default"})
                with self.assertRaisesRegex(dust.Error, "has routes"):
                    dust.dedicated_takeover(self.session)
                self.assertIsNone(self.ns)
                self.assertIsNotNone(self.host)

    def test_activation_during_namespace_creation_prevents_move(self):
        with self.network():
            def change(args):
                if args == ("netns", "add", dust.NETNS):
                    self.host["flags"] = ["UP"]
            self.after_ip = change
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.dedicated_takeover(self.session)
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.cleanup()
            self.assertIsNotNone(self.host)
            self.assertIsNone(self.ns)
            self.assertFalse(any("flush" in x for x in self.commands))
            self.unlink.assert_not_called()

    def test_activation_during_move_is_not_flushed_or_reconfigured(self):
        with self.network():
            def change(args):
                if args == ("link", "set", "dev", "enp1s0", "netns", dust.NETNS):
                    self.moved.update(flags=["UP"], addr_info=[{"local": "192.0.2.5"}])
            self.after_ip = change
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.dedicated_takeover(self.session)
            self.commands.clear()
            with self.assertRaisesRegex(dust.Error, "became active"):
                dust.cleanup()
            self.assert_no_adapter_mutation()
            self.assertEqual(self.moved["addr_info"], [{"local": "192.0.2.5"}])
            self.assertIsNotNone(self.ns)
            self.unlink.assert_not_called()

    def test_successful_takeover_returns_adapter_and_original_management(self):
        for managed in (True, False):
            with self.subTest(managed=managed), self.network(managed):
                dust.dedicated_takeover(self.session)
                self.assertIsNone(self.host)
                self.assertEqual(self.moved["ifname"], "pxe0")
                self.assertFalse(self.managed)
                dust.cleanup()
                self.assertIsNone(self.ns)
                self.assertEqual(self.host["ifname"], "enp1s0")
                self.assertEqual(self.host["flags"], [])
                self.assertEqual(self.host["addr_info"], [])
                self.assertEqual(self.managed, managed)
                self.unlink.assert_called_once()

    def test_unrelated_namespace_after_preflight_is_never_touched(self):
        with self.network():
            self.ns = [1, 99]
            dust.cleanup()
            self.assertEqual(self.ns, [1, 99])
            self.assertEqual(self.commands, [])

    def test_replaced_owned_namespace_preserves_recovery_and_all_links(self):
        with self.network():
            dust.dedicated_takeover(self.session)
            self.ns = [1, 99]
            self.commands.clear()
            with self.assertRaisesRegex(dust.Error, "namespace identity changed"):
                dust.cleanup()
            self.assertEqual(self.commands, [])
            self.unlink.assert_not_called()

    def test_crash_before_namespace_ownership_write_never_deletes_unproven_namespace(self):
        with self.network():
            self.journal["namespace_intent"] = True
            self.ns = [1, 99]
            with self.assertRaisesRegex(dust.Error, "ownership was not recorded"):
                dust.cleanup()
            self.assertEqual(self.commands, [])
            self.unlink.assert_not_called()

    def test_recovery_refuses_replacement_adapter_after_nm_change(self):
        with self.network():
            self.journal["nm_restore"] = True
            self.managed = False
            self.host["ifindex"] = 99
            with self.assertRaisesRegex(dust.Error, "missing or changed"):
                dust.cleanup()
            self.assert_no_adapter_mutation()
            self.unlink.assert_not_called()

    def test_unrelated_address_in_owned_namespace_is_preserved(self):
        with self.network():
            dust.dedicated_takeover(self.session)
            self.moved["addr_info"].append({"family": "inet", "local": "192.0.2.5", "prefixlen": 24})
            self.commands.clear()
            with self.assertRaisesRegex(dust.Error, "unrelated addresses"):
                dust.cleanup()
            self.assert_no_adapter_mutation()
            self.unlink.assert_not_called()


class LANTests(unittest.TestCase):
    def row(self, address='192.0.2.10'):
        return {'ifname': 'enp1s0', 'ifindex': 3, 'address': '02:00:00:00:00:01',
                'link_type': 'ether', 'flags': ['UP'],
                'addr_info': [{'family': 'inet', 'scope': 'global', 'local': address, 'prefixlen': 24}]}

    def session(self):
        return {'mode': 'lan', 'interface': 'enp1s0', 'ifindex': 3,
                'address': '02:00:00:00:00:01', 'server': '192.0.2.10',
                'network': '192.0.2.0/24', 'firewall_chain': 'PXDUST_001122334455'}

    def test_proxy_configuration_cannot_allocate_addresses(self):
        config = dust.dnsmasq_config(self.session()).decode()
        ranges = [x for x in config.splitlines() if x.startswith('dhcp-range=')]
        self.assertEqual(ranges, ['dhcp-range=192.0.2.0,proxy,255.255.255.0'])
        self.assertNotIn('dhcp-option=3', config)
        self.assertNotIn('dhcp-option=6', config)
        self.assertNotIn('dhcp-authoritative', config)
        self.assertIn('interface=enp1s0\n', config)
        self.assertIn('pxe-service=tag:!ipxe,7,', config)
        self.assertIn('pxe-service=tag:ipxe,9,', config)
        self.assertIn('http://192.0.2.10:8080/boot.ipxe', config)

    def test_existing_connection_is_accepted_without_nm_changes(self):
        with patch.object(dust, 'REQUIRED', ()), patch.object(dust, 'IPXE') as efi, \
             patch.object(dust.os, 'access', return_value=True), \
             patch.object(Path, 'exists', autospec=True, side_effect=lambda p: p.name == 'device'), \
             patch.object(dust, 'ip', return_value=json.dumps([self.row()]).encode()) as ip, \
             patch.object(dust, 'bounded') as command:
            efi.is_file.return_value = True
            result = dust.preflight('enp1s0', 'lan')
            self.assertEqual(result['server'], '192.0.2.10')
            self.assertEqual(result['network'], '192.0.2.0/24')
            command.assert_not_called()
            ip.assert_called_once_with('-j', 'address', 'show', 'dev', 'enp1s0')

    def test_wifi_host_is_accepted_only_in_existing_lan_mode(self):
        row = self.row()
        row['ifname'] = 'wlo1'
        with patch.object(dust, 'REQUIRED', ()), patch.object(dust, 'IPXE') as efi, \
             patch.object(dust.os, 'access', return_value=True), \
             patch.object(Path, 'exists', autospec=True,
                          side_effect=lambda p: p.name in ('device', 'wireless')), \
             patch.object(dust, 'ip', return_value=json.dumps([row]).encode()), \
             patch.object(dust, 'bounded') as command:
            efi.is_file.return_value = True
            session = dust.preflight('wlo1', 'lan')
            self.assertEqual(session['interface'], 'wlo1')
            self.assertEqual(session['server'], '192.0.2.10')
            config = dust.dnsmasq_config(session).decode()
            self.assertIn('interface=wlo1\n', config)
            self.assertIn('dhcp-range=192.0.2.0,proxy,255.255.255.0', config)
            with self.assertRaisesRegex(dust.Error, 'Dedicated network mode requires'):
                dust.preflight('wlo1', 'dedicated')
            command.assert_not_called()

    def test_discovery_includes_physical_wifi_and_excludes_virtual_adapters(self):
        wired, wifi, bridge = self.row(), self.row(), self.row()
        wifi['ifname'] = 'wlo1'
        bridge['ifname'] = 'docker0'
        def exists(path):
            return ((path.name == 'device' and path.parent.name in ('enp1s0', 'wlo1'))
                    or (path.name == 'wireless' and path.parent.name == 'wlo1'))
        with patch.object(Path, 'exists', autospec=True, side_effect=exists), \
             patch.object(dust, 'ip', return_value=json.dumps([wired, wifi, bridge]).encode()):
            found = dust.interfaces()
        self.assertEqual([(x['name'], x['wireless']) for x in found],
                         [('enp1s0', False), ('wlo1', True)])

    def test_lan_refuses_virtual_interfaces_and_bridge_members(self):
        for extra in ({'master': 'br0'}, {'linkinfo': {'info_kind': 'veth'}}):
            with self.subTest(extra=extra), patch.object(dust, 'REQUIRED', ()), \
                 patch.object(dust, 'IPXE') as efi, \
                 patch.object(dust, 'ip', return_value=json.dumps([self.row() | extra]).encode()):
                efi.is_file.return_value = True
                with self.assertRaisesRegex(dust.Error, 'physical network interface'):
                    dust.preflight('enp1s0', 'lan')

    def test_reject_ambiguous_or_missing_ipv4(self):
        row = self.row()
        row['addr_info'] *= 2
        with self.assertRaisesRegex(dust.Error, 'exactly one'):
            dust.lan_address(row)
        row['addr_info'] = []
        with self.assertRaises(dust.Error):
            dust.lan_address(row)
        row = self.row('169.254.1.2')
        with self.assertRaisesRegex(dust.Error, 'Unsupported'):
            dust.lan_address(row)

    def test_address_or_adapter_change_stops_session(self):
        with patch.object(dust, 'ip', return_value=json.dumps([self.row()]).encode()):
            dust.lan_unchanged(self.session())
        for field, value in (('ifindex', 99), ('address', '02:00:00:00:00:02'),
                             ('addr_info', self.row('192.0.2.11')['addr_info'])):
            row = self.row(); row[field] = value
            with self.subTest(field=field), patch.object(dust, 'ip', return_value=json.dumps([row]).encode()):
                with self.assertRaises(dust.Error):
                    dust.lan_unchanged(self.session())

    def test_lan_cleanup_never_changes_connection(self):
        with patch.object(dust, 'read_json', return_value=self.session()), \
             patch.object(dust, 'firewall_cleanup') as firewall, \
             patch.object(dust, 'ip') as ip, patch.object(dust, 'bounded') as command, \
             patch.object(Path, 'unlink') as unlink:
            dust.cleanup()
            firewall.assert_called_once_with(self.session())
            ip.assert_not_called(); command.assert_not_called(); unlink.assert_called_once()

    def test_lan_service_launches_without_network_mutations(self):
        from unittest.mock import MagicMock
        child = MagicMock()
        child.pid = 999999
        # Alive at readiness, then exits to exercise the service's finally path.
        child.poll.side_effect = [None, None, 1]
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(dust, 'RUN', Path(temporary)), \
             patch.object(dust, 'read_json', side_effect=[{'interface': 'enp1s0', 'mode': 'lan'}, None]), \
             patch.object(dust, 'preflight', return_value=self.session()), \
             patch.object(dust, 'write_json'), patch.object(dust, 'bounded') as command, \
             patch.object(dust, 'ip') as ip, patch.object(dust, 'lan_unchanged'), \
             patch.object(dust, 'firewall_install') as firewall, patch.object(dust, 'cleanup') as cleanup, \
             patch.object(dust, 'notify_ready'), patch.object(dust.signal, 'signal'), \
             patch.object(dust.time, 'sleep'), patch.object(dust.os, 'killpg'), \
             patch.object(dust.subprocess, 'Popen', return_value=child) as spawn:
            with self.assertRaisesRegex(dust.Error, 'exited'):
                dust.serve()
            ip.assert_not_called()
            self.assertEqual([call.args[0][0] for call in spawn.call_args_list],
                             ['/usr/bin/dnsmasq', dust.LIBEXEC])
            command.assert_called_once_with([dust.LIBEXEC, 'probe'], timeout=10)
            firewall.assert_called_once_with(self.session())
            cleanup.assert_called_once()

    def test_failed_firewall_cleanup_keeps_recovery_state(self):
        with patch.object(dust, 'read_json', return_value=self.session()), \
             patch.object(dust, 'firewall_cleanup', side_effect=dust.Error('locked')), \
             patch.object(Path, 'unlink') as unlink:
            with self.assertRaises(dust.Error):
                dust.cleanup()
            unlink.assert_not_called()

    def test_dynamic_boot_script_uses_session_address(self):
        script = dust.boot_script(True, '192.0.2.10')
        self.assertIn(b'http://192.0.2.10:8080', script)
        self.assertNotIn(dust.SERVER.encode(), script)
        with self.assertRaises(ValueError):
            dust.boot_script(True, '192.0.2.10\nchain evil')

    def test_firewall_permissions_error_not_treated_as_absent(self):
        with patch.object(dust, 'bounded', side_effect=dust.CommandError('no permissions', 4)):
            with self.assertRaises(dust.Error):
                dust.firewall_exists('-S', 'PXDUST_001122334455')


if __name__ == "__main__":
    unittest.main()
