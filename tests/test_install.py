import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('install_system', Path(__file__).resolve().parents[1] / 'tools/install-system.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FixtureSystem(installer.System):
    """All mutations stay in one temporary tree; no sudo or host services."""
    def __init__(self, fail=None, states=None):
        super().__init__()
        self.operations = []
        self.fail = fail
        self.states = iter(states or [('loaded', 'inactive')] * 2)
        self.locked = False

    def event(self, name, *args):
        if name not in ('directory', 'lock'):
            assert self.locked, f'{name} escaped the operation lock'
        self.operations.append((name, *args))
        if self.fail and self.fail(name, *args):
            raise installer.InstallError(f'injected {name} failure')

    def directory(self, path):
        self.event('directory', path)
        path.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def lock(self):
        self.event('lock')
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def run(self, argv, privileged=True):
        self.event('run', argv)
        if argv[0] == '/usr/bin/systemctl':
            load, active = next(self.states)
            return f'LoadState={load}\nActiveState={active}\n'.encode()
        return b''

    def copy(self, source, target, mode):
        self.event('copy', source, target)
        shutil.copyfile(source, target)
        target.chmod(mode)

    def move(self, source, target):
        self.event('move', source, target)
        source.replace(target)

    def remove(self, path):
        self.event('remove', path)
        path.unlink()

    def rmdir(self, path):
        self.event('rmdir', path)
        path.rmdir()

    def reload(self):
        self.event('reload')


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pxe-dust-test-install-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'checkout'
        self.source.mkdir()
        self.files = {self.base / str(index) / 'artifact': (f'source{index}', mode)
                      for index, mode in enumerate((0o755, 0o755, 0o644, 0o644))}
        for index, (source, _) in enumerate(self.files.values()):
            (self.source / source).write_text(f'new reviewed bytes {index}')
        self.data = self.base / 'data'
        self.receipt = self.data / 'installation.json'
        self.pending = self.data / 'installation-pending.json'
        self.addCleanup(patch.stopall)
        patch.object(installer, 'FILES', self.files).start()
        patch.object(installer, 'DATA', self.data).start()
        patch.object(installer, 'RECEIPT', self.receipt).start()
        patch.object(installer, 'PENDING', self.pending).start()
        original = installer.checked_file
        patch.object(installer, 'checked_file', side_effect=lambda path, owner=0:
                     original(path, owner=os.geteuid())).start()

    def existing(self):
        ownership = {}
        for index, target in enumerate(self.files):
            target.parent.mkdir()
            raw = f'old reviewed bytes {index}'.encode()
            target.write_bytes(raw)
            target.chmod(0o700 if index < 2 else 0o600)
            ownership[str(target)] = installer.digest(raw)
        self.data.mkdir()
        self.receipt.write_text(json.dumps(ownership))
        self.receipt.chmod(0o640)
        return {target: (target.read_bytes(), target.stat().st_mode & 0o777)
                for target in [*self.files, self.receipt]}

    def assert_preserved(self, originals):
        for target, (raw, mode) in originals.items():
            self.assertEqual(target.read_bytes(), raw)
            self.assertEqual(target.stat().st_mode & 0o777, mode)
        self.assertFalse(self.pending.exists())
        self.assertFalse(list(self.base.glob('*/.pxe-dust-install-*')))

    def test_fresh_install_and_upgrade_publish_matching_receipt(self):
        for upgrading in (False, True):
            with self.subTest(upgrading=upgrading):
                system = FixtureSystem()
                installer.install(system, self.source)
                receipt = json.loads(self.receipt.read_text())
                for target, (source, mode) in self.files.items():
                    self.assertEqual(target.read_bytes(), (self.source / source).read_bytes())
                    self.assertEqual(target.stat().st_mode & 0o777, mode)
                    self.assertEqual(receipt[str(target)], installer.digest(target.read_bytes()))
                self.assertFalse(self.pending.exists())
                self.assertEqual([e[0] for e in system.operations].count('reload'), 1)

    def test_rejects_every_unknown_or_transitional_state_before_mutation(self):
        for load, active in [('loaded', 'active'), ('loaded', 'activating'),
                             ('loaded', 'deactivating'), ('loaded', 'reloading'),
                             ('loaded', ''), ('error', 'inactive'), ('not-found', 'failed')]:
            with self.subTest(load=load, active=active):
                system = FixtureSystem(states=[(load, active)])
                with self.assertRaises(installer.InstallError):
                    installer.install(system, self.source)
                self.assertFalse(any(e[0] == 'copy' for e in system.operations))

    def test_first_install_requires_reachable_system_manager(self):
        system = FixtureSystem(fail=lambda name, *args: name == 'run')
        with self.assertRaises(installer.InstallError):
            installer.install(system, self.source)
        self.assertFalse(self.receipt.exists())

    def test_not_found_inactive_is_safe_first_install(self):
        installer.install(FixtureSystem(states=[('not-found', 'inactive')] * 2), self.source)
        self.assertTrue(self.receipt.exists())

    def test_rollback_staging_publication_and_reload_failures(self):
        original = self.existing()
        for phase in ('copy', 'move', 'reload'):
            for location in range(5 if phase != 'reload' else 1):
                with self.subTest(phase=phase, location=location):
                    counter = [0]
                    def fail(name, *args):
                        if name != phase:
                            return False
                        if phase == 'copy' and Path(args[1]).name != 'new':
                            return False
                        if phase == 'move' and Path(args[1]) == self.pending:
                            return False
                        counter[0] += 1
                        return counter[0] == location + 1
                    with self.assertRaises(installer.InstallError):
                        installer.install(FixtureSystem(fail=fail), self.source)
                    self.assert_preserved(original)

    def test_fresh_install_rollback_removes_only_created_artifacts(self):
        for location in range(1, 6):
            with self.subTest(location=location):
                count = [0]
                def fail(name, *args):
                    if name == 'move' and Path(args[1]) != self.pending:
                        count[0] += 1
                        return count[0] == location
                    return False
                with self.assertRaises(installer.InstallError):
                    installer.install(FixtureSystem(fail=fail), self.source)
                self.assertFalse(any(target.exists() for target in [*self.files, self.receipt]))
                self.assertFalse(self.pending.exists())

    def test_state_change_at_commit_rolls_back_staging_only(self):
        original = self.existing()
        with self.assertRaises(installer.InstallError):
            installer.install(FixtureSystem(states=[('loaded', 'inactive'), ('loaded', 'activating')]), self.source)
        self.assert_preserved(original)

    def test_rejects_modified_and_unowned_artifacts(self):
        original = self.existing()
        target = next(iter(self.files))
        target.write_text('administrator changes')
        for has_receipt in (True, False):
            if not has_receipt:
                self.receipt.unlink()
            with self.assertRaises(installer.InstallError):
                installer.install(FixtureSystem(), self.source)
            self.assertEqual(target.read_text(), 'administrator changes')

    def test_rollback_failure_retains_marker_and_backups_then_refuses_retry(self):
        self.existing()
        reload_count = [0]
        def fail(name, *args):
            if name == 'reload':
                reload_count[0] += 1
                return reload_count[0] == 1
            return name == 'move' and Path(args[0]).name == 'previous'
        with self.assertRaisesRegex(installer.InstallError, 'rollback needs review'):
            installer.install(FixtureSystem(fail=fail), self.source)
        self.assertTrue(self.pending.exists())
        self.assertTrue(list(self.base.glob('*/.pxe-dust-install-*/previous')))
        with self.assertRaisesRegex(installer.InstallError, 'Interrupted installation'):
            installer.install(FixtureSystem(), self.source)

    def test_checkout_snapshot_change_is_detected_before_publication(self):
        original = self.existing()
        class ChangedCopy(FixtureSystem):
            def copy(self, source, target, mode):
                super().copy(source, target, mode)
                if target.name == 'new':
                    target.write_text('changed after authorization')
        with self.assertRaisesRegex(installer.InstallError, 'differs from reviewed snapshot'):
            installer.install(ChangedCopy(), self.source)
        self.assert_preserved(original)

    def test_real_copy_descriptor_prevents_source_symlink_swap(self):
        source = self.base / 'snapshot'
        source.write_text('reviewed bytes')
        secret = self.base / 'private'
        secret.write_text('private bytes must never be copied')
        target = self.base / 'staged'
        system = installer.System()
        def utility(argv, privileged=True, stdin=None):
            source.unlink()
            source.symlink_to(secret)
            # Run the actual system install utility in the private test tree,
            # without ownership changes or privilege elevation.
            self.assertEqual(argv[-2], '/proc/self/fd/0')
            subprocess.run([argv[0], '-m', '0644', '--', *map(str, argv[-2:])],
                           stdin=stdin, check=True)
        with patch.object(system, 'run', side_effect=utility):
            system.copy(source, target, 0o644)
        self.assertEqual(target.read_text(), 'reviewed bytes')

    def test_real_copy_rejects_symlink_and_fifo_before_root_utility(self):
        source = self.base / 'snapshot'
        source.symlink_to(self.source / 'source0')
        system = installer.System()
        with patch.object(system, 'run') as run:
            with self.assertRaises(OSError):
                system.copy(source, self.base / 'staged', 0o644)
            source.unlink()
            os.mkfifo(source)
            with self.assertRaises(installer.InstallError):
                system.copy(source, self.base / 'staged', 0o644)
            run.assert_not_called()

    def test_system_run_elevates_only_deadline_utility_with_streaming_cap(self):
        original = installer.subprocess.Popen
        observed = []
        def without_sudo(argv, **kwargs):
            self.assertEqual(argv[:4], ['/usr/bin/sudo', '-n', '--', '/usr/bin/timeout'])
            self.assertEqual(argv[4:6], ['--kill-after=2s', '20s'])
            observed.append(argv)
            return original(argv[3:], **kwargs)
        with patch.object(installer.subprocess, 'Popen', side_effect=without_sudo):
            system = installer.System()
            self.assertEqual(system.run(['/usr/bin/printf', 'bounded']), b'bounded')
            with self.assertRaisesRegex(installer.InstallError, 'output exceeds limit'):
                system.run(['/usr/bin/head', '-c', '9000', '/dev/zero'])
        self.assertEqual(len(observed), 2)

    def test_root_deadline_supervisor_reaps_unresponsive_utility(self):
        original = installer.subprocess.Popen
        marker = self.base / 'late-mutation'
        def without_sudo(argv, **kwargs):
            command = [*argv[3:]]
            command[1:3] = ['--kill-after=0.05s', '0.05s']
            return original(command, **kwargs)
        script = ('import pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                  f'time.sleep(0.3);pathlib.Path({str(marker)!r}).write_text("late")')
        with patch.object(installer.subprocess, 'Popen', side_effect=without_sudo):
            with self.assertRaises(installer.InstallError):
                installer.System().run([sys.executable, '-c', script])
        self.assertFalse(marker.exists())

    def test_real_flock_serializes_and_releases_same_inode_without_root(self):
        self.data.mkdir()
        lock = self.data / 'lock'
        lock.touch(mode=0o600)
        inode = lock.stat().st_ino
        original_popen = installer.subprocess.Popen
        original_lstat = Path.lstat
        def fixture_lstat(path):
            info = original_lstat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0,
                                   st_nlink=info.st_nlink)
        def without_sudo(argv, **kwargs):
            self.assertEqual(argv[:3], ['/usr/bin/sudo', '-n', '--'])
            return original_popen(argv[3:], **kwargs)
        first, second = installer.System(), installer.System()
        with patch.object(installer.subprocess, 'Popen', side_effect=without_sudo), \
             patch.object(Path, 'lstat', fixture_lstat), \
             patch.object(first, 'run'), patch.object(second, 'run'):
            with first.lock():
                with self.assertRaises((installer.InstallError, OSError)):
                    with second.lock():
                        self.fail('Second coordinator acquired held mutation lock')
                self.assertEqual(lock.stat().st_ino, inode)
            with second.lock():
                self.assertEqual(lock.stat().st_ino, inode)


class InstalledOwnershipTests(unittest.TestCase):
    def test_administrator_group_change_is_refused_before_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'installed-file'
            source.write_bytes(b'previous installed bytes')
            info = os.stat_result((stat.S_IFREG | 0o750, 1, 1, 1, 0, 1000,
                                   source.stat().st_size, 0, 0, 0))
            with patch.object(installer.os, 'fstat', return_value=info):
                with self.assertRaises(installer.InstallError):
                    installer.checked_file(source)
                system = installer.System()
                with patch.object(system, 'run') as utility:
                    with self.assertRaises(installer.InstallError):
                        system.copy(source, Path(temporary) / 'backup', 0o750)
                    utility.assert_not_called()


if __name__ == '__main__':
    unittest.main()
