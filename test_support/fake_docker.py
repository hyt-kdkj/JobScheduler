#!/usr/bin/python3
"""Docker lifecycle stand-in for tests; never calls Docker or SSH."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    action, *args = sys.argv[1:]
    with open(os.environ['JOB_SCHEDULER_TEST_DOCKER_LOG'], 'a') as log:
        log.write(json.dumps(sys.argv[1:]) + '\n')
    root = Path(os.environ['JOB_SCHEDULER_TEST_DOCKER_STATE'])
    containers, images = root / 'containers', root / 'images'
    containers.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    if action == 'image':
        return 0 if (images / args[-1]).exists() else 1
    if action == 'commit':
        (images / args[-1]).touch()
        print('sha256:test-runtime')
        return 0
    if action == 'inspect':
        name = args[-1]
        if name == 'test' and (root / 'template-missing').exists() and not (root / 'template-created').exists():
            print('error: no such object: test', file=sys.stderr)
            return 1
        if name == 'test':
            info = dict(Id='a' * 64, State=dict(Running=not (root / 'template-stopped').exists()),
                        Config=dict(Env=['TEST_RUNTIME=yes'], Labels={
                            'com.docker.compose.project': 'v2vsimulator',
                            'com.docker.compose.service': 'v2v-dev',
                            'com.docker.compose.container-number': '1'}),
                        Mounts=[dict(Type='bind', Source=str(root.parent),
                                     Destination='/workspace/V2VSimulator', RW=True)],
                        HostConfig=dict(Devices=[], DeviceRequests=None))
        elif (containers / name).exists():
            info = json.loads((containers / name).read_text())
        else:
            print('error: no such object: ' + name, file=sys.stderr)
            return 1
        print(json.dumps([info]))
        return 0
    if action == 'top':
        path = containers / args[0]
        if not path.exists():
            print('error: no such container', file=sys.stderr)
            return 1
        print('PID')
        print(json.loads(path.read_text())['pid'])
        return 0
    if action == 'start' and args[-1] == 'test':
        (root / 'template-stopped').unlink(missing_ok=True)
        return 0
    if action == 'stop' and args[-1] == 'test':
        (root / 'template-stopped').touch()
        return 0
    if action in ('stop', 'rm'):
        path = containers / args[-1]
        if not path.exists():
            print('error: no such container', file=sys.stderr)
            return 1
        info = json.loads(path.read_text())
        with contextlib.suppress(ProcessLookupError):
            os.killpg(info['pid'], signal.SIGTERM if action == 'stop' else signal.SIGKILL)
        if action == 'stop':
            time.sleep(0.15)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(info['pid'], signal.SIGKILL)
        else:
            path.unlink(missing_ok=True)
        return 0
    if action not in ('exec', 'run'):
        raise ValueError(action)
    if action == 'exec' and (root / 'template-stopped').exists():
        print('template container is stopped', file=sys.stderr)
        return 1
    env, cwd, name, labels = os.environ.copy(), None, None, {}
    while args and args[0].startswith('--'):
        key = args.pop(0)
        if key in ('--rm', '--init'):
            continue
        value = args.pop(0)
        if key == '--env':
            field, value = value.split('=', 1)
            env[field] = value
        elif key == '--workdir':
            cwd = value
        elif key == '--name':
            name = value
        elif key == '--label':
            field, value = value.split('=', 1)
            labels[field] = value
    args = args[1:]  # container or runtime image
    if action == 'run':
        args.insert(0, 'bash')  # --entrypoint bash
    shell = args[-1]
    if '--query-gpu=' in shell:
        print('0, GPU-test, GTX 1080 Ti, 11264, 0, 0')
        print('1, GPU-test-2, GTX 1080 Ti, 11264, 0, 0')
        return 0
    if '--query-compute-apps=' in shell:
        return 0
    if action == 'exec':
        if cwd:
            os.chdir(cwd)
        os.execvpe(args[0], args, env)
    process = subprocess.Popen(args, env=env, cwd=cwd, start_new_session=True)
    path = containers / name
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(pid=process.pid, Config=dict(Labels=labels))))
    temp.replace(path)
    try:
        code = process.wait()
        return code if code >= 0 else 128 - code
    finally:
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    sys.exit(main())
