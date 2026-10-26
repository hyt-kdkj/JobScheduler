#!/usr/bin/env python3
"""A small SSH/tmux/Docker GPU job queue. Requires Python 3.9+ and OpenSSH."""
import argparse
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import itertools
import math
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
TERMINAL = {'succeeded', 'failed', 'cancelled'}


def remote(host, request):
    source = (HERE / 'remote.py').read_text()
    program = 'SOURCE_CODE = ' + repr(source) + '\n' + source
    encoded = base64.b64encode(program.encode()).decode()
    loader = f"import base64; exec(compile(base64.b64decode({encoded!r}), '<remote>', 'exec'))"
    cmd = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ControlPersist=no',
           '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2', host,
           shlex.join(['python3', '-c', loader])]
    try:
        result = subprocess.run(cmd, input=json.dumps(request), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=100)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'SSH exit {result.returncode}')
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError('Non-JSON SSH output: ' + result.stdout[:500]) from exc
    if 'error' in data and 'status' not in data:
        raise RuntimeError(data['error'])
    return data


def configuration(path):
    cfg = json.loads(path.read_text())
    names = [h['ssh'] for h in cfg['hosts']]
    if not names or len(names) != len(set(names)):
        raise ValueError('hosts must contain unique SSH names')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', h) for h in names):
        raise ValueError('Invalid SSH alias')
    for key in ('idle_max_used_mib', 'idle_max_utilization'):
        if not math.isfinite(cfg[key]) or cfg[key] < 0:
            raise ValueError(f'{key} must be finite and nonnegative')
    shared = cfg.get('shared_queue')
    if shared and (not isinstance(shared, dict) or shared.get('host') not in names):
        raise ValueError('shared_queue.host must name a configured SSH host')
    for entry in [cfg] + cfg['hosts']:
        limit = entry.get('max_jobs_per_gpu', cfg.get('max_jobs_per_gpu', 1))
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError('max_jobs_per_gpu must be a positive integer')
        margin = entry.get('gpu_memory_headroom_mib', cfg.get('gpu_memory_headroom_mib', 0))
        if not isinstance(margin, (int, float)) or not math.isfinite(margin) or margin < 0:
            raise ValueError('gpu_memory_headroom_mib must be nonnegative')
    return cfg


def host_request(cfg, host):
    entry = next(h for h in cfg['hosts'] if h['ssh'] == host)
    result = {key: entry.get(key, cfg[key]) for key in
              ('container', 'workdir', 'idle_max_used_mib', 'idle_max_utilization')}
    if entry.get('runtime_image', cfg.get('runtime_image')):
        result['runtime_image'] = entry.get('runtime_image', cfg.get('runtime_image'))
    for key, default in (('max_jobs_per_gpu', 1), ('gpu_memory_headroom_mib', 0)):
        result[key] = entry.get(key, cfg.get(key, default))
    return result


def priority(cfg, name):
    return max((value for pattern, value in cfg['priorities'].items()
                if pattern.lower() in name.lower()), default=0)


def inventory(cfg):
    def check(entry):
        host = entry['ssh']
        try:
            data = remote(host, dict(host_request(cfg, host), action='probe'))
            for gpu in data['gpus']:
                gpu.update(host=host, priority=priority(cfg, gpu['name']))
            return host, data
        except RuntimeError as exc:
            return host, {'error': str(exc)}
    with ThreadPoolExecutor(max_workers=len(cfg['hosts'])) as pool:
        return dict(pool.map(check, cfg['hosts']))


def candidates(cfg, inv, job):
    options = [gpu for host, data in inv.items() for gpu in data.get('gpus', [])
               if (not job.get('requested_host') or job['requested_host'] == host)
               and gpu.get('accepting_jobs', gpu['idle'])
               and gpu.get('available_mib', gpu['total_mib'] - gpu['used_mib']) >= job['min_free_mib']]
    # Stable order for equal GPU models; priority is configurable, not a benchmark.
    return sorted(options, key=lambda g: (-g['priority'], g['utilization'],
                                        -g['total_mib'] + g['used_mib'],
                                        g['host'], g['index']))


