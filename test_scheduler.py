"""Scheduling, failure recovery and real detached-tmux lifecycle tests (no SSH/GPU)."""
import contextlib
import copy
import io
import json
import os
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


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.q = s.Queue(Path(self.temp.name))
        self.cfg = s.configuration(s.HERE / 'hosts.json')
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

    def test_host_restriction(self):
        job = self.job()
        job['requested_host'] = 'marimba'
        inv = {'xylophone0': {'gpus': [self.gpu()]}}
        self.assertEqual(s.candidates(self.cfg, inv, job), [])

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
        cfg = s.configuration(s.HERE / 'hosts.json')
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
            gpu = self.ns['probe'](request)['gpus'][0]
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
            gpu = self.ns['probe'](request)['gpus'][0]
        self.assertEqual(gpu['occupied_slots'], 1)
        self.assertTrue(gpu['accepting_jobs'])


if __name__ == '__main__':
    unittest.main()
