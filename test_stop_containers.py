"""Container stop tests without SSH or real Docker operations."""
import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

import stop_containers as r


class StopTest(unittest.TestCase):
    def test_dry_run_uses_default_hosts_without_connecting(self):
        output = io.StringIO()
        with patch.object(r, 'stop') as remove, contextlib.redirect_stdout(output):
            self.assertEqual(r.main(['--dry-run']), 0)
        remove.assert_not_called()
        self.assertEqual([line.split(':')[0] for line in output.getvalue().splitlines()],
                         ['glockenspiel', 'marimba', 'xylophone1', 'xylophone3'])

    def test_partial_failure_does_not_skip_other_hosts(self):
        def remove(host, container):
            return host, host != 'offline', container
        with patch.object(r, 'stop', side_effect=remove) as call, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(r.main(['a', 'offline', 'b', '--container', 'named']), 1)
        self.assertCountEqual([c.args for c in call.call_args_list],
                              [('a', 'named'), ('offline', 'named'), ('b', 'named')])

    def test_remote_stops_only_exact_name_and_verifies_retention(self):
        responses = [subprocess.CompletedProcess([], 0, 'target\trunning\ntarget-other\trunning\n', ''),
                     subprocess.CompletedProcess([], 0, 'target', ''),
                     subprocess.CompletedProcess([], 0, 'target\texited\ntarget-other\trunning\n', '')]
        with patch.object(sys, 'argv', ['-', 'target']), \
             patch.object(subprocess, 'run', side_effect=responses) as call, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            exec(r.REMOTE_PROGRAM, {})
        self.assertEqual(output.getvalue().strip(), 'STOPPED')
        self.assertEqual(call.call_args_list[1].args[0],
                         ['docker', 'container', 'stop', '--time', '10', 'target'])
        self.assertEqual(call.call_count, 3)

    def test_missing_is_success_without_removal(self):
        with patch.object(sys, 'argv', ['-', 'target']), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                 [], 0, 'target-other\trunning\n', '')) as call, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            exec(r.REMOTE_PROGRAM, {})
        self.assertEqual(output.getvalue().strip(), 'ABSENT')
        call.assert_called_once()

    def test_docker_error_is_not_reported_as_missing(self):
        with patch.object(sys, 'argv', ['-', 'target']), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                 [], 1, '', 'daemon unavailable')) as call, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            exec(r.REMOTE_PROGRAM, {})
        self.assertEqual(exc.exception.code, 1)
        call.assert_called_once()

    def test_ssh_timeout_is_failure(self):
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 90)):
            self.assertFalse(r.stop('a', 'target')[1])

    def test_already_stopped_is_retained_without_docker_stop(self):
        with patch.object(sys, 'argv', ['-', 'target']), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                 [], 0, 'target\texited\n', '')) as call, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            exec(r.REMOTE_PROGRAM, {})
        self.assertEqual(output.getvalue().strip(), 'ALREADY_STOPPED')
        call.assert_called_once()

    def test_verification_rejects_running_or_deleted_container(self):
        for final in ('target\trunning\n', ''):
            with self.subTest(final=final):
                responses = [subprocess.CompletedProcess([], 0, 'target\trunning\n', ''),
                             subprocess.CompletedProcess([], 0, 'target', ''),
                             subprocess.CompletedProcess([], 0, final, '')]
                with patch.object(sys, 'argv', ['-', 'target']), \
                     patch.object(subprocess, 'run', side_effect=responses), \
                     contextlib.redirect_stderr(io.StringIO()), \
                     self.assertRaises(SystemExit) as exc:
                    exec(r.REMOTE_PROGRAM, {})
                self.assertEqual(exc.exception.code, 1)

    def test_old_command_also_only_stops(self):
        import remove_containers
        self.assertIs(remove_containers.main, r.main)


if __name__ == '__main__':
    unittest.main()
