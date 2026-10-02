#!/usr/bin/python3 -I
"""Unprivileged transaction coordinator; elevate fixed system utilities only."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
DATA = Path('/var/lib/pxe-dust')
RECEIPT = DATA / 'installation.json'
PENDING = DATA / 'installation-pending.json'
FILES = {
    Path('/usr/local/libexec/pxe-dust'): ('bin/pxe-dust', 0o755),
    Path('/usr/local/bin/pxe-dust'): ('bin/pxe-dust', 0o755),
    Path('/etc/systemd/system/pxe-dust.service'): ('system/pxe-dust.service', 0o644),
    Path('/usr/share/polkit-1/actions/org.techluddite.pxe-dust.policy'):
        ('system/org.techluddite.pxe-dust.policy', 0o644),
}
ENV = {'PATH': '/usr/bin:/usr/sbin', 'LANG': 'C', 'LC_ALL': 'C'}
LIMIT = 512 * 1024


class InstallError(Exception):
    pass


def checked_file(path, owner=0):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or (owner == 0 and info.st_gid != 0) or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > LIMIT):
            raise InstallError(f'Refusing untrusted file: {path}')
        raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise InstallError(f'File exceeds installation limit: {path}')
        return raw, stat.S_IMODE(info.st_mode)


def protected_directory(path):
    """Validate each parent without following symlinks; never repair unknown trees."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise InstallError(f'Refusing unprotected directory: {path}')
    finally:
        os.close(fd)


