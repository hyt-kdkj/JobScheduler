"""One authenticated gateway connection per local user, with shared backoff."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import time


class GatewayUnavailable(RuntimeError):
    pass


def validate_gateway(settings):
    if settings is None:
        return
    if (not isinstance(settings, dict) or not isinstance(settings.get('host'), str) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', settings['host'])):
        raise ValueError('ssh_gateway.host must be an SSH alias')
    for key in ('control_persist_seconds', 'cooldown_seconds', 'max_cooldown_seconds'):
        value = settings.get(key, {'control_persist_seconds': 60,
                                  'cooldown_seconds': 60, 'max_cooldown_seconds': 600}[key])
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError('ssh_gateway.' + key + ' must be a positive integer')
    if settings.get('max_cooldown_seconds', 600) < settings.get('cooldown_seconds', 60):
        raise ValueError('ssh_gateway.max_cooldown_seconds must be >= cooldown_seconds')


class GatewayTransport:
    def __init__(self, settings, directory=None):
        validate_gateway(settings)
        self.host = settings['host']
        self.persist = settings.get('control_persist_seconds', 60)
        self.cooldown = settings.get('cooldown_seconds', 60)
        self.max_cooldown = settings.get('max_cooldown_seconds', 600)
        # Local sockets cannot live on the shared SAN. Keep lock/backoff state
        # beside them so separate checkouts and --state-dir values coordinate.
        self.directory = Path(directory) if directory is not None else Path(
            tempfile.gettempdir()) / f'job-scheduler-gateway-{os.getuid()}'
        key = hashlib.sha256(self.host.encode()).hexdigest()[:16]
        self.socket = self.directory / (key + '.sock')
        self.state = self.directory / (key + '.json')
        self.lock = self.directory / (key + '.lock')

    @contextmanager
    def _locked(self, timeout):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise GatewayUnavailable('Gateway control directory must be private and user-owned')
        deadline = time.monotonic() + timeout
        with self.lock.open('a') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GatewayUnavailable(f'{self.host}: another command is connecting to the gateway')
                    time.sleep(0.05)
            yield

    def _read(self):
        if not self.state.exists():
            return {}
        return json.loads(self.state.read_text())

    def _save(self, data):
        temporary = self.state.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        temporary.replace(self.state)

    def _failure(self, state, reason):
        failures = min(state.get('failures', 0) + 1, 20)
        delay = min(self.max_cooldown, self.cooldown * 2 ** (failures - 1))
        self._save(dict(failures=failures, retry_after=time.time() + delay, error=reason[-500:]))
        return GatewayUnavailable(f'{self.host}: gateway unavailable; retry paused for {delay}s: {reason[-500:]}')

    def alive(self):
        result = subprocess.run(['ssh', '-S', str(self.socket), '-O', 'check', self.host],
                                capture_output=True, text=True, timeout=3)
        return result.returncode == 0

    def proxy_option(self, lock_timeout=15):
        with self._locked(lock_timeout):
            state = self._read()
            remaining = state.get('retry_after', 0) - time.time()
            if remaining > 0:
                raise GatewayUnavailable(f'{self.host}: gateway reconnect paused for {int(remaining) + 1}s')
            try:
                if not self.alive():
                    command = ['ssh', '-x', '-T', '-M', '-N', '-f', '-S', str(self.socket),
                               '-o', 'ControlMaster=yes', '-o', f'ControlPersist={self.persist}',
                               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                               '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=10',
                               '-o', 'ServerAliveCountMax=2', self.host]
                    # A background master can inherit stderr. A pipe would make
                    # communicate() wait until that master exits; use a file.
                    with tempfile.TemporaryFile(mode='w+t') as errors:
                        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=errors,
                                                text=True, timeout=15)
                        errors.seek(0)
                        error = errors.read(2000)
                    if result.returncode:
                        raise self._failure(state, error.strip() or 'SSH gateway startup failed')
                    if not self.alive():
                        raise self._failure(state, 'SSH gateway control socket is unavailable after startup')
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise self._failure(state, type(exc).__name__) from exc
            self._save(dict(failures=0, retry_after=0))
        # If the master disappears, NEVER fall back to a new TCP connection
        # outside the startup lock. The next request will restart it safely.
        proxy = ['ssh', '-x', '-T', '-S', str(self.socket), '-o', 'ControlMaster=no',
                 '-o', 'BatchMode=yes', '-o', 'ProxyCommand=/bin/false',
                 '-W', '%h:%p', self.host]
        return ['-o', 'ProxyCommand=' + shlex.join(proxy)]

    def check_failure(self, error):
        """Target failures may use another host; losing the gateway may not."""
        with self._locked(2):
            state = self._read()
            if state.get('retry_after', 0) > time.time():
                raise GatewayUnavailable(f'{self.host}: gateway reconnect is paused')
            try:
                alive = self.alive()
            except (OSError, subprocess.TimeoutExpired):
                alive = False
            if not alive:
                raise self._failure(state, error)