class Queue:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.db = sqlite3.connect(directory / 'queue.sqlite3', timeout=20)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs '
                        '(id TEXT PRIMARY KEY, created REAL NOT NULL, body TEXT NOT NULL)')

    def put(self, job):
        with self.db:
            self.db.execute('INSERT INTO jobs VALUES (?, ?, ?) ON CONFLICT(id) '
                            'DO UPDATE SET body=excluded.body',
                            (job['id'], job['created'], json.dumps(job, ensure_ascii=False)))

    def add_many(self, jobs):
        with self.db:
            self.db.executemany('INSERT INTO jobs VALUES (?, ?, ?)', [
                (j['id'], j['created'], json.dumps(j, ensure_ascii=False)) for j in jobs])

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT body FROM jobs ORDER BY created, id')]

    def update_if_unchanged(self, before, after):
        with self.db:
            self.db.execute('UPDATE jobs SET body=? WHERE id=? AND body=?',
                            (json.dumps(after, ensure_ascii=False), before['id'],
                             json.dumps(before, ensure_ascii=False)))

    def get(self, prefix):
        matches = [j for j in self.all() if j['id'].startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f'Job ID must match exactly one job: {prefix}')
        return matches[0]


def apply_result(job, data):
    if 'redirect' in data:
        data = data['redirect']
    job.pop('connection_error', None)
    job.pop('error', None)
    if data['status'] == 'missing':
        job.update(status='unknown', error='Remote job record is missing; inspect manually')
        return
    for key in ('status', 'exit_code', 'started', 'finished', 'session', 'error',
                'window_id', 'job_container', 'cleanup_error'):
        if key in data:
            job[key] = data[key]
    for key in ('host', 'gpu', 'gpu_index', 'gpu_name', 'cancel_requested'):
        if key in data:
            job[key] = data[key]


def requeue(job):
    job['status'] = 'queued'
    for key in ('host', 'gpu', 'gpu_index', 'gpu_name', 'session', 'connection_error', 'error'):
        job.pop(key, None)


def launch_request(cfg, job):
    req = dict(host_request(cfg, job['host']), action='launch', id=job['id'],
                command=job['command'], gpu=job['gpu'], min_free_mib=job['min_free_mib'])
    if cfg.get('shared_queue'):
        req.update(shared_queue=True, host=job['host'], client_job=job)
    return req


def shared_rpc(cfg, request):
    preferred = cfg['shared_queue']['host']
    hosts = [preferred] + [h['ssh'] for h in cfg['hosts'] if h['ssh'] != preferred]
    errors = []
    for host in hosts:
        try:
            return remote(host, request)
        except RuntimeError as exc:
            errors.append(f'{host}: {exc}')
    raise RuntimeError('Cannot access the SAN shared queue: ' + '; '.join(errors))


def refresh_shared(cfg, queue, publish=False):
    if not cfg.get('shared_queue'):
        return
    pending = [j for j in queue.all() if j['status'] == 'queued' and not j.get('published')] if publish else []
    response = shared_rpc(cfg, {'action': 'shared_sync', 'jobs': pending})
    existing = {j['id']: j for j in queue.all()}
    for remote_job in response['jobs']:
        job = dict(existing.get(remote_job['id'], {}))
        if job.get('status') in TERMINAL and remote_job['status'] not in TERMINAL:
            # Another local command may have completed a cancel while the SAN
            # snapshot was in flight. Never resurrect a terminal local record.
            continue
        # Result-sync metadata is local to each PC, never shared across clients.
        sync = job.get('result_sync')
        job.update(remote_job, published=True)
        if sync:
            job['result_sync'] = sync
        queue.put(job)


def reconcile(cfg, queue):
    for job in queue.all():
        if job['status'] not in ('dispatching', 'reserved', 'running', 'cancelling', 'unknown'):
            continue
        try:
            if job.get('cancel_requested'):
                apply_result(job, remote(job['host'], {
                    'action': 'cancel', 'id': job['id'],
                    'allow_missing': job['status'] == 'dispatching'}))
            elif job['status'] == 'dispatching':
                # Same job ID, host and GPU make retries safe after a lost reply.
                result = remote(job['host'], launch_request(cfg, job))
                if result.get('status') == 'busy':
                    requeue(job)
                else:
                    apply_result(job, result)
            else:
                apply_result(job, remote(job['host'], {'action': 'status', 'id': job['id']}))
        except RuntimeError as exc:
            job['connection_error'] = str(exc)
        queue.put(job)


