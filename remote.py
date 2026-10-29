"""Remote helper, sent over SSH by scheduler.py. Python standard library only."""
import contextlib
import csv
import fcntl
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time


class CancelledBeforeStart(Exception):
    pass


# This supervisor lives inside the container. Its child has its own process group,
# allowing cancellation of the job tree without stopping the container or tmux.
CONTAINER_RUNNER = r'''
import contextlib, json, os, signal, subprocess, sys, time
from pathlib import Path
job_id, command = sys.argv[1:]
directory = Path('/tmp') / ('job-scheduler-' + str(os.getuid()))
directory.mkdir(mode=0o700, exist_ok=True)
marker = directory / (job_id + '.json')
cancelled = False
def request_cancel(signum, frame):
    global cancelled
    cancelled = True
signal.signal(signal.SIGTERM, request_cancel)
process = subprocess.Popen(['bash', '-o', 'pipefail', '-c', command], start_new_session=True)
start = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
temporary = marker.with_suffix('.tmp')
temporary.write_text(json.dumps(dict(pid=os.getpid(), start=start)))
temporary.replace(marker)
try:
    while process.poll() is None and not cancelled:
        time.sleep(0.1)
    if cancelled:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        sys.exit(130)
    sys.exit(process.returncode)
finally:
    marker.unlink(missing_ok=True)
'''

CONTAINER_CANCEL = r'''
import json, os, signal, sys
from pathlib import Path
job_id = sys.argv[1]
marker = Path('/tmp') / ('job-scheduler-' + str(os.getuid())) / (job_id + '.json')
try:
    data = json.loads(marker.read_text())
    proc = Path('/proc') / str(data['pid'])
    start = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    environment = (proc / 'environ').read_bytes().split(b'\0')
    if start != data['start'] or ('JOB_SCHEDULER_ID=' + job_id).encode() not in environment:
        sys.exit(3)
    os.kill(data['pid'], signal.SIGTERM)
except (FileNotFoundError, ProcessLookupError):
    sys.exit(3)
'''


def run(args, **kwargs):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=kwargs.pop('timeout', 25), **kwargs)


def tmux(args):
    # A dedicated server avoids the user's tmux hooks and session settings.
    sock = os.environ.get('JOB_SCHEDULER_TMUX_SOCKET', 'job-scheduler')
    return run(['tmux', '-L', sock, '-f', '/dev/null'] + args)


def window_exists(window):
    result = tmux(['display-message', '-p', '-t', window, '#{pane_dead}'])
    return result.returncode == 0 and result.stdout.strip() == '0'


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def root_dir():
    # Some servers share HOME. Keep reservations and tmux state host-specific.
    return Path.home() / '.local/state/job-scheduler' / socket.gethostname()


