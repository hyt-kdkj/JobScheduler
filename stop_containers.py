#!/usr/bin/env python3
"""Stop and retain one named container on several SSH hosts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import re
import shlex
import subprocess


DEFAULT_HOSTS = ('glockenspiel', 'marimba', 'xylophone1', 'xylophone3')
DEFAULT_CONTAINER = 'v2vsimulator-v2v-dev-1'
REMOTE_PROGRAM = '''\
import subprocess
import sys

name = sys.argv[1]
def state():
    result = subprocess.run(['docker', 'container', 'ls', '--all', '--format', '{{.Names}}\t{{.State}}'],
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Cannot list containers')
    return dict(line.split('\t', 1) for line in result.stdout.splitlines()).get(name)

try:
    initial = state()
    if initial is None:
        print('ABSENT')
    elif initial in ('exited', 'created'):
        print('ALREADY_STOPPED')
    else:
        result = subprocess.run(['docker', 'container', 'stop', '--time', '10', name],
                                text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or 'Container stop failed')
        if state() not in ('exited', 'created'):
            raise RuntimeError('Container was not retained in a stopped state')
        print('STOPPED')
except Exception as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
'''


def stop(host, container):
    command = ['ssh', '-x', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
               host, shlex.join(['python3', '-', container])]
    try:
        result = subprocess.run(command, input=REMOTE_PROGRAM, text=True,
                                capture_output=True, timeout=90)
        if result.returncode:
            return host, False, result.stderr.strip() or f'SSH exit {result.returncode}'
        status = result.stdout.strip()
        if status == 'ABSENT':
            return host, True, 'コンテナなし（処理不要）'
        if status == 'ALREADY_STOPPED':
            return host, True, '既に停止済み（コンテナは保持）'
        if status == 'STOPPED':
            return host, True, '停止完了（コンテナの保持を確認済み）'
        return host, False, '予期しない応答: ' + status[:500]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return host, False, str(exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hosts', nargs='*', default=list(DEFAULT_HOSTS),
                        help='SSH hosts (default: %(default)s)')
    parser.add_argument('--container', default=DEFAULT_CONTAINER, help='Exact container name')
    parser.add_argument('--dry-run', action='store_true', help='Show targets without SSH or stopping')
    args = parser.parse_args(argv)
    hosts = list(dict.fromkeys(args.hosts))
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host) for host in hosts):
        parser.error('Invalid SSH host alias')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.container):
        parser.error('Invalid container name')
    if args.dry_run:
        for host in hosts:
            print(f'{host}: docker container stop --time 10 {args.container}')
        return 0
    failures = 0
    with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
        for host, ok, message in pool.map(lambda host: stop(host, args.container), hosts):
            print(f'{host}: {"OK" if ok else "FAILED"}: {message}', flush=True)
            failures += not ok
    print(f'完了: 成功 {len(hosts) - failures} / 失敗 {failures}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