def dispatch(cfg, queue):
    reconcile(cfg, queue)
    if not any(j['status'] == 'queued' for j in queue.all()):
        return
    inv = inventory(cfg)
    for host, data in inv.items():
        if 'error' in data:
            print(f'[{host}] unavailable: {data["error"]}', file=sys.stderr, flush=True)
    for job in queue.all():
        if job['status'] != 'queued':
            continue
        for gpu in candidates(cfg, inv, job):
            # Persist the target before sending anything to the server.
            job.update(status='dispatching', host=gpu['host'], gpu=gpu['uuid'],
                       gpu_name=gpu['name'], gpu_index=gpu['index'])
            queue.put(job)
            try:
                result = remote(job['host'], launch_request(cfg, job))
                if result.get('status') == 'busy':
                    requeue(job)
                    queue.put(job)
                    gpu['idle'] = False
                    gpu['accepting_jobs'] = False
                    continue
                apply_result(job, result)
            except RuntimeError as exc:
                # Keep the reservation target and retry idempotently next time.
                job['connection_error'] = str(exc)
            queue.put(job)
            gpu['idle'] = False
            gpu['accepting_jobs'] = False
            if host_request(cfg, gpu['host'])['max_jobs_per_gpu'] > 1:
                # Refresh server-side counts/budgets after each launch, including
                # jobs still preparing their image and not yet visible to NVML.
                try:
                    data = remote(gpu['host'], dict(host_request(cfg, gpu['host']), action='probe'))
                    for current in data['gpus']:
                        current.update(host=gpu['host'], priority=priority(cfg, current['name']))
                    inv[gpu['host']] = data
                except RuntimeError as exc:
                    # No fresh accounting: don't reuse any stale slot on this host.
                    inv[gpu['host']] = {'error': str(exc)}
            print(f'{job["id"][:8]} {job["status"]} '
                  f'{job["host"]} GPU {gpu["index"]} {gpu["name"]}', flush=True)
            break


def dispatch_once(cfg, queue):
    with (queue.directory / 'dispatch.lock').open('a') as lock:
        # Serialize local submissions; no process stays resident.
        fcntl.flock(lock, fcntl.LOCK_EX)
        refresh_shared(cfg, queue, publish=True)
        dispatch(cfg, queue)
    sync_completed(cfg, queue)