class System:
    def __init__(self):
        self.holder = None

    def run(self, argv, privileged=True, stdin=None):
        if self.holder is not None and self.holder.poll() is not None:
            raise InstallError('Installation lock supervisor exited unexpectedly')
        command = (['/usr/bin/sudo', '-n', '--', '/usr/bin/timeout', '--kill-after=2s', '20s']
                   if privileged else []) + list(map(str, argv))
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=stdin, env=ENV, start_new_session=True)
        except BaseException:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            raise
        buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for stream in buffers:
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    for key, _ in selector.select(1):
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            buffers[key.fileobj].extend(chunk)
                            if len(buffers[key.fileobj]) > 8192:
                                raise InstallError('System utility output exceeds limit')
            proc.wait()
        except BaseException:
            # Root timeout owns termination/reaping of its utility. Retain the
            # transaction lock until it finishes, including after interruption.
            # Never kill only sudo and leave its privileged child running.
            proc.wait()
            raise
        finally:
            proc.stdout.close()
            proc.stderr.close()
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        stdout, stderr = bytes(buffers[proc.stdout]), bytes(buffers[proc.stderr])
        if proc.returncode:
            raise InstallError(f'{Path(argv[0]).name} failed: {stderr.decode("utf-8", "replace")[:1000]}')
        return stdout

    def directory(self, path):
        try:
            protected_directory(path)
            return
        except FileNotFoundError:
            self.directory(path.parent)
        self.run(['/usr/bin/install', '-d', '-o', 'root', '-g', 'root', '-m', '0755', '--', path])
        protected_directory(path)

    @contextlib.contextmanager
    def lock(self):
        path = DATA / 'lock'
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise InstallError('Refusing untrusted operation lock')
        except FileNotFoundError:
            pass  # flock creates the root-owned file without replacing any inode.
        token = (uuid.uuid4().hex + '\n').encode()
        holder = subprocess.Popen(['/usr/bin/sudo', '-n', '--', '/usr/bin/flock', '--nonblock',
                                   '--no-fork', str(path), '/usr/bin/cat'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=ENV, start_new_session=True)
        self.holder = holder
        try:
            holder.stdin.write(token)
            holder.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(holder.stdout, selectors.EVENT_READ)
                if not selector.select(10) or os.read(holder.stdout.fileno(), len(token)) != token:
                    raise InstallError('Could not acquire installation lock; another operation may be running')
            if holder.poll() is not None:
                raise InstallError('Installation lock supervisor exited')
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise InstallError('Refusing untrusted operation lock')
            self.run(['/usr/bin/chmod', '0600', '--', path])
            yield
        finally:
            self.holder = None
            holder.stdin.close()
            holder.wait(timeout=5)
            holder.stdout.close()
            holder.stderr.close()

    def stopped(self):
        raw = self.run(['/usr/bin/systemctl', 'show', '--property=LoadState',
                        '--property=ActiveState', 'pxe-dust.service'])
        fields = dict(line.split('=', 1) for line in raw.decode('ascii').splitlines())
        if (set(fields) != {'LoadState', 'ActiveState'}
                or fields['LoadState'] not in ('loaded', 'not-found')
                or fields['ActiveState'] not in ('inactive', 'failed')
                or (fields['LoadState'] == 'not-found' and fields['ActiveState'] != 'inactive')):
            raise InstallError('Stop Luddite PXE Dust before installing; service state must be known and stopped')

    def copy(self, source, target, mode):
        # Root must never reopen a user-writable source path: a swapped symlink
        # could disclose a root-only file before post-copy hash validation.
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                    or (info.st_uid == 0 and info.st_gid != 0)
                    or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > LIMIT):
                raise InstallError('Refusing untrusted installation source')
            self.run(['/usr/bin/install', '-o', 'root', '-g', 'root', '-m', f'{mode:04o}',
                      '--', '/proc/self/fd/0', target], stdin=fd)
        finally:
            os.close(fd)

    def move(self, source, target):
        self.run(['/usr/bin/mv', '-fT', '--', source, target])

    def remove(self, path):
        self.run(['/usr/bin/rm', '--', path])

    def rmdir(self, path):
        self.run(['/usr/bin/rmdir', '--', path])

    def reload(self):
        self.run(['/usr/bin/systemctl', 'daemon-reload'])


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def install(system, root=ROOT):
    """Stage all public files before publishing; rollback failures retain backups."""
    with tempfile.TemporaryDirectory(prefix='pxe-dust-install-') as temporary:
        snapshots = {}
        for target, (source, mode) in FILES.items():
            raw, _ = checked_file(root / source, owner=os.geteuid())
            snapshot = Path(temporary) / str(len(snapshots))
            snapshot.write_bytes(raw)
            snapshots[target] = (snapshot, digest(raw), mode)
        receipt_source = Path(temporary) / 'receipt'
        receipt_payload = (json.dumps({str(p): h for p, (_, h, _) in snapshots.items()}) + '\n').encode()
        receipt_source.write_bytes(receipt_payload)
        snapshots[RECEIPT] = (receipt_source, digest(receipt_payload), 0o644)
        system.directory(DATA)
        with system.lock():
            system.stopped()
            if PENDING.exists() or PENDING.is_symlink():
                raise InstallError(f'Interrupted installation requires review: {PENDING}; retained backups are listed there')
            try:
                previous, _ = checked_file(RECEIPT)
                ownership = json.loads(previous)
                if not isinstance(ownership, dict) or set(ownership) != {str(p) for p in FILES}:
                    raise InstallError('Invalid installation receipt')
            except FileNotFoundError:
                ownership = None
            originals = {}
            for target in snapshots:
                system.directory(target.parent)
                try:
                    raw, mode = checked_file(target)
                except FileNotFoundError:
                    if ownership is not None:
                        raise InstallError(f'Installation is incomplete: {target}')
                    originals[target] = None
                    continue
                if target != RECEIPT and (ownership is None or ownership[str(target)] != digest(raw)):
                    raise InstallError(f'Refusing to replace unowned or modified file: {target}')
                originals[target] = (digest(raw), mode)
            transaction = uuid.uuid4().hex
            stages, backups, directories = {}, {}, []
            committed = []
            marked = False
            complete = False
            try:
                for target, (snapshot, expected, mode) in snapshots.items():
                    directory = target.parent / ('.pxe-dust-install-' + transaction)
                    system.directory(directory)
                    directories.append(directory)
                    stage = directory / 'new'
                    stages[target] = stage
                    system.copy(snapshot, stage, mode)
                    raw, actual_mode = checked_file(stage)
                    if digest(raw) != expected or actual_mode != mode:
                        raise InstallError('Staged installation differs from reviewed snapshot')
                    if originals[target] is not None:
                        backup = directory / 'previous'
                        backups[target] = backup
                        system.copy(target, backup, originals[target][1])
                        if digest(checked_file(backup)[0]) != originals[target][0]:
                            raise InstallError('Installed file changed while staging backup')
                journal = Path(temporary) / 'pending'
                journal_payload = (json.dumps({'transaction': transaction, 'files': [
                    {'target': str(p), 'previous': str(backups[p]) if p in backups else None,
                     'previous_sha256': originals[p][0] if originals[p] else None,
                     'new': str(stages[p]), 'new_sha256': snapshots[p][1]} for p in snapshots]}) + '\n').encode()
                journal.write_bytes(journal_payload)
                pending_stage = stages[RECEIPT].parent / 'pending'
                system.copy(journal, pending_stage, 0o644)
                if digest(checked_file(pending_stage)[0]) != digest(journal_payload):
                    raise InstallError('Staged installation journal changed')
                system.run(['/usr/bin/sync'])
                marked = True
                system.move(pending_stage, PENDING)
                system.run(['/usr/bin/sync', '-f', DATA])
                system.stopped()
                for target, stage in stages.items():
                    # Record before rename: interruption after success must still restore it.
                    committed.append(target)
                    system.move(stage, target)
                system.reload()
                system.run(['/usr/bin/sync'])
                complete = True
            except BaseException as exc:
                failures = []
                for target in reversed(committed):
                    try:
                        if target in backups:
                            system.move(backups[target], target)
                        elif target.exists() or target.is_symlink():
                            system.remove(target)
                    except Exception as rollback:
                        failures.append(str(rollback))
                if committed:
                    try:
                        system.reload()
                    except Exception as rollback:
                        failures.append(str(rollback))
                if marked and not failures:
                    try:
                        system.run(['/usr/bin/sync'])
                    except Exception as rollback:
                        failures.append(str(rollback))
                if failures:
                    raise InstallError(f'Install failed and rollback needs review; retain {PENDING}: '
                                       + '; '.join(failures)) from exc
                complete = True  # Successful rollback also permits staging cleanup.
                raise
            finally:
                if complete or not marked:
                    if marked and (PENDING.exists() or PENDING.is_symlink()):
                        system.remove(PENDING)
                        system.run(['/usr/bin/sync', '-f', DATA])
                    if 'pending_stage' in locals() and pending_stage.exists():
                        system.remove(pending_stage)
                    for stage in stages.values():
                        if stage.exists():
                            system.remove(stage)
                    for backup in backups.values():
                        if backup.exists():
                            system.remove(backup)
                    for directory in reversed(directories):
                        system.rmdir(directory)


def main():
    if os.geteuid() == 0:
        raise InstallError('Run make install as your normal user; checkout code must not run as root')
    # Authenticate on the user's terminal before using the lock supervisor's pipes.
    subprocess.run(['/usr/bin/sudo', '-v'], check=True, timeout=120)
    def interrupted(signum, frame):
        raise InstallError('Installation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    install(System())
    print('Installed reviewed system files. The service remains stopped.')


if __name__ == '__main__':
    try:
        main()
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Installation failed: {error}', file=sys.stderr)
        sys.exit(1)