def job_path(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('Invalid job ID')
    return root_dir() / job_id


@contextlib.contextmanager
def shared_lock():
    directory = root_dir().parent / 'shared'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def shared_record(path):
    job = json.loads(path.read_text())
    submitted = job['created']
    if job.get('remote_dir'):
        status = Path(job['remote_dir']) / 'status.json'
        if status.exists():
            job.update(json.loads(status.read_text()))
    job['created'] = submitted
    return job


def shared_sync(jobs):
    with shared_lock() as directory:
        allowed = {'id', 'command', 'name', 'created', 'status', 'requested_host',
                   'min_free_mib', 'batch_id', 'batch_file'}
        for job in jobs:
            job_path(job['id'])  # Validate before using the ID as a filename.
            path = directory / (job['id'] + '.json')
            if not path.exists():
                if job['status'] != 'queued':
                    raise ValueError('Only queued jobs can be published')
                save(path, {k: v for k, v in job.items() if k in allowed})
        return {'jobs': [shared_record(p) for p in sorted(directory.glob('*.json'))]}


def shared_cancel(job_id):
    job_path(job_id)
    with shared_lock() as directory:
        path = directory / (job_id + '.json')
        job = shared_record(path)
        if job['status'] not in ('succeeded', 'failed', 'cancelled'):
            job['cancel_requested'] = True
            if not job.get('host'):
                job.update(status='cancelled', finished=time.time(), exit_code=None)
            save(path, job)
        return job


def driver_env():
    """Only select isolated libraries matching the currently loaded module."""
    version_file = Path('/proc/driver/nvidia/version')
    text = version_file.read_text() if version_file.exists() else ''
    match = re.search(r'Kernel Module\s+(\d+\.\d+\.\d+)', text)
    directory = f'/opt/nvidia-driver-{match[1]}/lib' if match else ''
    return (f'if [ -d {shlex.quote(directory)} ]; then '
            f'export LD_LIBRARY_PATH={shlex.quote(directory)}'
            '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}; fi; ')


def docker_command(req, shell, gpu=None):
    args = ['docker', 'exec', '--user', f'{os.getuid()}:{os.getgid()}',
            '--workdir', req['workdir']]
    if gpu:
        args += ['--env', f'CUDA_VISIBLE_DEVICES={gpu}',
                 '--env', 'CUDA_DEVICE_ORDER=PCI_BUS_ID',
                 '--env', 'PYTHONUNBUFFERED=1',
                 '--env', f'JOB_SCHEDULER_ID={req["id"]}']
    return args + [req['container'], 'bash', '-c', 'set -o pipefail; ' + driver_env() + shell]


def session_exists(session):
    return run(['tmux', 'has-session', '-t', '=' + session]).returncode == 0


def read_job(job_id):
    path = job_path(job_id) / 'status.json'
    if not path.exists():
        return {'id': job_id, 'status': 'missing'}
    data = json.loads(path.read_text())
    alive = (window_exists(data['window_id']) if data.get('window_id') else
             session_exists(data['session'])) if data['status'] in ('reserved', 'running') else True
    if data['status'] in ('reserved', 'running') and not alive:
        # Do not release a GPU or retry an uncertain command automatically.
        data['status'] = 'unknown'
        data['error'] = 'tmux session is absent; check the container before retrying'
    elif data['status'] in ('reserved', 'running') and (path.parent / 'cancel.request').exists():
        data['status'] = 'cancelling'
    return data


def reservations():
    result = {}
    for path in root_dir().glob('*/status.json'):
        data = read_job(path.parent.name)
        if data['status'] in ('reserved', 'running', 'cancelling', 'unknown'):
            request_path = path.parent / 'request.json'
            req = json.loads(request_path.read_text()) if request_path.exists() else {}
            data['min_free_mib'] = req.get('min_free_mib', 2048)
            data['job_container'] = data.get('job_container', req.get('job_container'))
            result.setdefault(data['gpu'], []).append(data)
    return result


@contextlib.contextmanager
def template_lock():
    root_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root_dir() / 'template.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def template_running(req):
    result = run(['docker', 'inspect', req['container']])
    if result.returncode:
        raise RuntimeError('Cannot inspect template container: ' + result.stderr.strip())
    return json.loads(result.stdout)[0].get('State', {}).get('Running', True)


def stop_template(req):
    with template_lock():
        if template_running(req):
            result = run(['docker', 'stop', '--time', '10', req['container']])
            if result.returncode:
                raise RuntimeError('Cannot stop template container: ' + result.stderr.strip())


def probe(req):
    # GPU inspection uses docker exec. Temporarily restart a stopped template,
    # and serialize against workers stopping it or taking an image snapshot.
    with template_lock():
        was_running = template_running(req)
        if not was_running:
            result = run(['docker', 'start', req['container']])
            if result.returncode:
                raise RuntimeError('Cannot start template container: ' + result.stderr.strip())
        try:
            return probe_running(req)
        finally:
            if not was_running:
                result = run(['docker', 'stop', '--time', '10', req['container']])
                if result.returncode:
                    raise RuntimeError('Cannot stop template container: ' + result.stderr.strip())


def probe_running(req):
    query = ('nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,'
             'utilization.gpu --format=csv,noheader,nounits')
    result = run(docker_command(req, query))
    if result.returncode:
        raise RuntimeError((result.stderr + result.stdout).strip())
    apps = run(docker_command(req, 'nvidia-smi --query-compute-apps=gpu_uuid,pid,used_gpu_memory '
                              '--format=csv,noheader,nounits'))
    if apps.returncode:
        raise RuntimeError('Cannot check GPU processes: ' + apps.stderr.strip())
    active = {}
    for row in csv.reader(io.StringIO(apps.stdout)):
        if not row or not row[0].strip().startswith('GPU-'):
            continue
        uuid, pid = row[0].strip(), int(row[1].strip())
        try:
            used = int(row[2].strip())
        except (IndexError, ValueError):
            used = 0
        active.setdefault(uuid, {})[pid] = used
    reserved = reservations()
    gpus = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if not row:
            continue
        index, uuid, name, total, used, util = [v.strip() for v in row]
        jobs = reserved.get(uuid, [])
        managed_pids, pending_mib = set(), 0
        for job in jobs:
            pids = set()
            if job.get('job_container'):
                top = run(['docker', 'top', job['job_container'], '-eo', 'pid'])
                if top.returncode == 0:
                    pids = {int(line.strip()) for line in top.stdout.splitlines()
                            if line.strip().isdigit()}
            matched = pids & set(active.get(uuid, {}))
            managed_pids.update(matched)
            allocated = sum(active[uuid][pid] for pid in matched)
            pending_mib += max(0, job['min_free_mib'] - allocated)
        external_pids = set(active.get(uuid, {})) - managed_pids
        slots = len(jobs) + len(external_pids)
        limit = req.get('max_jobs_per_gpu', 1)
        margin = req.get('gpu_memory_headroom_mib', 0)
        gpu = dict(index=int(index), uuid=uuid, name=name, total_mib=int(total),
                   used_mib=int(used), utilization=int(util),
                   reserved_by=jobs[0]['id'] if jobs else None,
                   compute_process=bool(active.get(uuid)), occupied_slots=slots,
                   max_jobs=limit, external_processes=len(external_pids),
                   pending_mib=pending_mib,
                   available_mib=max(0, int(total) - int(used) - pending_mib - margin))
        gpu['idle'] = (not gpu['reserved_by'] and not gpu['compute_process']
                       and gpu['used_mib'] <= req['idle_max_used_mib']
                       and gpu['utilization'] <= req['idle_max_utilization'])
        gpu['accepting_jobs'] = (gpu['idle'] if limit == 1 else
                                 slots < limit and gpu['available_mib'] > 0
                                 and not any(j['status'] == 'unknown' for j in jobs))
        gpus.append(gpu)
    return {'gpus': gpus}


def runtime_image(req, template):
    if req.get('runtime_image'):
        return req['runtime_image']
    image = 'job-scheduler-runtime:' + str(os.getuid()) + '-' + template['Id'].split(':')[-1][:12]
    with (root_dir() / 'runtime.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if run(['docker', 'image', 'inspect', image]).returncode:
            message = 'Preparing reusable runtime image: ' + image
            print(message, flush=True)
            if job_path(req['id']).is_dir():
                with (job_path(req['id']) / 'output.log').open('a') as log:
                    log.write(message + '\n')
            result = run(['docker', 'commit', '--pause=false', req['container'], image], timeout=1800)
            if result.returncode:
                raise RuntimeError('Runtime snapshot failed: ' + result.stderr.strip())
    return image


def job_command(req):
    with template_lock():
        return job_command_locked(req)


def job_command_locked(req):
    inspected = run(['docker', 'inspect', req['container']])
    if inspected.returncode:
        raise RuntimeError(inspected.stderr.strip())
    template = json.loads(inspected.stdout)[0]
    image = runtime_image(req, template)
    args = ['docker', 'run', '--rm', '--init', '--name', req['job_container'],
            '--label', 'job-scheduler.id=' + req['id'],
            '--user', f'{os.getuid()}:{os.getgid()}', '--workdir', req['workdir'],
            '--entrypoint', 'bash']
    # docker commit preserves Compose ownership labels. Neutralize them at run
    # time, including for existing cached/custom images, so Compose operations
    # on the template project cannot select and stop our independent jobs.
    compose_labels = {key for key in (template['Config'].get('Labels') or {})
                      if key.startswith('com.docker.compose.')}
    compose_labels.update(('com.docker.compose.project', 'com.docker.compose.service'))
    for key in sorted(compose_labels):
        args += ['--label', key + '=']
    for env in template['Config'].get('Env') or []:
        args += ['--env', env]
    for env in (f'CUDA_VISIBLE_DEVICES={req["gpu"]}', 'CUDA_DEVICE_ORDER=PCI_BUS_ID',
                'PYTHONUNBUFFERED=1', f'JOB_SCHEDULER_ID={req["id"]}'):
        args += ['--env', env]
    for mount in template.get('Mounts', []):
        kind = mount['Type']
        if kind not in ('bind', 'volume'):
            continue
        source = mount['Source'] if kind == 'bind' else mount['Name']
        if ',' in source or ',' in mount['Destination']:
            raise ValueError('Mount paths containing commas are unsupported')
        value = f'type={kind},src={source},dst={mount["Destination"]}'
        if not mount.get('RW', True):
            value += ',readonly'
        args += ['--mount', value]
    config = template.get('HostConfig', {})
    for device in config.get('Devices') or []:
        args += ['--device', device['PathOnHost'] + ':' + device['PathInContainer'] + ':' +
                 device.get('CgroupPermissions', 'rwm')]
    if config.get('DeviceRequests'):
        args += ['--gpus', 'device=' + req['gpu']]
    if config.get('ShmSize'):
        args += ['--shm-size', str(config['ShmSize'])]
    return args + [image, '-o', 'pipefail', '-c', driver_env() + req['command']]


def owned_container(req):
    result = run(['docker', 'inspect', req['job_container']])
    if result.returncode:
        if 'no such' in result.stderr.lower():
            return False
        raise RuntimeError('Cannot inspect job container: ' + result.stderr.strip())
    info = json.loads(result.stdout)[0]
    if (info['Config'].get('Labels') or {}).get('job-scheduler.id') != req['id']:
        raise RuntimeError('Refusing to touch a container with a different job label')
    return True


def remove_container(req):
    if not owned_container(req):
        return
    result = run(['docker', 'rm', '--force', req['job_container']])
    if result.returncode and 'no such' not in result.stderr.lower():
        raise RuntimeError('Container cleanup failed: ' + result.stderr.strip())


def launch(req):
    if not req.get('shared_queue'):
        return launch_local(req)
    with shared_lock() as directory:
        path = directory / (req['id'] + '.json')
        job = shared_record(path)
        if job['status'] in ('succeeded', 'failed', 'cancelled'):
            return {'redirect': job}
        if job.get('host') and job['host'] != req['host']:
            return {'redirect': job}
        if job.get('host') and (job_path(req['id']) / 'status.json').exists():
            return {'redirect': job}
        if job.get('cancel_requested'):
            return {'redirect': job}
        submitted = job['created']
        before = dict(job)
        for field in ('host', 'gpu', 'gpu_index', 'gpu_name'):
            if field in req.get('client_job', {}):
                job[field] = req['client_job'][field]
        job.update(status='dispatching', host=req['host'], gpu=req['gpu'],
                   remote_dir=str(job_path(req['id'])))
        save(path, job)  # Claim globally before starting any processes.
        result = launch_local(req)
        if result['status'] == 'busy':
            for field in ('host', 'gpu', 'gpu_index', 'gpu_name', 'remote_dir'):
                before.pop(field, None)
            before['status'] = 'queued'
            save(path, before)
        else:
            job.update(result, created=submitted)
            save(path, job)
        return result


def launch_local(req):
    directory = job_path(req['id'])
    if (directory / 'status.json').exists():
        return read_job(req['id'])  # A retry after SSH disconnect is idempotent.
    gpus = probe(req)['gpus']
    candidate = next((g for g in gpus if g['uuid'] == req['gpu']), None)
    if (not candidate or not candidate['accepting_jobs'] or
            candidate['available_mib'] < req['min_free_mib']):
        return {'status': 'busy'}
    directory.mkdir(mode=0o700)
    req['session'] = 'jobs'
    req['job_container'] = 'job-scheduler-' + req['id']
    req['cancellation_protocol'] = 1
    save(directory / 'request.json', req)
    data = dict(id=req['id'], status='reserved', gpu=req['gpu'],
                session=req['session'], created=time.time())
    if req.get('host'):
        data['host'] = req['host']
    save(directory / 'status.json', data)
    # The worker remains available even if the local checkout subsequently changes.
    (directory / 'worker.py').write_text(SOURCE_CODE)
    worker_cmd = shlex.join(['python3', str(directory / 'worker.py'),
                             '--worker', req['id']])
    try:
        if tmux(['has-session', '-t', '=jobs']).returncode:
            created = tmux(['new-session', '-d', '-s', 'jobs', '-n', 'overview', 'sleep infinity'])
            if created.returncode:
                raise RuntimeError(created.stderr.strip())
        window = tmux(['new-window', '-d', '-P', '-F', '#{window_id}', '-t', '=jobs:',
                       '-n', 'job-' + req['id'][:8], 'sleep infinity'])
        if window.returncode:
            raise RuntimeError(window.stderr.strip())
        req['window_id'] = data['window_id'] = window.stdout.strip()
        data['job_container'] = req['job_container']
        save(directory / 'request.json', req)
        save(directory / 'status.json', data)
        tmux(['set-window-option', '-t', data['window_id'], 'remain-on-exit', 'off'])
        result = tmux(['respawn-pane', '-k', '-t', data['window_id'], worker_cmd])
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
    except Exception as exc:
        data.update(status='failed', exit_code=None, error=str(exc))
        finish_job(directory, data)
    return data


def cancel(job_id, allow_missing=False):
    directory = job_path(job_id)
    data = read_job(job_id)
    if data['status'] == 'missing':
        if not allow_missing:
            raise RuntimeError('Remote record is missing; inspect the container before stopping it')
        # A delayed launch request with this ID must never start after cancel.
        directory.mkdir(mode=0o700, exist_ok=True)
        data = dict(id=job_id, status='cancelled', finished=time.time(), exit_code=None)
        save(directory / 'status.json', data)
        return data
    if data['status'] in ('succeeded', 'failed', 'cancelled'):
        return data
    req = json.loads((directory / 'request.json').read_text())
    if req.get('cancellation_protocol') != 1:
        raise RuntimeError('This job predates cancellation support; inspect it manually')
    if data['status'] == 'unknown':
        raise RuntimeError('Worker state is unknown; inspect the container before stopping it')
    (directory / 'cancel.request').touch()
    return read_job(job_id)


def watch_cancel(req, directory, stopped):
    while not stopped.wait(0.2):
        if not (directory / 'cancel.request').exists():
            continue
        try:
            if req.get('job_container'):
                if not owned_container(req):
                    continue
                result = run(['docker', 'stop', '--time', '3', req['job_container']])
            else:
                command = shlex.join(['python3', '-c', CONTAINER_CANCEL, req['id']])
                result = run(docker_command(req, command))
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass


def finish_job(directory, data):
    if data['status'] in ('succeeded', 'failed', 'cancelled'):
        try:
            req = json.loads((directory / 'request.json').read_text())
            if req.get('job_container'):
                stop_template(req)
        except Exception as exc:
            # Template cleanup must not change the job's actual exit result.
            data['template_cleanup_error'] = str(exc)
    # Persist the result first: killing our own tmux session can terminate us.
    save(directory / 'status.json', data)
    if data['status'] in ('succeeded', 'failed', 'cancelled'):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if data.get('window_id'):
                tmux(['kill-window', '-t', data['window_id']])
            else:
                run(['tmux', 'kill-session', '-t', '=' + data['session']])


def worker(job_id):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    directory = job_path(job_id)
    req = json.loads((directory / 'request.json').read_text())
    data = json.loads((directory / 'status.json').read_text())
    if (directory / 'cancel.request').exists():
        data.update(status='cancelled', finished=time.time(), exit_code=None)
        finish_job(directory, data)
        return
    data.update(status='running', started=time.time())
    save(directory / 'status.json', data)
    process = None
    stopped = threading.Event()
    try:
        # Output is visible in tmux and also retained after the session exits.
        if req.get('job_container'):
            command = job_command(req)
            if (directory / 'cancel.request').exists():
                raise CancelledBeforeStart()
        else:
            supervised = shlex.join(['python3', '-u', '-c', CONTAINER_RUNNER,
                                     req['id'], req['command']])
            command = docker_command(req, 'exec ' + supervised, req['gpu'])
        process = subprocess.Popen(command,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=watch_cancel, args=(req, directory, stopped), daemon=True).start()
        with (directory / 'output.log').open('ab', buffering=0) as log:
            while True:
                chunk = process.stdout.read1(65536)
                if not chunk:
                    break
                log.write(chunk)
                with contextlib.suppress(OSError):
                    os.write(sys.stdout.fileno(), chunk)
        code = process.wait()
        state = 'succeeded' if code == 0 else 'failed'
        if code != 0 and (directory / 'cancel.request').exists():
            state = 'cancelled'
        data.update(status=state, exit_code=code)
    except CancelledBeforeStart:
        data.update(status='cancelled', exit_code=None)
    except Exception as exc:
        # A logging/worker error does not prove the container command stopped.
        # Keep the reservation rather than risk starting another job on this GPU.
        data.update(status='unknown' if process is not None else 'failed',
                    error=str(exc), exit_code=None)
    finally:
        stopped.set()
        if req.get('job_container'):
            try:
                remove_container(req)
            except Exception as exc:
                data.update(status='unknown', cleanup_error=str(exc))
    data['finished'] = time.time()
    finish_job(directory, data)


def main(req):
    root_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    # All copies of this tool using the same SSH account coordinate here.
    with (root_dir() / 'dispatch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = req['action']
        if action == 'probe':
            return probe(req)
        if action == 'shared_sync':
            return shared_sync(req.get('jobs', []))
        if action == 'shared_cancel':
            return shared_cancel(req['id'])
        if action == 'launch':
            return launch(req)
        if action == 'status':
            return read_job(req['id'])
        if action == 'status_batch':
            results = []
            for request in req['requests']:
                try:
                    if request['action'] == 'cancel':
                        result = cancel(request['id'], request.get('allow_missing', False))
                    elif request['action'] == 'status':
                        result = read_job(request['id'])
                    else:
                        raise ValueError('Invalid status batch action')
                except Exception as exc:
                    result = {'error': str(exc)}
                results.append(result)
            return {'jobs': results}
        if action == 'cancel':
            return cancel(req['id'], req.get('allow_missing', False))
        if action == 'logs_batch':
            return {'jobs': [dict(read_job(job_id), log=read_log(job_id, req.get('lines', 80)))
                             for job_id in req['ids']]}
        if action == 'logs':
            return {'log': read_log(req['id'], req.get('lines', 80))}
        raise ValueError('Unknown action')


def read_log(job_id, lines):
    path = job_path(job_id) / 'output.log'
    if not path.exists():
        return ''
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 256 * 1024))
        tail = stream.read().decode('utf-8', errors='replace')
    return '\n'.join(tail.splitlines()[-lines:])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(sys.argv[2])
    else:
        try:
            print(json.dumps(main(json.load(sys.stdin)), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