def sync_completed(cfg, queue):
    """Pull results once for newly completed jobs; retry failed transfers later."""
    settings = cfg.get('results_sync')
    if not settings:
        return
    with (queue.directory / 'results-sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = [j for j in queue.all() if j['status'] in TERMINAL and j.get('host')
                   and j.get('result_sync', {}).get('status') != 'succeeded']
        if not pending:
            return
        attempted = time.time()
        source = settings['source']
        destination = Path(settings['destination']).expanduser()
        ssh = shlex.join(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                          '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                          '-o', 'ControlPersist=no', '-o', 'ServerAliveInterval=10',
                          '-o', 'ServerAliveCountMax=2'])
        # Keep the requested -az behavior: overwrite changed files, never --delete.
        args = ['rsync', '-az', '--timeout=60', '-e', ssh, '--', source,
                str(destination) + '/']
        print(f'Syncing results: {source} -> {destination}', flush=True)
        try:
            destination.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    timeout=settings.get('timeout_seconds', 1800))
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f'rsync exit {result.returncode}')
            outcome = dict(status='succeeded', completed=time.time())
            print('Results sync succeeded.', flush=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            outcome = dict(status='failed', error=str(exc))
            print(f'Results sync failed; will retry on the next check: {exc}',
                  file=sys.stderr, flush=True)
        outcome.update(attempted=attempted, source=source, destination=str(destination))
        for previous in pending:
            job = queue.get(previous['id'])
            job['result_sync'] = outcome.copy()
            queue.put(job)


def elapsed(job, now=None):
    if job.get('started') is None:
        return '-'
    end = job.get('finished', time.time() if now is None else now)
    seconds = max(0, int(end - job['started']))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


def show_jobs(jobs):
    now = time.time()
    print(f'{"ID":8} {"STATE":12} {"HOST":13} {"GPU":3} {"EXIT":4} '
          f'{"ELAPSED":10} {"SYNC":9} NAME / COMMAND')
    for j in jobs:
        print(f'{j["id"][:8]} {j["status"]:12} {(j.get("host") or "-"):13} '
              f'{str(j.get("gpu_index", "-")):3} '
              f'{str(j.get("exit_code", "-")):4} '
              f'{elapsed(j, now):10} '
              f'{j.get("result_sync", {}).get("status", "-"):9} '
              f'{j.get("name") or j["command"]}')
        for key in ('error', 'connection_error', 'cleanup_error'):
            if j.get(key):
                print(f'  {key}: {j[key]}')
        if j.get('result_sync', {}).get('error'):
            print('  sync_error: ' + j['result_sync']['error'])


def new_job(cfg, spec):
    allowed = {'command', 'name', 'host', 'min_free_gb'}
    if set(spec) - allowed:
        raise ValueError('Unknown job fields: ' + ', '.join(sorted(set(spec) - allowed)))
    command = spec.get('command')
    if not isinstance(command, str) or not command.strip():
        raise ValueError('Command must be a nonempty string')
    if not isinstance(spec.get('name', ''), str):
        raise ValueError('Job name must be a string')
    host = spec.get('host')
    if host and host not in [h['ssh'] for h in cfg['hosts']]:
        raise ValueError('Unknown host: ' + str(host))
    memory = spec.get('min_free_gb', 2)
    if not isinstance(memory, (float, int)) or not math.isfinite(memory) or memory < 0:
        raise ValueError('min_free_gb must be finite and nonnegative')
    return dict(id=uuid.uuid4().hex, created=time.time(), status='queued',
                command=command, name=spec.get('name', ''), requested_host=host,
                min_free_mib=math.ceil(memory * 1024))


def batch_jobs(cfg, path):
    data = json.loads(path.read_text())
    defaults = data.get('defaults', {}) if isinstance(data, dict) else {}
    specs = data.get('jobs') if isinstance(data, dict) else data
    if not isinstance(defaults, dict) or not isinstance(specs, list) or not specs:
        raise ValueError('Batch requires a nonempty jobs list and optional defaults object')
    jobs, batch_id = [], uuid.uuid4().hex
    for row in specs:
        if not isinstance(row, dict):
            raise ValueError('Each batch job must be an object')
        spec = dict(defaults, **row)
        matrix = spec.pop('matrix', {})
        if not isinstance(matrix, dict) or any(
                not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or
                not isinstance(values, list) or not values or any(
                    not isinstance(v, (str, int, float, bool)) or
                    (isinstance(v, float) and not math.isfinite(v)) for v in values)
                for key, values in matrix.items()):
            raise ValueError('matrix must map variable names to nonempty scalar lists')
        count = math.prod(len(values) for values in matrix.values())
        if len(jobs) + count > 10000:
            raise ValueError('A batch may contain at most 10000 jobs')
        for values in itertools.product(*matrix.values()):
            expanded = dict(spec)
            for field in ('command', 'name'):
                text = expanded.get(field, '')
                if isinstance(text, str):
                    # Preserve unrelated braces used by shell/Python commands.
                    for key, value in zip(matrix, values):
                        text = text.replace('{' + key + '}', str(value))
                    expanded[field] = text
            job = new_job(cfg, expanded)
            job.update(batch_id=batch_id, batch_file=str(path.resolve()))
            jobs.append(job)
    return jobs


def cancel_jobs(cfg, queue, prefixes, batch=None):
    # Share the dispatch lock so a locally queued job cannot launch during cancel.
    with (queue.directory / 'dispatch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        selected = {queue.get(prefix)['id'] for prefix in prefixes}
        if batch:
            matches = {j['batch_id'] for j in queue.all()
                       if j.get('batch_id', '').startswith(batch)}
            if len(matches) != 1:
                raise ValueError('Batch ID must match exactly one batch')
            selected.update(j['id'] for j in queue.all() if j.get('batch_id') in matches)
        if not selected:
            raise ValueError('Specify job IDs or --batch BATCH_ID')
        for job_id in selected:
            job = queue.get(job_id)
            if job.get('published') and cfg.get('shared_queue'):
                current = shared_rpc(cfg, {'action': 'shared_cancel', 'id': job_id})
                apply_result(job, current)
            if job['status'] in TERMINAL:
                queue.put(job)
                continue
            if job['status'] == 'queued':
                job.update(status='cancelled', finished=time.time(), exit_code=None)
            else:
                # Persist intent before SSH; a lost reply can be retried by status.
                job['cancel_requested'] = True
                queue.put(job)
                try:
                    apply_result(job, remote(job['host'], {
                        'action': 'cancel', 'id': job_id,
                        'allow_missing': job['status'] == 'dispatching'}))
                except RuntimeError as exc:
                    job['connection_error'] = str(exc)
            queue.put(job)
    # Cancelling is asynchronous. A subsequent status confirms termination/sync.
    show_jobs([j for j in queue.all() if j['id'] in selected])


def log_filename(job):
    name = re.sub(r'[^\w.-]+', '_', job.get('name') or 'job')
    name = name.encode('utf-8')[:160].decode('utf-8', errors='ignore').strip('._-') or 'job'
    return f'{name}_{job["id"][:8]}.log'


def save_log(job, log, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / log_filename(job)
    path.write_text(log + '\n', encoding='utf-8')
    print(f'Saved: {path}')


def running_logs(cfg, queue, lines, output_dir=Path('logs/current')):
    selected = [j for j in queue.all() if j.get('host') and
                j['status'] in ('dispatching', 'reserved', 'running', 'cancelling')]
    if not selected:
        sync_completed(cfg, queue)
        print('No running jobs.')
        return
    grouped = {}
    for job in selected:
        grouped.setdefault(job['host'], []).append(job)
    for host, jobs in grouped.items():
        try:
            result = remote(host, dict(action='logs_batch', ids=[j['id'] for j in jobs], lines=lines))
        except RuntimeError as exc:
            print(f'[{host}] logs unavailable: {exc}', file=sys.stderr)
            continue
        records = {j['id']: j for j in result['jobs']}
        for job in jobs:
            record = records[job['id']]
            updated = copy.deepcopy(job)
            if not (record['status'] == 'missing' and job['status'] == 'dispatching'):
                apply_result(updated, record)
                queue.update_if_unchanged(job, updated)
            save_log(job, record['log'], output_dir)
    sync_completed(cfg, queue)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=HERE / 'hosts.json')
    p.add_argument('--state-dir', type=Path, default=HERE / 'state')
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('gpus', help='Inspect GPUs; makes no job submissions')
    s = sub.add_parser('submit', help='Add one shell command to the local queue')
    s.add_argument('command', help='Quote the entire shell command')
    s.add_argument('--name', default='')
    s.add_argument('--host', help='Restrict to one configured SSH host')
    s.add_argument('--min-free-gb', type=float, default=2,
                   help='Minimum free VRAM in GiB (default: 2)')
    s.add_argument('--queue-only', action='store_true', help='Store locally without SSH')
    s = sub.add_parser('submit-batch', help='Submit a JSON job list or parameter matrix')
    s.add_argument('file', type=Path)
    s.add_argument('--queue-only', action='store_true')
    s.add_argument('--dry-run', action='store_true', help='Preview without submitting or SSH')
    s = sub.add_parser('cancel', help='Cancel queued jobs or stop assigned jobs')
    s.add_argument('ids', nargs='*')
    s.add_argument('--batch', help='Cancel all jobs in this batch')
    sub.add_parser('dispatch', help='Try queued jobs once, then disconnect')
    s = sub.add_parser('status', help='Show completion state and exit codes')
    s.add_argument('--local', action='store_true', help='Show cached state without SSH')
    s.add_argument('--dispatch', action='store_true', help='Also try queued jobs once')
    s = sub.add_parser('logs', help='Save the remote log tail to files, including after completion')
    s.add_argument('id', nargs='?')
    s.add_argument('--running', action='store_true', help='Get logs for all active jobs')
    s.add_argument('--output-dir', type=Path, default=Path('logs/current'),
                   help='Directory for log files (default: logs/current)')
    s.add_argument('--lines', type=int, default=80)
    s = sub.add_parser('info', help='Show saved command, session and results without SSH')
    s.add_argument('id')
    s = sub.add_parser('export', help='Export job history as a Git-friendly JSON snapshot')
    s.add_argument('--output', type=Path, default=HERE / 'jobs/history.json')
    s.add_argument('--refresh', action='store_true', help='Fetch shared/remote state first')
    return p


def main():
    args = parser().parse_args()
    cfg = configuration(args.config)
    queue = Queue(args.state_dir)
    if args.action == 'submit':
        job = new_job(cfg, dict(command=args.command, name=args.name,
                               host=args.host, min_free_gb=args.min_free_gb))
        queue.put(job)
        print(job['id'], flush=True)
        if not args.queue_only:
            dispatch_once(cfg, queue)
            show_jobs([queue.get(job['id'])])
    elif args.action == 'submit-batch':
        jobs = batch_jobs(cfg, args.file)
        print(f'Batch {jobs[0]["batch_id"]}: {len(jobs)} jobs', flush=True)
        if args.dry_run:
            show_jobs(jobs)
            for job in jobs:
                print(job['command'])
        else:
            queue.add_many(jobs)
            if not args.queue_only:
                dispatch_once(cfg, queue)
            ids = {j['id'] for j in jobs}
            show_jobs([j for j in queue.all() if j['id'] in ids])
    elif args.action == 'cancel':
        try:
            for prefix in args.ids:
                queue.get(prefix)
            if args.batch and not any(j.get('batch_id', '').startswith(args.batch) for j in queue.all()):
                raise ValueError('Unknown batch')
        except ValueError:
            refresh_shared(cfg, queue)
        cancel_jobs(cfg, queue, args.ids, args.batch)
    elif args.action == 'gpus':
        for host, data in inventory(cfg).items():
            print(host)
            if 'error' in data:
                print('  UNAVAILABLE: ' + data['error'])
            if not data.get('gpus') and 'error' not in data:
                print('  No GPUs visible')
            for g in data.get('gpus', []):
                print(f'  GPU {g["index"]} {g["name"]}: '
                      f'{g["total_mib"] - g["used_mib"]}/{g["total_mib"]} MiB free, '
                      f'{g["utilization"]}% util, priority={g["priority"]}, '
                      f'{"AVAILABLE" if g.get("accepting_jobs", g["idle"]) else "FULL"}, '
                      f'slots={g.get("occupied_slots", 0)}/{g.get("max_jobs", 1)}, '
                      f'budget={g.get("available_mib", g["total_mib"] - g["used_mib"])} MiB'
                      + (f' job={g["reserved_by"][:8]}' if g['reserved_by'] else ''))
    elif args.action == 'dispatch':
        dispatch_once(cfg, queue)
        show_jobs(queue.all())
    elif args.action == 'status':
        if args.local and args.dispatch:
            raise ValueError('--local and --dispatch cannot be combined')
        if args.dispatch:
            dispatch_once(cfg, queue)
        elif not args.local:
            # Status never starts a job unless --dispatch is explicit.
            refresh_shared(cfg, queue)
            refresh_status(queue)
            sync_completed(cfg, queue)
        show_jobs(queue.all())
    elif args.action == 'logs':
        if args.lines < 1:
            raise ValueError('--lines must be positive')
        if args.running:
            if args.id:
                raise ValueError('Specify either a job ID or --running')
            refresh_shared(cfg, queue)
            running_logs(cfg, queue, args.lines, args.output_dir)
        else:
            if not args.id:
                raise ValueError('Specify a job ID, or use --running')
            j = queue.get(args.id)
            if not j.get('host'):
                raise ValueError('Job has not been assigned to a host yet')
            refresh_status(queue, j['id'])
            sync_completed(cfg, queue)
            log = remote(j['host'], {'action': 'logs', 'id': j['id'], 'lines': args.lines})['log']
            save_log(j, log, args.output_dir)
    elif args.action == 'info':
        j = queue.get(args.id)
        j['elapsed'] = elapsed(j)
        if j.get('started') is not None:
            j['elapsed_seconds'] = max(0, int(j.get('finished', time.time()) - j['started']))
        print(json.dumps(j, ensure_ascii=False, indent=2))
    elif args.action == 'export':
        if args.refresh:
            refresh_shared(cfg, queue)
            refresh_status(queue)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        snapshot = dict(schema_version=1, exported_at=time.time(), jobs=queue.all())
        temporary = args.output.with_name(args.output.name + '.tmp-' + uuid.uuid4().hex)
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(args.output)
        print(f'Exported {len(snapshot["jobs"])} jobs: {args.output}')
    return 0


def refresh_status(queue, only=None):
    for job in queue.all():
        if (only and job['id'] != only) or job['status'] in TERMINAL or not job.get('host'):
            continue
        before = copy.deepcopy(job)
        try:
            action = 'cancel' if job.get('cancel_requested') else 'status'
            data = remote(job['host'], {'action': action, 'id': job['id'],
                                       'allow_missing': job['status'] == 'dispatching'})
            # A lost launch reply may precede creation of the remote record.
            if data['status'] == 'missing' and job['status'] == 'dispatching':
                continue
            apply_result(job, data)
        except RuntimeError as exc:
            job['connection_error'] = str(exc)
        # A status reader must not overwrite a concurrent dispatcher's newer state.
        queue.update_if_unchanged(before, job)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print('error: ' + str(exc), file=sys.stderr)
        sys.exit(2)
