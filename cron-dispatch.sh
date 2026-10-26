#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
mkdir -p "$SCRIPT_DIR/state"

# cron does not inherit the interactive shell's SSH agent environment.
if [[ -z "${SSH_AUTH_SOCK:-}" || ! -S "${SSH_AUTH_SOCK:-}" ]]; then
  if [[ -r "$HOME/.ssh/agent.env" ]]; then
    source "$HOME/.ssh/agent.env" >/dev/null
  fi
fi

exec 9>"$SCRIPT_DIR/state/cron-dispatch.lock"
if ! /usr/bin/flock -n 9; then
  printf '[%s] Previous cron dispatch is still running; skipping.\n' "$(date --iso-8601=seconds)"
  exit 0
fi

printf '[%s] Scheduled dispatch started.\n' "$(date --iso-8601=seconds)"
if /usr/bin/python3 -u "$SCRIPT_DIR/scheduler.py" dispatch; then
  dispatch_status=0
else
  dispatch_status=$?
fi
printf '[%s] Scheduled dispatch finished (exit=%s).\n' "$(date --iso-8601=seconds)" "$dispatch_status"

printf '[%s] Scheduled log collection started.\n' "$(date --iso-8601=seconds)"
if /usr/bin/python3 -u "$SCRIPT_DIR/scheduler.py" logs --running --lines 1000 --output-dir logs/current; then
  logs_status=0
else
  logs_status=$?
fi
printf '[%s] Scheduled log collection finished (exit=%s).\n' "$(date --iso-8601=seconds)" "$logs_status"
if (( dispatch_status != 0 )); then
  exit "$dispatch_status"
fi
exit "$logs_status"
