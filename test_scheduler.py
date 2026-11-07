"""Scheduling, failure recovery and real detached-tmux lifecycle tests (no SSH/GPU)."""
import contextlib
import copy
import io
import json
import os
import select
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import scheduler as s


def fixture_configuration():
    cfg = s.configuration(s.HERE / 'hosts.json')
    # Tests exercise a second worktree independently of enabled user projects.
    cfg['projects'].setdefault('V2VSimulator-division', dict(
        container='v2vsimulator-division-v2v-dev-1',
        workdir='/workspace/V2VSimulator/Simulator',
        template_start_script='~/workspace/V2VSimulator-division/docker-shell.sh'))
    return cfg


class StatusHistoryTest(unittest.TestCase):
    def test_two_day_cutoff_uses_finish_time_and_preserves_unfinished_jobs(self):
        now = 1000000
        cutoff = now - 172800
        jobs = [dict(id='old', status='succeeded', created=1, finished=cutoff - 1),
                dict(id='boundary', status='succeeded', created=1, finished=cutoff),
                dict(id='recent', status='succeeded', created=1, finished=cutoff + 1),
                dict(id='missing-finish', status='succeeded', created=1),
                dict(id='null-finish', status='succeeded', created=1, finished=None),
                dict(id='failed', status='failed', created=now),
                dict(id='cancelled', status='cancelled', created=now)]
        active = ('queued', 'running', 'reserved', 'dispatching', 'cancelling', 'unknown')
        jobs.extend(dict(id=state, status=state, created=1) for state in active)
        self.assertEqual([j['id'] for j in s.status_jobs(jobs, now=now)], ['recent', *active])
        self.assertEqual(s.status_jobs(jobs, include_all=True, now=now), jobs)

    def test_local_status_filters_history_and_all_restores_it_without_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            q = s.Queue(Path(directory))
            now = time.time()
            jobs = [dict(id=uuid.uuid4().hex, status=status, created=now - 300000,
                         finished=now - age, command='echo test', name=name)
                    for name, status, age in [('old-result', 'succeeded', 172801),
                                               ('recent-result', 'succeeded', 1),
                                               ('old-failure', 'failed', 172801)]]
            q.add_many(jobs)
            argv = ['scheduler.py', '--state-dir', directory, 'status', '--local']
            with patch.object(sys, 'argv', argv), patch.object(s, 'remote') as remote, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                s.main()
            remote.assert_not_called()
            self.assertNotIn('old-result', output.getvalue())
            self.assertIn('recent-result', output.getvalue())
            with patch.object(sys, 'argv', argv + ['--all']), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                s.main()
            self.assertTrue(all(j['name'] in output.getvalue() for j in jobs))
            self.assertEqual(len(q.all()), 3)


