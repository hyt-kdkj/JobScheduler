#!/usr/bin/env python3
"""Prepare user-owned NVIDIA libraries and host-specific V2V Compose overrides.

Run on a compute host. Does not install packages or change the host driver.
Template scripts are backed up before editing; containers are not started here.
"""
import argparse
from datetime import datetime
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request


PACKAGES = {
    '580.173.02': (
        ('libnvidia-compute-580_580.173.02-1ubuntu1_amd64.deb',
         'bc06b7517819f2fa4a18811272987ab16b4c8f3d4ae555731b7f4c09695d7dd6'),
        ('libnvidia-gpucomp-580_580.173.02-1ubuntu1_amd64.deb',
         'e5e56a8f46be2e328c9b1f40680c9c027ee4ad6aa73b051a46eaec2bd3654464'),
    ),
}
BASE_URL = 'https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/'


def install_libraries(version):
    destination = Path.home() / 'workspace/.job-scheduler' / ('nvidia-driver-' + version)
    if (destination / 'lib/libcuda.so.1').is_file():
        print('Reusing libraries:', destination, flush=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        staging = Path(staging)
        for filename, digest in PACKAGES[version]:
            package = staging / filename
            print('Downloading:', filename, flush=True)
            with urllib.request.urlopen(BASE_URL + filename, timeout=60) as response, package.open('wb') as out:
                shutil.copyfileobj(response, out)
            if hashlib.sha256(package.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Package checksum mismatch: ' + filename)
            subprocess.run(['dpkg-deb', '--extract', str(package), str(staging / 'root')], check=True)
        libraries = staging / 'root/usr/lib/x86_64-linux-gnu'
        for name in ('libcuda.so.1', 'libnvidia-ml.so.1', 'libnvidia-ptxjitcompiler.so.1',
                     'libnvidia-nvvm.so.4', 'libnvidia-gpucomp.so.' + version):
            if not (libraries / name).is_file():
                raise RuntimeError('Missing extracted library: ' + name)
        # Publish only after all packages and required files have been verified.
        prepared = staging / 'prepared'
        prepared.mkdir()
        shutil.copytree(libraries, prepared / 'lib', symlinks=True)
        (prepared / 'SOURCE.txt').write_text(BASE_URL + '\n' + repr(PACKAGES[version]) + '\n')
        prepared.rename(destination)
    print('Prepared libraries:', destination, flush=True)


def patch_script(source):
    if '# job-scheduler: host-specific GPU environment' in source:
        return source.replace('${HOME}/.local/share/job-scheduler/nvidia-driver-',
                              '${HOME}/workspace/.job-scheduler/nvidia-driver-')
    old = 'GPU_COMPOSE_FILE=".docker-compose.gpu.generated.yml"'
    if source.count(old) != 1:
        raise ValueError('Unrecognized GPU_COMPOSE_FILE setting')
    source = source.replace(old, '# job-scheduler: host-specific GPU environment', 1)
    anchor = 'cd "${SCRIPT_DIR}"\n'
    if source.count(anchor) != 1:
        raise ValueError('Unrecognized script directory setup')
    source = source.replace(anchor, anchor + '''
# HOME is shared by the compute hosts. Never share generated device mappings.
GPU_COMPOSE_FILE="${XDG_CACHE_HOME:-${HOME}/.cache}/job-scheduler/$(hostname)/$(basename "${SCRIPT_DIR}").gpu.yml"
mkdir -p "$(dirname "${GPU_COMPOSE_FILE}")"
''', 1)
    anchor = '  local device\n  local lib\n'
    if source.count(anchor) != 1:
        raise ValueError('Unrecognized GPU library setup')
    source = source.replace(anchor, anchor + '''
  local driver_version
  local isolated_lib
  driver_version="$(sed -nE 's/.*Kernel Module[[:space:]]+([0-9]+\\.[0-9]+\\.[0-9]+).*/\\1/p' /proc/driver/nvidia/version)"
  isolated_lib="${HOME}/workspace/.job-scheduler/nvidia-driver-${driver_version}/lib"
  if [[ -f "${isolated_lib}/libcuda.so.1" ]]; then
    gpu_libs=()
    for lib in "${isolated_lib}"/*.so*; do
      [[ -f "${lib}" ]] && gpu_libs+=("${lib}")
    done
  fi
''', 1)
    return source


def configure_script(path):
    source = path.read_text()
    updated = patch_script(source)
    if updated == source:
        print('Already configured:', path, flush=True)
        return
    subprocess.run(['bash', '-n'], input=updated, text=True, check=True)
    backup = path.with_name(path.name + '.bak.' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    shutil.copy2(path, backup)
    path.write_text(updated)
    print('Updated:', path, 'Backup:', backup, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-driver-version', choices=PACKAGES)
    parser.add_argument('scripts', nargs='*', type=Path)
    args = parser.parse_args()
    if args.install_driver_version:
        install_libraries(args.install_driver_version)
    for path in args.scripts:
        configure_script(path.expanduser())


if __name__ == '__main__':
    main()
