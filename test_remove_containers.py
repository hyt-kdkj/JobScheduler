"""Container removal tests without SSH or real Docker operations."""
import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

import remove_containers as r


class RemovalTest(unittest.TestCase):
    def test_dry_run_uses_default_hosts_without_connecting(self):
        output = io.StringIO()
        with patch.object(r, 'remove') as remove, contextlib.redirect_stdout(output):
            self.assertEqual(r.main(['--dry-run']), 0)
        remove.assert_not_called()
        self.assertEqual([line.split(':')[0] for line in output.getvalue().splitlines()],
                         list(r.DEFAULT_HOSTS))

    def test_partial_failure_does_not_skip_other_hosts(self):
        def remove(host, container):
            return host, host != 'offline', container
        with patch.object(r, 'remove', side_effect=remove) as call, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(r.main(['a', 'offline', 'b', '--container', 'named']), 1)
        self.assertCountEqual([c.args for c in call.call_args_list],
                              [('a', 'named'), ('offline', 'named'), ('b', 'named')])

    def test_remote_removes_only_exact_name_and_verifies(self):
        responses = [subprocess.CompletedProcess([], 0, 'target\ntarget-other\n', ''),
                     subprocess.CompletedProcess([], 0, 'target', ''),
                     subprocess.CompletedProcess([], 0, 'target-other\n', '')]
        with patch.object(sys, 'argv', ['-', 'target']), \
             patch.object(subprocess, 'run', side_effect=responses) as call, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            exec(r.REMOTE_PROGRAM, {})
        self.assertEqual(output.getvalue().strip(), 'REMOVED')
        self.assertEqual(call.call_args_list[1].args[0],
                         ['docker', 'container', 'rm', '--force', 'target'])
        self.assertEqual(call.call_count, 3)

    def test_missing_is_success_without_removal(self):
        with patch.object(sys, 'argv', ['-', 'target']), \
             patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(
                 [], 0, 'target-other\n', '')) as call, \
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
            self.assertFalse(r.remove('a', 'target')[1])


if __name__ == '__main__':
    unittest.main()
