"""Gateway admission and reconnect policy. No network access or real SSH."""
import json
import os
import pwd
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import scheduler as scheduler
from ssh_gateway import GatewayTransport, GatewayUnavailable, validate_gateway


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = GatewayTransport({'host': 'test-gateway'}, self.root / 'control')

    def test_master_is_reused_and_proxy_cannot_fall_back_to_new_connection(self):
        with patch.object(self.gateway, 'alive', return_value=True), \
             patch('ssh_gateway.subprocess.run') as run:
            options = self.gateway.proxy_option()
        run.assert_not_called()
        self.assertIn('ProxyCommand=/bin/false', options[1])
        self.assertIn(str(self.gateway.socket), options[1])
        self.assertIn('-W %h:%p', options[1])

    def test_failed_handshake_pauses_other_processes_and_increases_backoff(self):
        def failure(command, **kwargs):
            kwargs['stderr'].write('Exceeded MaxStartups')
            return subprocess.CompletedProcess(command, 255)
        with patch.object(GatewayTransport, 'alive', return_value=False), \
             patch('ssh_gateway.subprocess.run', side_effect=failure) as run:
            with self.assertRaisesRegex(GatewayUnavailable, '60s.*Exceeded MaxStartups'):
                self.gateway.proxy_option()
            other = GatewayTransport({'host': 'test-gateway'}, self.root / 'control')
            with self.assertRaisesRegex(GatewayUnavailable, 'paused'):
                other.proxy_option()
            self.assertEqual(run.call_count, 1)
            data = json.loads(self.gateway.state.read_text())
            data['retry_after'] = 0
            self.gateway.state.write_text(json.dumps(data))
            with self.assertRaisesRegex(GatewayUnavailable, '120s'):
                other.proxy_option()
            self.assertEqual(run.call_count, 2)

    def test_gateway_failure_stops_shared_queue_fallback(self):
        cfg = {'shared_queue': {'host': 'a'}, 'hosts': [{'ssh': 'a'}, {'ssh': 'b'}]}
        with patch.object(scheduler, 'remote', side_effect=GatewayUnavailable('paused')) as remote:
            with self.assertRaises(GatewayUnavailable):
                scheduler.shared_rpc(cfg, {'action': 'shared_sync', 'jobs': []})
        self.assertEqual(remote.call_count, 1)

    def test_target_failure_does_not_pause_healthy_gateway(self):
        self.gateway.directory.mkdir(mode=0o700)
        with patch.object(self.gateway, 'alive', return_value=True):
            self.gateway.check_failure('target refused connection')
        self.assertFalse(self.gateway.state.exists())

    def test_lost_master_pauses_reconnection(self):
        with patch.object(self.gateway, 'alive', return_value=False):
            with self.assertRaisesRegex(GatewayUnavailable, 'paused for 60s'):
                self.gateway.check_failure('control connection lost')

    def test_settings_validation(self):
        for settings in ({'host': 'bad/alias'}, {'host': 42}, {'host': True},
                         {'host': 'x', 'cooldown_seconds': 0},
                         {'host': 'x', 'control_persist_seconds': True},
                         {'host': 'x', 'cooldown_seconds': 700, 'max_cooldown_seconds': 600}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate_gateway(settings)

    def test_private_control_directory_is_required(self):
        self.gateway.directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(GatewayUnavailable, 'private'):
            self.gateway.proxy_option()

    def test_simultaneous_processes_create_only_one_master(self):
        binary = self.root / 'bin'
        binary.mkdir()
        fake = binary / 'ssh'
        fake.write_text('''#!/usr/bin/env python3
import pathlib,sys,time
args=sys.argv[1:]
socket=pathlib.Path(args[args.index('-S')+1])
if '-O' in args:
    sys.exit(0 if socket.exists() else 255)
assert '-M' in args
with socket.with_suffix('.attempts').open('a') as log:log.write('start\\n')
time.sleep(0.15)
socket.touch()
''')
        fake.chmod(0o755)
        code = '''
import sys
from ssh_gateway import GatewayTransport
print(GatewayTransport({'host':'test-gateway'},sys.argv[1]).proxy_option())
'''
        env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'])
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.gateway.directory)],
                                     cwd=Path(__file__).parent, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(4)]
        try:
            for process in processes:
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                self.assertIn('ProxyCommand=', output)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(self.gateway.socket.with_suffix('.attempts').read_text(), 'start\n')

    @unittest.skipUnless(shutil.which('sshd') and shutil.which('ssh-keygen'), 'local sshd required')
    def test_real_ssh_proxy_reuses_master_and_refuses_fallback(self):
        # Entirely local SSH server with temporary keys, config, and control
        # socket. Neither hpcgate nor the user's SSH configuration is contacted.
        ssh = shutil.which('ssh')
        key = self.root / 'key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        server_config = self.root / 'sshd_config'
        server_config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {key}
PidFile {self.root}/sshd.pid
AuthorizedKeysFile {key}.pub
StrictModes no
PasswordAuthentication no
UsePAM no
AllowTcpForwarding yes
LogLevel ERROR
''')
        client_config = self.root / 'ssh_config'
        client_config.write_text(f'''Host test-gateway test-target
  HostName 127.0.0.1
  Port {port}
  User {pwd.getpwuid(os.getuid()).pw_name}
  IdentityFile {key}
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile {self.root}/known_hosts
''')
        binary = self.root / 'bin'
        binary.mkdir()
        wrapper = binary / 'ssh'
        import shlex
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.join([ssh, '-F', str(client_config)]) + ' "$@"\n')
        wrapper.chmod(0o755)
        with (self.root / 'sshd.log').open('w') as log:
            daemon = subprocess.Popen([shutil.which('sshd'), '-D', '-e', '-f', str(server_config)],
                                      stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 5
                while True:
                    if daemon.poll() is not None:
                        self.skipTest('local sshd cannot start: ' + (self.root / 'sshd.log').read_text())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail('local sshd did not listen')
                        time.sleep(0.05)
                with patch.dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH']):
                    try:
                        proxy = self.gateway.proxy_option()
                        result = subprocess.run(['ssh', '-o', 'BatchMode=yes'] + proxy +
                                                ['test-target', 'printf gateway-ok'],
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, 'gateway-ok')
                        self.assertTrue(self.gateway.alive())
                        subprocess.run(['ssh', '-S', str(self.gateway.socket), '-O', 'exit', 'test-gateway'],
                                       capture_output=True, timeout=5)
                        # Use the old proxy after the master exits. Even though
                        # the server is still available, it must not reconnect.
                        result = subprocess.run(['ssh', '-o', 'BatchMode=yes'] + proxy +
                                                ['test-target', 'printf forbidden'],
                                                capture_output=True, text=True, timeout=5)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn('forbidden', result.stdout)
                    finally:
                        subprocess.run(['ssh', '-S', str(self.gateway.socket), '-O', 'exit', 'test-gateway'],
                                       capture_output=True, timeout=5)
            finally:
                daemon.terminate()
                daemon.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