class SSHConnectionLimitTest(unittest.TestCase):
    CHILD = '''
import sys
from pathlib import Path
import scheduler as s
s.configure_ssh({'hosts': [{'ssh': sys.argv[2]}], 'max_ssh_connections': int(sys.argv[3])}, Path(sys.argv[1]))
print('waiting', flush=True)
sys.stdin.readline()
with s.ssh_connection_slot(sys.argv[2]):
    print('acquired', flush=True)
    sys.stdin.readline()
'''

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('SSH_LOCK_DIRECTORY', self.root / 'ssh-connections'),
                            ('SSH_CONNECTION_LIMITS', {})]:
            patcher = patch.object(s, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def child(self, host='a', limit=1):
        process = subprocess.Popen([sys.executable, '-u', '-c', self.CHILD,
                                    str(self.root), host, str(limit)], cwd=s.HERE,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        self.assert_line(process, 'waiting')
        process.stdin.write('start\n')
        process.stdin.flush()
        return process

    def assert_line(self, process, expected):
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], expected)
        self.assertEqual(process.stdout.readline().strip(), expected)

    def test_limits_are_shared_across_processes_and_allow_configured_slot_count(self):
        from contextlib import ExitStack
        for limit in (1, 2):
            with self.subTest(limit=limit):
                s.SSH_CONNECTION_LIMITS['a'] = limit
                with ExitStack() as stack:
                    for _ in range(limit):
                        stack.enter_context(s.ssh_connection_slot('a'))
                    process = self.child(limit=limit)
                    self.assertFalse(select.select([process.stdout], [], [], 0.2)[0])
                self.assert_line(process, 'acquired')
                process.stdin.write('\n')
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=5), 0)

    def test_different_hosts_do_not_block_each_other(self):
        with s.ssh_connection_slot('a'):
            self.assert_line(self.child(host='b'), 'acquired')

    def test_ssh_timeout_releases_the_slot(self):
        with patch.object(s.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            with self.assertRaises(RuntimeError):
                s.remote('a', {'action': 'status'})
        self.assert_line(self.child(), 'acquired')

    def test_host_override_and_invalid_limits(self):
        cfg = fixture_configuration()
        cfg['max_ssh_connections'] = 2
        cfg['hosts'][0]['max_ssh_connections'] = 1
        s.configure_ssh(cfg, self.root)
        self.assertEqual(s.SSH_CONNECTION_LIMITS[cfg['hosts'][0]['ssh']], 1)
        self.assertEqual(s.SSH_CONNECTION_LIMITS[cfg['hosts'][1]['ssh']], 2)
        path = self.root / 'hosts.json'
        for value in (0, -1, True, 1.5):
            cfg['hosts'][0]['max_ssh_connections'] = value
            path.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, 'max_ssh_connections'):
                s.configuration(path)


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.q = s.Queue(Path(self.temp.name))
        transport_path = patch.object(s, 'SSH_LOCK_DIRECTORY', Path(self.temp.name) / 'ssh')
        transport_path.start()
        self.addCleanup(transport_path.stop)
        transport_limits = patch.object(s, 'SSH_CONNECTION_LIMITS', {})
        transport_limits.start()
        self.addCleanup(transport_limits.stop)
        self.cfg = fixture_configuration()
        # Fixtures use these aliases independently of the user's enabled hosts.
        self.cfg['hosts'] = [{'ssh': host} for host in
                             ('glockenspiel', 'marimba', 'xylophone0', 'xylophone1', 'xylophone3')]
        self.cfg.pop('shared_queue', None)
        self.cfg['max_jobs_per_gpu'] = 1
        self.cfg['gpu_memory_headroom_mib'] = 0

    def job(self):
        job = dict(id=uuid.uuid4().hex, created=time.time(), status='queued',
                   command='python train.py', min_free_mib=2048, requested_host=None)
        self.q.put(job)
        return job

    def gpu(self, name='GTX 1080 Ti', host='xylophone0', **overrides):
        result = dict(name=name, host=host, uuid='GPU-' + host, index=0,
                      priority=s.priority(self.cfg, name), idle=True,
                      total_mib=11000, used_mib=0, utilization=0)
        result.update(overrides)
        return result

    def test_prefers_fastest_available_gpu_and_respects_memory(self):
        slow = self.gpu()
        fast = self.gpu('RTX 5000 Ada', 'glockenspiel', total_mib=32000)
        inv = {'xylophone0': {'gpus': [slow]}, 'glockenspiel': {'gpus': [fast]}}
        job = self.job()
        self.assertEqual(s.candidates(self.cfg, inv, job)[0]['host'], 'glockenspiel')
        fast['idle'] = False
        self.assertEqual(s.candidates(self.cfg, inv, job)[0]['host'], 'xylophone0')
        job['min_free_mib'] = 12000
        self.assertEqual(s.candidates(self.cfg, inv, job), [])

    def test_host_limit_is_shared_across_gpus_and_projects(self):
        cfg = dict(self.cfg, max_jobs_per_host=2,
                   hosts=[{'ssh': 'marimba'}, {'ssh': 'xylophone0'}])
        jobs = [s.new_job(cfg, dict(command='echo job', project=project))
                for project in ('V2VSimulator', 'V2VSimulator-division') * 3]
        self.q.add_many(jobs)
        inv = {host: {'gpus': [self.gpu(host=host, uuid=f'GPU-{host}-{i}', index=i)
                              for i in range(3)]}
               for host in ('marimba', 'xylophone0')}
        with patch.object(s, 'inventory', side_effect=lambda cfg: copy.deepcopy(inv)), \
             patch.object(s, 'remote', return_value={'status': 'running'}) as remote, \
             contextlib.redirect_stdout(io.StringIO()):
            s.dispatch(cfg, self.q)
        current = self.q.all()
        for host in inv:
            self.assertEqual(sum(j['status'] == 'running' and j.get('host') == host for j in current), 2)
        self.assertEqual(sum(j['status'] == 'queued' for j in current), 2)
        self.assertEqual(remote.call_count, 4)

    def test_host_limit_respects_remote_reservations_and_local_uncertain_jobs(self):
        cfg = dict(self.cfg, max_jobs_per_host=2)
        job = self.job()
        inv = {'xylophone0': {'gpus': [self.gpu()], 'host_occupied_jobs': 2}}
        self.assertEqual(s.candidates(cfg, inv, job), [])
        inv['xylophone0']['host_occupied_jobs'] = 0
        self.assertEqual(s.candidates(cfg, inv, job, {'xylophone0': 3}), [])
        self.assertEqual(len(s.candidates(cfg, inv, job, {'xylophone0': 1})), 1)

    def test_host_limit_validation_and_forwarding(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['max_jobs_per_host'] = 2
        cfg['hosts'][0]['max_jobs_per_host'] = 1
        self.assertEqual(s.host_request(cfg, cfg['hosts'][0]['ssh'])['max_jobs_per_host'], 1)
        self.assertEqual(s.host_request(cfg, cfg['hosts'][1]['ssh'])['max_jobs_per_host'], 2)
        path = Path(self.temp.name) / 'hosts.json'
        for value in (0, -1, True, 1.5):
            cfg['hosts'][0]['max_jobs_per_host'] = value
            path.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, 'max_jobs_per_host'):
                s.configuration(path)

    def test_host_restriction(self):
        job = self.job()
        job['requested_host'] = 'marimba'
        inv = {'xylophone0': {'gpus': [self.gpu()]}}
        self.assertEqual(s.candidates(self.cfg, inv, job), [])

    def test_host_gpu_exclusion_applies_to_saved_jobs_and_remote_requests(self):
        entry = next(h for h in self.cfg['hosts'] if h['ssh'] == 'marimba')
        entry['excluded_gpu_uuids'] = ['GPU-p100']
        slow = self.gpu('Tesla P100', 'marimba', uuid='GPU-p100')
        fast = self.gpu('Quadro RTX 6000', 'marimba', uuid='GPU-rtx')
        job = s.new_job(self.cfg, dict(command='echo test'))
        inv = {'marimba': {'gpus': [slow, fast]}}
        self.assertEqual(s.candidates(self.cfg, inv, job), [fast])
        saved_cfg = s.job_configuration(self.cfg, job)
        self.assertEqual(s.host_request(saved_cfg, 'marimba')['excluded_gpu_uuids'], ['GPU-p100'])
        self.assertEqual(s.host_request(saved_cfg, 'xylophone0')['excluded_gpu_uuids'], [])

    def test_gpu_exclusion_configuration_rejects_malformed_values(self):
        path = Path(self.temp.name) / 'hosts.json'
        for invalid in ('GPU-p100', [0], ['']):
            cfg = copy.deepcopy(self.cfg)
            cfg['hosts'][0]['excluded_gpu_uuids'] = invalid
            path.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, 'excluded_gpu_uuids'):
                s.configuration(path)

    def test_lost_launch_reply_retries_same_id_and_target(self):
        job = self.job()
        inv = {'xylophone0': {'gpus': [self.gpu()]}}
        with patch.object(s, 'inventory', return_value=inv), \
             patch.object(s, 'remote', side_effect=RuntimeError('lost reply')), \
             contextlib.redirect_stdout(io.StringIO()):
            s.dispatch(self.cfg, self.q)
        before = self.q.get(job['id'])
        self.assertEqual(before['status'], 'dispatching')
        with patch.object(s, 'remote', return_value={'status': 'running'}) as call:
            s.reconcile(self.cfg, self.q)
        self.assertEqual(call.call_args.args[1]['id'], job['id'])
        self.assertEqual(call.call_args.args[1]['gpu'], before['gpu'])
        self.assertEqual(self.q.get(job['id'])['status'], 'running')

    def test_gpu_not_double_assigned_in_one_pass(self):
        a, b = self.job(), self.job()
        inv = {'xylophone0': {'gpus': [self.gpu()]}}
        with patch.object(s, 'inventory', return_value=inv), \
             patch.object(s, 'remote', return_value={'status': 'running'}), \
             contextlib.redirect_stdout(io.StringIO()):
            s.dispatch(self.cfg, self.q)
        self.assertEqual(self.q.get(a['id'])['status'], 'running')
        self.assertEqual(self.q.get(b['id'])['status'], 'queued')

    def test_status_retains_failure_exit_code(self):
        job = self.job()
        job.update(status='running', host='xylophone0', gpu='GPU-a')
        self.q.put(job)
        with patch.object(s, 'remote', return_value={'status': 'failed', 'exit_code': 7}):
            s.refresh_status(self.q)
        self.assertEqual(self.q.get(job['id'])['exit_code'], 7)

    def test_connection_failure_is_not_job_failure(self):
        job = self.job()
        job.update(status='running', host='xylophone0')
        self.q.put(job)
        with patch.object(s, 'remote', side_effect=RuntimeError('offline')):
            s.refresh_status(self.q)
        result = self.q.get(job['id'])
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['connection_error'], 'offline')

    def test_status_batches_jobs_and_checks_hosts_in_parallel(self):
        jobs = [self.job() for _ in range(4)]
        for job, host in zip(jobs, ('a', 'a', 'b', 'b')):
            job.update(status='running', host=host)
            self.q.put(job)
        jobs[0]['cancel_requested'] = True
        self.q.put(jobs[0])
        barrier = threading.Barrier(2)

        def remote(host, request):
            barrier.wait(timeout=5)
            self.assertEqual(request['action'], 'status_batch')
            return {'jobs': [dict(status='cancelled' if r['action'] == 'cancel'
                                 else 'succeeded', exit_code=0)
                             for r in request['requests']]}

        with patch.object(s, 'remote', side_effect=remote) as connect:
            s.refresh_status(self.q)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(self.q.get(jobs[0]['id'])['status'], 'cancelled')
        for job in jobs[1:]:
            self.assertEqual(self.q.get(job['id'])['status'], 'succeeded')

    def test_status_batch_errors_and_missing_do_not_damage_other_jobs(self):
        jobs = [self.job() for _ in range(3)]
        for job in jobs:
            job.update(status='dispatching', host='a')
            self.q.put(job)
        results = [{'error': 'offline'}, {'status': 'missing'},
                   {'status': 'failed', 'exit_code': 7}]
        with patch.object(s, 'remote', return_value={'jobs': results}):
            s.refresh_status(self.q)
        self.assertEqual(self.q.get(jobs[0]['id'])['connection_error'], 'offline')
        self.assertEqual(self.q.get(jobs[1]['id'])['status'], 'dispatching')
        self.assertEqual(self.q.get(jobs[2]['id'])['exit_code'], 7)

    def test_remote_status_batch_isolates_errors_and_retries_cancellation(self):
        import remote as r

        def read(job_id):
            if job_id == 'broken':
                raise OSError('unreadable record')
            return dict(id=job_id, status='running')

        requests = [dict(action='status', id='broken'),
                    dict(action='cancel', id='cancelled', allow_missing=True),
                    dict(action='status', id='healthy')]
        with patch.object(r, 'root_dir', return_value=Path(self.temp.name)), \
             patch.object(r, 'read_job', side_effect=read), \
             patch.object(r, 'cancel', return_value={'status': 'cancelled'}) as cancel:
            result = r.main(dict(action='status_batch', requests=requests))
        self.assertEqual(result['jobs'][0], {'error': 'unreadable record'})
        self.assertEqual(result['jobs'][1]['status'], 'cancelled')
        self.assertEqual(result['jobs'][2]['id'], 'healthy')
        cancel.assert_called_once_with('cancelled', True)

    def test_shared_refresh_saves_changes_in_one_transaction_and_skips_unchanged(self):
        jobs = [dict(self.job(), published=True) for _ in range(3)]
        cfg = dict(self.cfg, shared_queue={'host': 'a'})
        statements = []
        self.q.db.set_trace_callback(statements.append)
        with patch.object(s, 'shared_rpc', return_value={'jobs': jobs}):
            s.refresh_shared(cfg, self.q)
            self.assertEqual(statements.count('COMMIT'), 1)
            statements.clear()
            s.refresh_shared(cfg, self.q)
        self.assertFalse(any(statement.startswith('INSERT') for statement in statements))

    def test_status_reader_cannot_roll_back_newer_state(self):
        job = self.job()
        stale = copy.deepcopy(job)
        job.update(status='succeeded', exit_code=0, host='glockenspiel')
        self.q.put(job)
        self.q.update_if_unchanged(stale, dict(stale, status='running'))
        self.assertEqual(self.q.get(job['id'])['status'], 'succeeded')

    def test_transport_disables_persistent_ssh_and_preserves_command(self):
        command = "printf '%s\\n' 'spaces; $HOME `literal` 日本語'"
        response = subprocess.CompletedProcess([], 0, '{"status":"running"}', '')
        with patch.object(s.subprocess, 'run', return_value=response) as call:
            s.remote('xylophone0', {'action': 'launch', 'command': command})
        argv = call.call_args.args[0]
        self.assertIn('ControlMaster=no', argv)
        self.assertIn('ControlPersist=no', argv)
        self.assertIn('ControlPath=none', argv)
        self.assertEqual(json.loads(call.call_args.kwargs['input'])['command'], command)

    def test_template_start_script_is_forwarded_and_status_timeout_stays_short(self):
        cfg = dict(self.cfg, template_start_script='~/custom/start.sh')
        self.assertEqual(s.host_request(cfg, cfg['hosts'][0]['ssh'])['template_start_script'],
                         '~/custom/start.sh')
        response = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.object(s.subprocess, 'run', return_value=response) as call:
            s.remote('a', {'action': 'probe'})
            self.assertEqual(call.call_args.kwargs['timeout'], 1900)
            s.remote('a', {'action': 'status'})
            self.assertEqual(call.call_args.kwargs['timeout'], 100)

    def test_template_inspection_error_does_not_trigger_creation(self):
        import remote as r
        with patch.object(r, 'run', return_value=subprocess.CompletedProcess(
                [], 1, '', 'permission denied')) as call:
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                r.ensure_template({'container': 'test'})
        call.assert_called_once()

    def test_template_setup_failure_is_reported(self):
        import remote as r
        script = Path(self.temp.name) / 'setup.sh'
        script.touch()
        with patch.object(r, 'template_running', return_value=None), \
             patch.object(r, 'run', return_value=subprocess.CompletedProcess(
                 [], 1, 'setup output', 'setup failed')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot create template container'):
                r.ensure_template(dict(container='test', template_start_script=str(script)))

    def test_stopped_template_regenerates_device_mappings_with_configured_script(self):
        import remote as r
        script = Path(self.temp.name) / 'setup.sh'
        script.touch()
        with patch.object(r, 'template_running', side_effect=[False, True]), \
             patch.object(r, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertFalse(r.ensure_template(dict(container='test', template_start_script=str(script))))
        self.assertEqual(run.call_args.args[0], ['bash', str(script), 'start'])

    def test_open_kernel_module_version_selects_matching_driver_directory(self):
        import remote as r
        with patch.object(r.Path, 'exists', return_value=True), \
             patch.object(r.Path, 'read_text', return_value=(
                 'NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  615.71.10  Release Build')):
            self.assertIn('nvidia-driver-615.71.10/lib', r.driver_env())

    def test_local_status_and_queue_only_never_connect(self):
        for argv in (['status', '--local'], ['submit', '--queue-only', 'echo safe']):
            with patch.object(sys, 'argv', ['scheduler.py', '--state-dir',
                                           self.temp.name] + argv), \
                 patch.object(s, 'remote') as connect, \
                 contextlib.redirect_stdout(io.StringIO()):
                s.main()
                connect.assert_not_called()

    def test_completed_jobs_sync_in_one_transfer_and_do_not_repeat(self):
        for state in ('succeeded', 'failed'):
            job = self.job()
            job.update(status=state, host='glockenspiel')
            self.q.put(job)
        cfg = dict(self.cfg, results_sync=dict(
            source='glockenspiel:~/workspace/V2VSimulator/Simulator/save_results/',
            destination=str(Path(self.temp.name) / 'results')))
        success = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(s.subprocess, 'run', return_value=success) as transfer, \
             contextlib.redirect_stdout(io.StringIO()):
            s.sync_completed(cfg, self.q)
            s.sync_completed(cfg, self.q)
        transfer.assert_called_once()
        argv = transfer.call_args.args[0]
        self.assertEqual(argv[:2], ['rsync', '-az'])
        self.assertEqual(argv[-2], cfg['results_sync']['source'])
        self.assertNotIn('--delete', argv)
        self.assertIn('ControlPersist=no', argv[4])
        self.assertTrue(all(j['result_sync']['status'] == 'succeeded' for j in self.q.all()))

    def test_failed_sync_retries_without_changing_job_result(self):
        job = self.job()
        job.update(status='succeeded', exit_code=0, host='glockenspiel')
        self.q.put(job)
        cfg = dict(self.cfg, results_sync=dict(source='glockenspiel:results/',
                   destination=str(Path(self.temp.name) / 'results')))
        responses = [subprocess.CompletedProcess([], 23, '', 'transfer failed'),
                     subprocess.CompletedProcess([], 0, '', '')]
        with patch.object(s.subprocess, 'run', side_effect=responses) as transfer, \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            s.sync_completed(cfg, self.q)
            self.assertEqual(self.q.get(job['id'])['result_sync']['status'], 'failed')
            self.assertEqual(self.q.get(job['id'])['status'], 'succeeded')
            s.sync_completed(cfg, self.q)
        self.assertEqual(transfer.call_count, 2)
        self.assertEqual(self.q.get(job['id'])['result_sync']['status'], 'succeeded')

    def test_running_jobs_do_not_trigger_sync(self):
        job = self.job()
        job.update(status='running', host='glockenspiel')
        self.q.put(job)
        with patch.object(s.subprocess, 'run') as transfer:
            s.sync_completed(self.cfg, self.q)
        transfer.assert_not_called()

    def test_disabled_sync_never_transfers_completed_results(self):
        job = self.job()
        job.update(status='succeeded', host='glockenspiel', exit_code=0)
        self.q.put(job)
        cfg = dict(self.cfg, results_sync=None)
        with patch.object(s.subprocess, 'run') as transfer:
            s.sync_completed(cfg, self.q)
        transfer.assert_not_called()
        self.assertNotIn('result_sync', self.q.get(job['id']))

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_real_rsync_preserves_local_only_results(self):
        source = Path(self.temp.name) / 'source'
        destination = Path(self.temp.name) / 'destination'
        source.mkdir()
        destination.mkdir()
        (source / 'new-result.txt').write_text('new result')
        (destination / 'local-only.txt').write_text('preserve me')
        job = self.job()
        job.update(status='succeeded', host='glockenspiel')
        self.q.put(job)
        cfg = dict(self.cfg, results_sync=dict(source=str(source) + '/',
                                              destination=str(destination)))
        with contextlib.redirect_stdout(io.StringIO()):
            s.sync_completed(cfg, self.q)
        self.assertEqual((destination / 'new-result.txt').read_text(), 'new result')
        self.assertEqual((destination / 'local-only.txt').read_text(), 'preserve me')
        self.assertEqual(self.q.get(job['id'])['result_sync']['status'], 'succeeded')

    def test_batch_matrix_and_unrelated_shell_braces(self):
        path = Path(self.temp.name) / 'jobs.json'
        path.write_text(json.dumps(dict(defaults=dict(min_free_gb=4), jobs=[dict(
            command='echo ${UNCHANGED} seed={seed} vehicles={vehicles}',
            name='s{seed}-v{vehicles}', matrix=dict(seed=[1, 2], vehicles=[120, 240]))])))
        jobs = s.batch_jobs(self.cfg, path)
        self.assertEqual(len(jobs), 4)
        self.assertEqual(len({j['batch_id'] for j in jobs}), 1)
        self.assertEqual(jobs[-1]['command'], 'echo ${UNCHANGED} seed=2 vehicles=240')
        self.assertEqual(jobs[-1]['min_free_mib'], 4096)

    def test_invalid_batch_does_not_register_any_jobs(self):
        path = Path(self.temp.name) / 'invalid.json'
        path.write_text(json.dumps([dict(command='echo valid'), dict(command='')]))
        with self.assertRaises(ValueError):
            self.q.add_many(s.batch_jobs(self.cfg, path))
        self.assertEqual(self.q.all(), [])

    def test_project_environment_survives_later_configuration_changes(self):
        job = s.new_job(self.cfg, dict(command='echo worktree', project='V2VSimulator-division'))
        job.update(host='marimba', gpu='GPU-a')
        cfg = copy.deepcopy(self.cfg)
        cfg['projects'].clear()
        cfg['container'] = 'different-template'
        cfg['runtime_image'] = 'different-runtime'
        cfg['hosts'][1]['container'] = 'host-override'
        request = s.launch_request(cfg, job)
        self.assertEqual(request['container'], 'v2vsimulator-division-v2v-dev-1')
        self.assertEqual(request['template_start_script'], '~/workspace/V2VSimulator-division/docker-shell.sh')
        self.assertEqual(request['workdir'], '/workspace/V2VSimulator/Simulator')
        self.assertNotIn('runtime_image', request)

    def test_project_defaults_and_unknown_project(self):
        job = s.new_job(self.cfg, dict(command='echo original'))
        self.assertEqual(job['project'], 'V2VSimulator')
        self.assertEqual(job['execution']['marimba']['container'], 'v2vsimulator-v2v-dev-1')
        for project in ('unknown', '', ['V2VSimulator-division']):
            with self.subTest(project=project), self.assertRaises(ValueError):
                s.new_job(self.cfg, dict(command='echo invalid', project=project))

    def test_batch_project_defaults_and_cli_override(self):
        path = Path(self.temp.name) / 'projects.json'
        path.write_text(json.dumps(dict(defaults=dict(project='V2VSimulator-division'), jobs=[
            dict(command='echo worktree'), dict(command='echo original', project='V2VSimulator')])))
        self.assertEqual([j['project'] for j in s.batch_jobs(self.cfg, path)],
                         ['V2VSimulator-division', 'V2VSimulator'])
        self.assertEqual([j['project'] for j in s.batch_jobs(self.cfg, path, 'V2VSimulator-division')],
                         ['V2VSimulator-division', 'V2VSimulator-division'])

    def test_project_submit_queue_only_records_environment_without_ssh(self):
        argv = ['scheduler.py', '--state-dir', self.temp.name, 'submit',
                '--project', 'V2VSimulator-division', '--queue-only', 'echo worktree']
        with patch.object(sys, 'argv', argv), patch.object(s, 'remote') as connect, \
             patch.object(s, 'configuration', return_value=self.cfg), \
             contextlib.redirect_stdout(io.StringIO()):
            s.main()
        connect.assert_not_called()
        self.assertEqual(self.q.all()[0]['project'], 'V2VSimulator-division')

    def test_dispatch_mixed_projects_keeps_environment_and_gpu_accounting(self):
        cfg = dict(self.cfg, hosts=[{'ssh': 'marimba'}], max_jobs_per_gpu=2)
        jobs = [s.new_job(cfg, dict(command='echo '+project, project=project))
                for project in ('V2VSimulator-division', 'V2VSimulator', 'V2VSimulator-division')]
        self.q.add_many(jobs)
        launched = []
        probes = []

        def reply(host, request):
            if request['action'] == 'launch':
                launched.append(request['container'])
                return {'status': 'reserved'}
            probes.append(request['container'])
            return {'gpus': [self.gpu(host=host, accepting_jobs=len(launched) < 2,
                                     available_mib=10000, occupied_slots=len(launched))]}

        with patch.object(s, 'remote', side_effect=reply), contextlib.redirect_stdout(io.StringIO()):
            s.dispatch(cfg, self.q)
        self.assertEqual(launched, ['v2vsimulator-division-v2v-dev-1', 'v2vsimulator-v2v-dev-1'])
        self.assertIn('v2vsimulator-division-v2v-dev-1', probes)
        self.assertIn('v2vsimulator-v2v-dev-1', probes)
        self.assertEqual(self.q.get(jobs[-1]['id'])['status'], 'queued')

    def test_project_gpus_selects_template(self):
        argv = ['scheduler.py', '--state-dir', self.temp.name, 'gpus', '--project', 'V2VSimulator-division']
        with patch.object(sys, 'argv', argv), patch.object(s, 'inventory', return_value={}) as probe, \
             patch.object(s, 'configuration', return_value=self.cfg):
            s.main()
        cfg = probe.call_args.args[0]
        self.assertEqual(s.host_request(cfg, 'marimba')['container'], 'v2vsimulator-division-v2v-dev-1')

    def test_batch_dry_run_never_submits_or_connects(self):
        path = Path(self.temp.name) / 'jobs.json'
        path.write_text('[{"command": "echo preview"}]')
        with patch.object(sys, 'argv', ['scheduler.py', '--state-dir', self.temp.name,
                                      'submit-batch', str(path), '--dry-run']), \
             patch.object(s, 'remote') as connect, contextlib.redirect_stdout(io.StringIO()):
            s.main()
        connect.assert_not_called()
        self.assertEqual(self.q.all(), [])

    def test_cancel_queued_batch_has_no_connections_or_result_transfer(self):
        a, b = self.job(), self.job()
        for j in (a, b):
            j['batch_id'] = 'abc12345'
            self.q.put(j)
        with patch.object(s, 'remote') as connect, patch.object(s.subprocess, 'run') as transfer, \
             contextlib.redirect_stdout(io.StringIO()):
            s.cancel_jobs(self.cfg, self.q, [], batch='abc')
            s.sync_completed(self.cfg, self.q)
        connect.assert_not_called()
        transfer.assert_not_called()
        self.assertTrue(all(j['status'] == 'cancelled' for j in self.q.all()))

    def test_cancel_lost_reply_keeps_intent_and_status_retries(self):
        job = self.job()
        job.update(status='running', host='xylophone0')
        self.q.put(job)
        with patch.object(s, 'remote', side_effect=RuntimeError('lost reply')), \
             contextlib.redirect_stdout(io.StringIO()):
            s.cancel_jobs(self.cfg, self.q, [job['id'][:8]])
        self.assertTrue(self.q.get(job['id'])['cancel_requested'])
        with patch.object(s, 'remote', return_value={'status': 'cancelled'}) as connect:
            s.refresh_status(self.q)
        self.assertEqual(connect.call_args.args[1]['action'], 'cancel')
        self.assertEqual(self.q.get(job['id'])['status'], 'cancelled')

    def test_cancel_all_cancels_every_unfinished_state_and_preserves_history(self):
        active = []
        for state in ('queued', 'dispatching', 'reserved', 'running', 'cancelling', 'unknown'):
            job = self.job()
            job.update(status=state, batch_id=uuid.uuid4().hex)
            if state != 'queued':
                job['host'] = 'xylophone0'
            self.q.put(job)
            active.append(job)
        terminal = []
        for state in sorted(s.TERMINAL):
            job = self.job()
            job.update(status=state, finished=123)
            self.q.put(job)
            terminal.append(job)
        with patch.object(s, 'remote', return_value={'status': 'cancelled'}) as connect, \
             contextlib.redirect_stdout(io.StringIO()):
            s.cancel_jobs(self.cfg, self.q, [], all_jobs=True)
        self.assertTrue(all(self.q.get(j['id'])['status'] == 'cancelled' for j in active))
        self.assertEqual(connect.call_count, 5)
        self.assertTrue(all(call.args[1]['action'] == 'cancel' for call in connect.call_args_list))
        for job in terminal:
            self.assertEqual(self.q.get(job['id']), job)

    def test_cancel_all_cli_with_no_pending_jobs_is_successful(self):
        with patch.object(sys, 'argv', ['scheduler.py', '--state-dir', self.temp.name,
                                      'cancel', '--all']), \
             patch.object(s, 'configuration', return_value=self.cfg), \
             patch.object(s, 'remote') as connect, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(s.main(), 0)
        connect.assert_not_called()
        self.assertIn('No jobs to cancel.', output.getvalue())

    def test_cancel_all_rejects_other_selectors_before_connecting(self):
        for selectors in (['missing-job'], ['--batch', 'missing-batch']):
            with patch.object(sys, 'argv', ['scheduler.py', '--state-dir', self.temp.name,
                                          'cancel', '--all'] + selectors), \
                 patch.object(s, 'remote') as connect:
                with self.assertRaisesRegex(ValueError, '--all cannot be combined'):
                    s.main()
            connect.assert_not_called()

    def test_elapsed_uses_finished_time_and_does_not_count_queue_wait(self):
        self.assertEqual(s.elapsed(dict(created=0), now=1000), '-')
        self.assertEqual(s.elapsed(dict(started=100), now=3761), '01:01:01')
        self.assertEqual(s.elapsed(dict(started=100, finished=160), now=5000), '00:01:00')

    def test_status_hides_failed_and_cancelled_and_sorts_without_changing_history(self):
        states = ['queued', 'cancelled', 'running', 'failed', 'succeeded', 'queued']
        jobs = []
        for i, state in enumerate(states):
            job = self.job()
            job.update(status=state, created=i)
            self.q.put(job)
            jobs.append(job)
        for extra, expected in (([], [4, 2, 0, 5]),
                                (['--all'], [4, 3, 2, 0, 5, 1])):
            terminal = io.StringIO()
            argv = ['scheduler.py', '--state-dir', str(self.q.directory),
                    'status', '--local', *extra]
            with patch.object(sys, 'argv', argv), patch.object(s, 'remote') as connect, \
                 contextlib.redirect_stdout(terminal):
                s.main()
            connect.assert_not_called()
            ids = [line.split()[0] for line in terminal.getvalue().splitlines()[1:]]
            self.assertEqual(ids, [jobs[i]['id'][:8] for i in expected])
        self.assertEqual(len(self.q.all()), len(jobs))

    def test_job_agents_uses_training_script_or_agent_arguments(self):
        cases = [
            ('python3 train_lstm.py --method 4', 'LSTM'),
            ('python3 /workspace/train_transformer.py --method 4', 'Transformer'),
            ('python3 evaluate_methods.py --agents LSTM --method 4', 'LSTM'),
            ('python3 evaluate_methods.py --agents Transformer --analysis-only', 'Transformer'),
            ('python3 plot_evaluation.py --agents SPS IPMI LSTM --method 4', 'SPS+IPMI+LSTM'),
            ('python3 plot_evaluation.py --agents SPS IPMI Transformer', 'SPS+IPMI+Transformer'),
            ('python3 evaluate_methods.py --agents=LSTM --method 4', 'LSTM'),
            ('echo unrelated', '-'),
            ('python3 "unterminated', '-'),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(s.job_agents({'command': command}), expected)

    def test_status_identifies_existing_jobs_with_the_same_name(self):
        jobs = []
        for agent in ('LSTM', 'Transformer'):
            job = self.job()
            job.update(name='eval_highway_4_10_120', status='running',
                       command=f'python3 evaluate_methods.py --agents {agent} --method 4')
            self.q.put(job)
            jobs.append(job)
        terminal = io.StringIO()
        argv = ['scheduler.py', '--state-dir', str(self.q.directory), 'status', '--local']
        with patch.object(sys, 'argv', argv), patch.object(s, 'remote') as connect, \
             contextlib.redirect_stdout(terminal):
            s.main()
        connect.assert_not_called()
        lines = terminal.getvalue().splitlines()
        self.assertIn('AGENTS', lines[0])
        for job, agent in zip(jobs, ('LSTM', 'Transformer')):
            row = next(line for line in lines[1:] if line.startswith(job['id'][:8]))
            self.assertIn(agent, row)
            self.assertIn(job['name'], row)
            self.assertEqual(self.q.get(job['id']), job)

    def test_running_logs_groups_requests_by_host_and_saves_files(self):
        jobs = [self.job() for _ in range(3)]
        for job, host in zip(jobs, ('glockenspiel', 'glockenspiel', 'xylophone0')):
            job.update(status='running', host=host)
            self.q.put(job)
        jobs[0]['status'] = 'unknown'
        self.q.put(jobs[0])
        def reply(host, request):
            return {'jobs': [dict(id=key, status='running', log='log-' + key)
                             for key in request['ids']]}
        output = Path(self.temp.name) / 'logs'
        terminal = io.StringIO()
        with patch.object(s, 'remote', side_effect=reply) as connect, \
             contextlib.redirect_stdout(terminal):
            s.running_logs(self.cfg, self.q, 200, output)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(len(list(output.glob('*.log'))), 3)
        self.assertTrue(all(c.args[1]['lines'] == 200 for c in connect.call_args_list))
        for job in jobs:
            self.assertEqual((output / s.log_filename(job)).read_text(), 'log-' + job['id'] + '\n')
            self.assertNotIn('log-' + job['id'], terminal.getvalue())
        self.assertEqual(terminal.getvalue().count('Saved:'), 3)

    def test_logs_default_to_files_for_running_and_single_jobs(self):
        for selection in (['--running'], ['1234abcd']):
            args = s.parser().parse_args(['logs', *selection, '--lines', '200'])
            self.assertEqual(args.output_dir, Path('logs/current'))
            self.assertEqual(args.lines, 200)
        args = s.parser().parse_args(['logs', '1234abcd', '--output-dir', 'logs/custom'])
        self.assertEqual(args.output_dir, Path('logs/custom'))

    def test_log_files_use_job_names_and_keep_duplicate_names_distinct(self):
        jobs = [self.job(), self.job()]
        for job in jobs:
            job.update(name='highway_4_30', status='running', host='glockenspiel')
            self.q.put(job)
        def reply(host, req):
            return {'jobs': [dict(id=key, status='running', log='training') for key in req['ids']]}
        output = Path(self.temp.name) / 'logs'
        with patch.object(s, 'remote', side_effect=reply), contextlib.redirect_stdout(io.StringIO()):
            s.running_logs(self.cfg, self.q, 200, output)
        self.assertEqual({p.name for p in output.iterdir()},
                         {f'highway_4_30_{job["id"][:8]}.log' for job in jobs})

    def test_log_filename_handles_paths_and_long_unicode_names(self):
        job = self.job()
        job['name'] = '../folder/highway:4/30'
        name = s.log_filename(job)
        self.assertNotIn('/', name)
        self.assertFalse(name.startswith('.'))
        job['name'] = '学習' * 200
        self.assertLess(len(s.log_filename(job).encode('utf-8')), 255)

    def test_shared_import_preserves_each_pcs_sync_state(self):
        cfg = dict(self.cfg, shared_queue={'host': 'glockenspiel'})
        job = self.job()
        job.update(status='succeeded', host='glockenspiel', result_sync={'status': 'succeeded'})
        self.q.put(job)
        published = {k: v for k, v in job.items() if k != 'result_sync'}
        other = s.Queue(Path(self.temp.name) / 'other-pc')
        with patch.object(s, 'remote', return_value={'jobs': [published]}):
            s.refresh_shared(cfg, self.q)
            s.refresh_shared(cfg, other)
        self.assertEqual(self.q.get(job['id'])['result_sync']['status'], 'succeeded')
        self.assertNotIn('result_sync', other.get(job['id']))

    def test_export_is_git_friendly_and_does_not_connect_by_default(self):
        job = self.job()
        output = Path(self.temp.name) / 'jobs/history.json'
        with patch.object(sys, 'argv', ['scheduler.py', '--state-dir', self.temp.name,
                                      'export', '--output', str(output)]), \
             patch.object(s, 'remote') as connect, contextlib.redirect_stdout(io.StringIO()):
            s.main()
        connect.assert_not_called()
        saved = json.loads(output.read_text())
        self.assertEqual(saved['schema_version'], 1)
        self.assertEqual(saved['jobs'][0]['id'], job['id'])

    def test_stale_shared_reply_cannot_resurrect_cancelled_job(self):
        cfg = dict(self.cfg, shared_queue={'host': 'glockenspiel'})
        job = self.job()
        stale = dict(job)
        job['status'] = 'cancelled'
        self.q.put(job)
        with patch.object(s, 'remote', return_value={'jobs': [stale]}):
            s.refresh_shared(cfg, self.q)
        self.assertEqual(self.q.get(job['id'])['status'], 'cancelled')

    def test_sharing_dispatch_refreshes_slots_and_stops_at_two(self):
        jobs = [self.job() for _ in range(3)]
        cfg = dict(self.cfg, max_jobs_per_gpu=2)
        gpu = self.gpu(accepting_jobs=True, occupied_slots=0, max_jobs=2,
                       available_mib=10000)
        inv = {'xylophone0': {'gpus': [gpu]}}
        launches = []
        def reply(host, req):
            if req['action'] == 'launch':
                launches.append(req['id'])
                return {'status': 'reserved'}
            count = len(launches)
            return {'gpus': [dict(gpu, occupied_slots=count, accepting_jobs=count < 2,
                                  available_mib=10000 - count * 2048)]}
        with patch.object(s, 'inventory', return_value=inv), \
             patch.object(s, 'remote', side_effect=reply), contextlib.redirect_stdout(io.StringIO()):
            s.dispatch(cfg, self.q)
        self.assertEqual(len(launches), 2)
        self.assertEqual(self.q.get(jobs[-1]['id'])['status'], 'queued')


class MonitorRecoveryTest(unittest.TestCase):
    def setUp(self):
        import remote as r
        self.r = r
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = patch.object(r, 'root_dir', return_value=Path(self.temp.name))
        root.start()
        self.addCleanup(root.stop)
        self.job_id = uuid.uuid4().hex
        self.directory = r.job_path(self.job_id)
        self.directory.mkdir()
        self.data = dict(id=self.job_id, status='running', session='jobs',
                         window_id='@4', gpu='GPU-test', started=123,
                         job_container='job-scheduler-' + self.job_id)
        r.save(self.directory / 'status.json', self.data)
        r.save(self.directory / 'request.json', self.data)
        self.info = dict(State=dict(Running=True), Config=dict(Labels={
            'job-scheduler.id': self.job_id}))

    def test_legacy_worker_cleanup_is_redirected_without_changing_running_command(self):
        req = dict(self.data, container='manual-template', command='training command')
        self.r.save(self.directory / 'request.json', req)
        (self.directory / 'worker.py').write_text('def finish_job():\n    stop_template(req)\n')
        with patch.object(self.r, 'inspect_job_container', return_value=self.info):
            result = self.r.protect_templates()
        self.assertEqual(result, dict(protected=[self.job_id], errors=[]))
        changed = json.loads((self.directory / 'request.json').read_text())
        self.assertEqual(changed['container'], req['job_container'])
        self.assertEqual(changed['legacy_template_container'], 'manual-template')
        self.assertEqual(changed['command'], req['command'])
        self.assertEqual(changed['job_container'], req['job_container'])
        backup = Path(self.temp.name) / '.template-protection-backups' / (self.job_id + '.json')
        self.assertEqual(json.loads(backup.read_text()), req)
        self.assertEqual(self.r.protect_templates(), dict(protected=[], errors=[]))

    def test_legacy_protection_skips_unstarted_jobs_and_current_workers(self):
        req = dict(self.data, container='manual-template')
        self.r.save(self.directory / 'request.json', req)
        (self.directory / 'worker.py').write_text('def finish_job():\n    stop_template(req)\n')
        with patch.object(self.r, 'inspect_job_container', return_value=None):
            self.assertEqual(self.r.protect_templates(), dict(protected=[], errors=[]))
        (self.directory / 'worker.py').write_text(Path(self.r.__file__).read_text())
        with patch.object(self.r, 'inspect_job_container') as inspect:
            self.assertEqual(self.r.protect_templates(), dict(protected=[], errors=[]))
            inspect.assert_not_called()
        self.assertEqual(json.loads((self.directory / 'request.json').read_text()), req)

    def test_probe_leaves_started_template_running_even_on_inspection_failure(self):
        for error in (False, True):
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(self.r, 'template_lock', contextlib.nullcontext))
                stack.enter_context(patch.object(self.r, 'ensure_template', return_value=False))
                probe = stack.enter_context(patch.object(self.r, 'probe_running', return_value={'gpus': []}))
                run = stack.enter_context(patch.object(self.r, 'run'))
                if error:
                    probe.side_effect = RuntimeError('GPU inspection failed')
                    with self.assertRaisesRegex(RuntimeError, 'GPU inspection failed'):
                        self.r.probe({})
                else:
                    self.assertEqual(self.r.probe({}), {'gpus': []})
                run.assert_not_called()

    def test_missing_tmux_falls_back_to_owned_running_container(self):
        for state in ('running', 'unknown'):
            self.r.save(self.directory / 'status.json', dict(self.data, status=state))
            with patch.object(self.r, 'window_exists', return_value=False), \
                 patch.object(self.r, 'inspect_job_container', return_value=self.info):
                result = self.r.read_job(self.job_id)
            self.assertEqual(result['status'], 'running')
            self.assertEqual(result['started'], 123)

    def test_missing_container_keeps_reservation_unknown(self):
        with patch.object(self.r, 'window_exists', return_value=False), \
             patch.object(self.r, 'inspect_job_container', return_value=None):
            self.assertEqual(self.r.read_job(self.job_id)['status'], 'unknown')
            with self.assertRaisesRegex(RuntimeError, 'absent or stopped'):
                self.r.recover(self.job_id)

    def test_wrong_container_label_is_rejected(self):
        self.info['Config']['Labels']['job-scheduler.id'] = 'someone-else'
        response = subprocess.CompletedProcess([], 0, json.dumps([self.info]), '')
        with patch.object(self.r, 'run', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'different job label'):
                self.r.inspect_job_container(self.data)

    def test_log_read_uses_docker_when_worker_log_is_stale(self):
        (self.directory / 'output.log').write_text('stale log')

        def docker_logs(args, **kwargs):
            self.assertEqual(args[:3], ['docker', 'logs', '--tail'])
            kwargs['stdout'].write(b'current Docker log\n')
            return subprocess.CompletedProcess(args, 0)

        with patch.object(self.r, 'window_exists', return_value=False), \
             patch.object(self.r, 'inspect_job_container', return_value=self.info), \
             patch.object(self.r.subprocess, 'run', side_effect=docker_logs):
            self.assertEqual(self.r.read_log(self.job_id, 50), 'current Docker log')

    def test_recover_starts_only_monitor_and_is_idempotent(self):
        response = subprocess.CompletedProcess([], 0, '@12\n', '')
        with patch.object(self.r, 'window_exists', return_value=False), \
             patch.object(self.r, 'inspect_job_container', return_value=self.info), \
             patch.object(self.r, 'SOURCE_CODE', 'monitor source', create=True), \
             patch.object(self.r, 'tmux', return_value=response) as tmux:
            result = self.r.recover(self.job_id)
        self.assertEqual(result['window_id'], '@12')
        self.assertEqual(result['started'], 123)
        command = tmux.call_args.args[0][-1]
        self.assertIn('--monitor', command)
        self.assertNotIn('--worker', command)
        with patch.object(self.r, 'window_exists', return_value=True), \
             patch.object(self.r, 'tmux') as tmux:
            self.r.recover(self.job_id)
            tmux.assert_not_called()

    def test_monitor_retains_exit_code_and_replaces_stale_log(self):
        for code, cancelled, state in ((0, False, 'succeeded'), (7, False, 'failed'),
                                        (143, True, 'cancelled')):
            self.r.save(self.directory / 'status.json', self.data)
            (self.directory / 'output.log').write_text('stale log')
            if cancelled:
                (self.directory / 'cancel.request').touch()
            waiter = unittest.mock.Mock(returncode=0)
            waiter.communicate.return_value = (str(code) + '\n', '')
            waiter.poll.return_value = 0
            logs = unittest.mock.Mock()
            logs.stdout = io.BytesIO(b'current training log\n')
            logs.poll.return_value = 0
            with patch.object(self.r, 'owned_container', return_value=True), \
                 patch.object(self.r.subprocess, 'Popen', side_effect=[waiter, logs]) as popen, \
                 patch.object(self.r.threading, 'Thread'), \
                 patch.object(self.r.os, 'write'), \
                 patch.object(self.r, 'finish_job') as finish:
                self.r.monitor(self.job_id)
            self.assertEqual(finish.call_args.args[1]['status'], state)
            self.assertEqual(finish.call_args.args[1]['exit_code'], code)
            self.assertEqual(popen.call_args_list[0].args[0][1], 'wait')
            self.assertEqual((self.directory / 'output.log').read_text(), 'current training log\n')


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required for lifecycle tests')
class DetachedLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binary = self.root / 'bin'
        binary.mkdir()
        self.socket = 'job-scheduler-test-' + uuid.uuid4().hex
        # An isolated tmux server and a Docker stand-in that runs the same bash
        # command locally. No real containers, GPUs or user tmux sessions touched.
        tmux = binary / 'tmux'
        tmux.write_text(f'#!/bin/sh\nexec /usr/bin/tmux -L {self.socket} "$@"\n')
        tmux.chmod(0o755)
        docker = binary / 'docker'
        shutil.copyfile(s.HERE / 'test_support/fake_docker.py', docker)
        docker.chmod(0o755)
        self.docker_log = self.root / 'docker.operations'
        self.environ = patch.dict(os.environ, {
            'PATH': str(binary) + ':' + os.environ['PATH'],
            'JOB_SCHEDULER_TEST_DOCKER_LOG': str(self.docker_log),
            'JOB_SCHEDULER_TEST_DOCKER_STATE': str(self.root / 'docker-state'),
            'JOB_SCHEDULER_TMUX_SOCKET': self.socket})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.addCleanup(self.kill_tmux)
        source = (s.HERE / 'remote.py').read_text().replace(
            "return Path.home() / '.local/state/job-scheduler' / socket.gethostname()",
            f'return Path({str(self.root / "remote-state")!r})')
        self.ns = {'__name__': 'test_remote', 'SOURCE_CODE': source}
        exec(compile(source, '<test-remote>', 'exec'), self.ns)
        self.req = dict(action='launch', id=uuid.uuid4().hex, container='test',
                        workdir=str(self.root), gpu='GPU-test', min_free_mib=2048,
                        idle_max_used_mib=512, idle_max_utilization=10,
                        command='sleep 0.3; printf "result:%s\\n" "$CUDA_VISIBLE_DEVICES"')

    def kill_tmux(self):
        subprocess.run(['/usr/bin/tmux', '-L', self.socket, 'kill-server'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_result(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            data = self.ns['main']({'action': 'status', 'id': self.req['id']})
            if data['status'] in s.TERMINAL:
                if data.get('window_id'):
                    cleanup_deadline = time.monotonic() + 2
                    while self.ns['window_exists'](data['window_id']) and time.monotonic() < cleanup_deadline:
                        time.sleep(0.05)
                    self.assertFalse(self.ns['window_exists'](data['window_id']))
                marker = Path('/tmp') / f'job-scheduler-{os.getuid()}' / (self.req['id'] + '.json')
                self.assertFalse(marker.exists())
                if self.docker_log.exists():
                    self.assertFalse((self.root / 'docker-state/containers' /
                                      ('job-scheduler-' + self.req['id'])).exists())
                    template_marker = ('worktree3-template-stopped'
                                       if self.req['container'] == 'test-worktree3' else 'template-stopped')
                    self.assertFalse((self.root / 'docker-state' / template_marker).exists())
                return data
            time.sleep(0.05)
        self.fail('Detached worker did not finish: ' + repr(data))

    def test_detached_completion_logs_and_idempotent_launch(self):
        result = self.ns['main'](self.req)
        self.assertEqual(result['status'], 'reserved')
        again = self.ns['main'](self.req)
        self.assertEqual(again['session'], result['session'])
        result = self.wait_result()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['exit_code'], 0)
        logs = self.ns['main']({'action': 'logs', 'id': self.req['id']})
        self.assertIn('result:GPU-test', logs['log'])
        operations = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        command = next(op for op in operations if op[0] == 'run')
        labels = dict(command[i + 1].split('=', 1) for i, arg in enumerate(command)
                      if arg == '--label')
        self.assertEqual(labels['job-scheduler.id'], self.req['id'])
        for key in ('project', 'service', 'container-number'):
            self.assertEqual(labels['com.docker.compose.' + key], '')
        # Completed requests are not executed again even after their tmux exits.
        self.assertEqual(self.ns['main'](self.req)['status'], 'succeeded')

    def test_pipeline_failure_is_reported(self):
        self.req['command'] = 'printf "failed-output\\n"; (exit 7) | cat'
        self.ns['main'](self.req)
        result = self.wait_result()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 7)

    def test_reservation_blocks_second_job_before_gpu_load(self):
        self.req['command'] = 'sleep 0.5'
        self.ns['main'](self.req)
        other = dict(self.req, id=uuid.uuid4().hex)
        self.assertEqual(self.ns['main'](other)['status'], 'busy')
        self.assertEqual(self.wait_result()['status'], 'succeeded')

    def test_missing_tmux_is_unknown_and_does_not_free_reservation(self):
        directory = self.ns['job_path'](self.req['id'])
        directory.mkdir(parents=True)
        self.ns['save'](directory / 'status.json', dict(
            id=self.req['id'], status='running', session='absent', gpu='GPU-test'))
        data = self.ns['main']({'action': 'status', 'id': self.req['id']})
        self.assertEqual(data['status'], 'unknown')
        self.assertEqual(self.ns['main'](dict(self.req, id=uuid.uuid4().hex))['status'], 'busy')

    def test_excluded_gpu_is_visible_but_cannot_launch(self):
        self.req['excluded_gpu_uuids'] = ['GPU-test']
        result = self.ns['main'](dict(self.req, action='probe'))
        first, second = result['gpus']
        self.assertTrue(first['excluded'])
        self.assertFalse(first['accepting_jobs'])
        self.assertFalse(second['excluded'])
        self.assertTrue(second['accepting_jobs'])
        self.assertEqual(self.ns['main'](self.req)['status'], 'busy')
        self.req['gpu'] = 'GPU-test-2'
        self.assertEqual(self.ns['main'](self.req)['status'], 'reserved')
        self.assertEqual(self.wait_result()['status'], 'succeeded')

    def test_recovered_monitor_collects_logs_and_failure_without_relaunch(self):
        directory = self.ns['job_path'](self.req['id'])
        directory.mkdir(parents=True)
        data = dict(id=self.req['id'], status='running', session='jobs', window_id='@999',
                    gpu='GPU-test', started=123, job_container='job-scheduler-' + self.req['id'])
        self.ns['save'](directory / 'status.json', data)
        self.ns['save'](directory / 'request.json', dict(self.req, **{
            'job_container': data['job_container']}))
        docker = self.root / 'bin/docker'
        docker.write_text('#!/usr/bin/python3\n' + '''
import json, os, sys, time
with open(os.environ['JOB_SCHEDULER_TEST_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
action = sys.argv[1]
if action == 'inspect':
    print(json.dumps([dict(State=dict(Running=sys.argv[-1] != 'test'),
                          Config=dict(Labels={'job-scheduler.id': sys.argv[-1].removeprefix('job-scheduler-')}))]))
elif action == 'wait':
    time.sleep(0.8)
    print(7)
elif action == 'logs':
    print('recovered-output', flush=True)
    time.sleep(0.8)
else:
    raise RuntimeError('Unexpected Docker mutation: ' + action)
''')
        result = self.ns['main'](dict(action='recover', id=self.req['id']))
        again = self.ns['main'](dict(action='recover', id=self.req['id']))
        self.assertEqual(result['window_id'], again['window_id'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.ns['main'](dict(action='status', id=self.req['id']))
            if result['status'] in s.TERMINAL:
                break
            time.sleep(0.05)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['started'], 123)
        self.assertIn('recovered-output', (directory / 'output.log').read_text())
        operations = [json.loads(line)[0] for line in self.docker_log.read_text().splitlines()]
        self.assertEqual(operations.count('wait'), 1)
        self.assertFalse(set(operations) & {'run', 'start', 'stop', 'rm'})

    def test_cancel_stops_process_group_and_preserves_unrelated_process(self):
        pidfile = self.root / 'child.pid'
        # The child ignores TERM; the supervisor must escalate to KILL for it.
        child = 'import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        child += f'open({str(pidfile)!r}, "w").write(str(os.getpid())); time.sleep(60)'
        self.req['command'] = shlex.join([sys.executable, '-c', child]) + ' & wait'
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait()))
        self.ns['main'](self.req)
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pidfile.exists())
        reply = self.ns['main']({'action': 'cancel', 'id': self.req['id']})
        self.assertIn(reply['status'], ('cancelling', 'cancelled'))
        result = self.wait_result()
        self.assertEqual(result['status'], 'cancelled')
        self.assertIn(result['exit_code'], (137, 143))
        pid = int(pidfile.read_text())
        proc = Path('/proc') / str(pid) / 'stat'
        self.assertTrue(not proc.exists() or proc.read_text().rsplit(')', 1)[1].split()[0] == 'Z')
        self.assertIsNone(unrelated.poll())

    def test_cancel_before_delayed_launch_creates_tombstone(self):
        result = self.ns['main']({'action': 'cancel', 'id': self.req['id'], 'allow_missing': True})
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.ns['main'](self.req)['status'], 'cancelled')

    def test_missing_running_record_does_not_claim_job_was_stopped(self):
        with self.assertRaisesRegex(RuntimeError, 'record is missing'):
            self.ns['main']({'action': 'cancel', 'id': self.req['id']})

    def test_finished_sessions_are_removed_even_with_remain_on_exit(self):
        # Keep an unrelated session to verify we never kill the whole server.
        tmux = ['/usr/bin/tmux', '-L', self.socket]
        subprocess.run(tmux + ['new-session', '-d', '-s', 'unrelated', 'sleep 60'], check=True)
        subprocess.run(tmux + ['set-window-option', '-g', 'remain-on-exit', 'on'], check=True)
        for command, expected in [('echo success', 'succeeded'), ('exit 7', 'failed'),
                                  ('sleep 60', 'cancelled')]:
            self.req.update(id=uuid.uuid4().hex, command=command)
            self.ns['main'](self.req)
            if expected == 'cancelled':
                deadline = time.monotonic() + 5
                container = self.root / 'docker-state/containers' / ('job-scheduler-' + self.req['id'])
                while not container.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(container.exists())
                self.ns['main']({'action': 'cancel', 'id': self.req['id']})
            self.assertEqual(self.wait_result()['status'], expected)
            self.assertTrue(self.ns['session_exists']('unrelated'))
        sessions = subprocess.run(tmux + ['list-sessions', '-F', '#S'],
                                  text=True, capture_output=True, check=True)
        self.assertEqual(set(sessions.stdout.splitlines()), {'jobs', 'unrelated'})
        windows = subprocess.run(tmux + ['list-windows', '-t', '=jobs', '-F', '#W'],
                                 text=True, capture_output=True, check=True)
        self.assertEqual(windows.stdout.strip(), 'overview')
        operations = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        self.assertEqual(sum(op[0] == 'commit' for op in operations), 1)
        self.assertEqual(sum(op[0] == 'run' for op in operations), 3)

    def test_server_host_limit_rejects_a_third_job_on_another_gpu(self):
        self.req.update(command='sleep 2', max_jobs_per_gpu=2, max_jobs_per_host=2)
        first = self.ns['main'](self.req)
        second_req = dict(self.req, id=uuid.uuid4().hex)
        second = self.ns['main'](second_req)
        third_req = dict(self.req, id=uuid.uuid4().hex, gpu='GPU-test-2')
        self.assertEqual(first['status'], 'reserved')
        self.assertEqual(second['status'], 'reserved')
        self.assertEqual(self.ns['main'](third_req)['status'], 'busy')
        self.assertFalse(self.ns['job_path'](third_req['id']).exists())
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        self.req = second_req
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        third_req['command'] = 'true'
        self.req = third_req
        self.assertEqual(self.ns['main'](third_req)['status'], 'reserved')
        self.assertEqual(self.wait_result()['status'], 'succeeded')

    def test_host_limit_counts_unknown_jobs_and_reservations_on_other_gpus(self):
        reserved = {'GPU-absent': [dict(id='a', status='unknown', min_free_mib=2048),
                                  dict(id='b', status='reserved', min_free_mib=2048)]}
        with patch.dict(self.ns, reservations=lambda: reserved):
            result = self.ns['probe_running'](dict(self.req, max_jobs_per_host=2))
        self.assertEqual(result['host_occupied_jobs'], 2)
        self.assertTrue(all(not gpu['accepting_jobs'] for gpu in result['gpus']))

    def test_two_concurrent_jobs_share_one_session_with_separate_windows(self):
        first = dict(self.req, command='sleep 1')
        second = dict(self.req, id=uuid.uuid4().hex, gpu='GPU-test-2', command='sleep 1')
        a, b = self.ns['main'](first), self.ns['main'](second)
        self.assertEqual(a['session'], b['session'])
        self.assertNotEqual(a['window_id'], b['window_id'])
        listed = self.ns['tmux'](['list-sessions', '-F', '#S'])
        self.assertEqual(listed.stdout.strip(), 'jobs')
        self.req = first
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        self.req = second
        self.assertEqual(self.wait_result()['status'], 'succeeded')

    def test_disposable_container_preserves_mounts_and_uses_auto_remove(self):
        self.ns['main'](self.req)
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        operations = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        command = next(op for op in operations if op[0] == 'run')
        self.assertIn('--rm', command)
        self.assertIn('--init', command)
        self.assertIn('job-scheduler.id=' + self.req['id'], command)
        self.assertIn('CUDA_VISIBLE_DEVICES=GPU-test', command)
        self.assertIn(f'type=bind,src={self.root},dst=/workspace/V2VSimulator', command)

    def test_second_project_uses_its_own_template_mount_and_runtime(self):
        self.ns['main'](self.req)
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        worktree = self.root / 'V2VSimulator-division'
        worktree.mkdir()
        (worktree / 'project.txt').write_text('from-worktree3')
        self.req = dict(self.req, id=uuid.uuid4().hex, container='test-worktree3',
                        workdir=str(worktree), command='cat project.txt')
        self.assertEqual(self.ns['main'](self.req)['status'], 'reserved')
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        log = self.ns['main'](dict(action='logs', id=self.req['id']))['log']
        self.assertIn('from-worktree3', log)
        operations = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        runs = [op for op in operations if op[0] == 'run']
        self.assertIn(f'type=bind,src={worktree},dst=/workspace/V2VSimulator', runs[-1])
        commits = [op for op in operations if op[0] == 'commit']
        self.assertEqual({op[-2] for op in commits}, {'test', 'test-worktree3'})
        self.assertNotEqual(commits[0][-1], commits[1][-1])

    def test_shared_project_job_keeps_execution_settings_on_another_pc(self):
        cfg = fixture_configuration()
        original = s.Queue(self.root / 'original-pc')
        other = s.Queue(self.root / 'other-pc')
        job = s.new_job(cfg, dict(command='echo shared-worktree', project='V2VSimulator-division'))
        original.put(job)
        with patch.object(s, 'remote', side_effect=lambda host, req: self.ns['main'](req)):
            s.refresh_shared(cfg, original, publish=True)
            s.refresh_shared(cfg, other)
        imported = other.get(job['id'])
        self.assertEqual(imported['project'], 'V2VSimulator-division')
        self.assertEqual(imported['execution'], job['execution'])
        cfg['projects'].clear()
        imported.update(host='marimba', gpu='GPU-test')
        self.assertEqual(s.launch_request(cfg, imported)['container'], 'v2vsimulator-division-v2v-dev-1')

    def test_stopped_template_can_probe_and_launch_the_next_job(self):
        state = self.root / 'docker-state'
        state.mkdir()
        (state / 'template-stopped').touch()
        self.assertEqual(len(self.ns['main'](dict(self.req, action='probe'))['gpus']), 2)
        self.assertFalse((state / 'template-stopped').exists())
        self.ns['main'](self.req)
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        self.assertEqual(len(self.ns['main'](dict(self.req, action='probe'))['gpus']), 2)
        self.assertFalse((self.root / 'docker-state/template-stopped').exists())
        self.req = dict(self.req, id=uuid.uuid4().hex)
        self.assertEqual(self.ns['main'](self.req)['status'], 'reserved')
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        operations = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        self.assertIn(['start', 'test'], operations)
        self.assertFalse(any(op[0] == 'rm' and op[-1] == 'test' for op in operations))

    def test_missing_template_is_created_once_then_reused(self):
        state = self.root / 'docker-state'
        state.mkdir()
        (state / 'template-missing').touch()
        script = self.root / 'start-template.sh'
        script.write_text('set -eu\n'
                          'test "$1" = start\n'
                          'printf started >> "$JOB_SCHEDULER_TEST_DOCKER_STATE/template-starts"\n'
                          'touch "$JOB_SCHEDULER_TEST_DOCKER_STATE/template-created"\n'
                          'rm -f "$JOB_SCHEDULER_TEST_DOCKER_STATE/template-stopped"\n')
        self.req['template_start_script'] = str(script)
        self.assertEqual(self.ns['main'](self.req)['status'], 'reserved')
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        self.req = dict(self.req, id=uuid.uuid4().hex)
        self.assertEqual(self.ns['main'](self.req)['status'], 'reserved')
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        self.assertEqual((state / 'template-starts').read_text(), 'started')

    def test_job_completion_preserves_template_for_manual_use(self):
        directory = self.root / 'finish-test'
        directory.mkdir()
        (directory / 'request.json').write_text(json.dumps(dict(self.req, job_container='owned')))
        for status, code in (('succeeded', 0), ('failed', 7), ('cancelled', 143)):
            data = dict(status=status, exit_code=code, window_id='@123')
            with patch.dict(self.ns, tmux=lambda args: None), \
                 patch.object(subprocess, 'run') as run:
                self.ns['finish_job'](directory, data)
            run.assert_not_called()
            saved = json.loads((directory / 'status.json').read_text())
            self.assertEqual(saved['status'], status)
            self.assertEqual(saved['exit_code'], code)

    def test_san_queue_claim_prevents_two_pcs_dispatching_same_job(self):
        job = dict(id=self.req['id'], created=time.time(), status='queued',
                   command='echo shared', name='shared', requested_host=None, min_free_mib=2048)
        self.ns['main']({'action': 'shared_sync', 'jobs': [job]})
        source = self.ns['SOURCE_CODE'].replace(str(self.root / 'remote-state'),
                                              str(self.root / 'other-host'))
        other = {'__name__': 'other_remote', 'SOURCE_CODE': source}
        exec(compile(source, '<other-host>', 'exec'), other)
        started = []
        self.ns['launch_local'] = lambda req: (started.append('a') or dict(status='reserved', gpu=req['gpu']))
        other['launch_local'] = lambda req: (started.append('b') or dict(status='reserved', gpu=req['gpu']))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(ns['main'], dict(self.req, shared_queue=True,
                     host=host, client_job=dict(job, host=host)))
                     for ns, host in ((self.ns, 'a'), (other, 'b'))]
            results = [task.result() for task in tasks]
        self.assertEqual(len(started), 1)
        self.assertEqual(sum('redirect' in result for result in results), 1)

    def test_other_pc_can_import_and_cancel_published_waiting_job(self):
        cfg = fixture_configuration()
        a = s.Queue(self.root / 'pc-a')
        b = s.Queue(self.root / 'pc-b')
        job = s.new_job(cfg, dict(command='echo shared'))
        a.put(job)
        with patch.object(s, 'remote', side_effect=lambda host, req: self.ns['main'](req)), \
             contextlib.redirect_stdout(io.StringIO()):
            s.refresh_shared(cfg, a, publish=True)
            s.refresh_shared(cfg, b)
            s.cancel_jobs(cfg, b, [job['id'][:8]])
            s.refresh_shared(cfg, a)
        self.assertEqual(a.get(job['id'])['status'], 'cancelled')
        self.assertEqual(b.get(job['id'])['status'], 'cancelled')

    def test_cancel_all_imports_shared_jobs_and_also_cancels_unpublished_local_jobs(self):
        cfg = fixture_configuration()
        a = s.Queue(self.root / 'pc-a')
        b = s.Queue(self.root / 'pc-b')
        shared = s.new_job(cfg, dict(command='echo shared'))
        local = s.new_job(cfg, dict(command='echo local', project='V2VSimulator-division'))
        a.put(shared)
        b.put(local)
        with patch.object(s, 'remote', side_effect=lambda host, req: self.ns['main'](req)), \
             contextlib.redirect_stdout(io.StringIO()):
            s.refresh_shared(cfg, a, publish=True)
            s.cancel_jobs(cfg, b, [], all_jobs=True)
            s.refresh_shared(cfg, a)
        self.assertEqual(a.get(shared['id'])['status'], 'cancelled')
        self.assertEqual(b.get(shared['id'])['status'], 'cancelled')
        self.assertEqual(b.get(local['id'])['status'], 'cancelled')

    def test_container_cleanup_refuses_a_different_job_label(self):
        req = dict(self.req, job_container='job-scheduler-' + self.req['id'])
        inspected = subprocess.CompletedProcess([], 0, json.dumps([
            dict(Config=dict(Labels={'job-scheduler.id': 'another-job'}))]), '')
        with patch.dict(self.ns, run=lambda args, **kwargs: inspected):
            with self.assertRaisesRegex(RuntimeError, 'different job label'):
                self.ns['remove_container'](req)

    def test_san_claim_is_persisted_before_starting_worker(self):
        job = dict(id=self.req['id'], created=time.time(), status='queued',
                   command='echo once', name='once', requested_host=None, min_free_mib=2048)
        self.ns['main']({'action': 'shared_sync', 'jobs': [job]})
        def crash_after_claim(req):
            path = self.root / 'shared' / (req['id'] + '.json')
            claimed = json.loads(path.read_text())
            self.assertEqual(claimed['host'], 'a')
            self.assertEqual(claimed['status'], 'dispatching')
            raise RuntimeError('launch interrupted')
        with patch.dict(self.ns, launch_local=crash_after_claim):
            with self.assertRaisesRegex(RuntimeError, 'launch interrupted'):
                self.ns['main'](dict(self.req, shared_queue=True, host='a', client_job=job))
        result = self.ns['main'](dict(self.req, shared_queue=True, host='b', client_job=job))
        self.assertEqual(result['redirect']['host'], 'a')

    def test_sharing_admits_two_jobs_on_one_gpu_and_rejects_third(self):
        self.req.update(command='sleep 1.5', max_jobs_per_gpu=2, gpu_memory_headroom_mib=1024)
        a = self.ns['main'](self.req)
        second = dict(self.req, id=uuid.uuid4().hex)
        b = self.ns['main'](second)
        third = dict(self.req, id=uuid.uuid4().hex)
        self.assertEqual(a['status'], 'reserved')
        self.assertEqual(b['status'], 'reserved')
        self.assertEqual(self.ns['main'](third)['status'], 'busy')
        self.assertEqual(self.wait_result()['status'], 'succeeded')
        self.req = second
        self.assertEqual(self.wait_result()['status'], 'succeeded')

    def test_pending_gpu_memory_is_budgeted_before_cuda_starts(self):
        self.req.update(command='sleep 0.8', max_jobs_per_gpu=2,
                        min_free_mib=6000, gpu_memory_headroom_mib=1024)
        self.ns['main'](self.req)
        other = dict(self.req, id=uuid.uuid4().hex)
        self.assertEqual(self.ns['main'](other)['status'], 'busy')
        self.assertEqual(self.wait_result()['status'], 'succeeded')

    def test_managed_gpu_process_is_not_counted_twice_and_external_is_counted(self):
        job = dict(id=uuid.uuid4().hex, status='running', job_container='managed', min_free_mib=2048)
        def response(args, **kwargs):
            if args[:2] == ['docker', 'top']:
                out = 'PID\n111\n'
            elif '--query-compute-apps=' in args[-1]:
                out = 'GPU-test, 111, 1024\nGPU-test, 222, 1024\n'
            else:
                out = '0, GPU-test, GTX 1080 Ti, 11264, 2048, 90\n'
            return subprocess.CompletedProcess(args, 0, out, '')
        request = dict(self.req, max_jobs_per_gpu=2, gpu_memory_headroom_mib=1024)
        with patch.dict(self.ns, run=response, reservations=lambda: {'GPU-test': [job]}):
            gpu = self.ns['probe_running'](request)['gpus'][0]
        self.assertEqual(gpu['occupied_slots'], 2)
        self.assertEqual(gpu['external_processes'], 1)
        self.assertEqual(gpu['pending_mib'], 1024)
        self.assertFalse(gpu['accepting_jobs'])
        # The same managed process alone is one slot, despite high utilization.
        def managed_only(args, **kwargs):
            result = response(args, **kwargs)
            if '--query-compute-apps=' in args[-1]:
                result.stdout = 'GPU-test, 111, 1024\n'
            return result
        with patch.dict(self.ns, run=managed_only, reservations=lambda: {'GPU-test': [job]}):
            gpu = self.ns['probe_running'](request)['gpus'][0]
        self.assertEqual(gpu['occupied_slots'], 1)
        self.assertTrue(gpu['accepting_jobs'])


if __name__ == '__main__':
    unittest.main()
