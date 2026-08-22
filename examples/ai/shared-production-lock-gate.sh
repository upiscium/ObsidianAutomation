#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${AI_ROOT:?set AI_ROOT to the disposable AI state root}"
REPO_ROOT=${REPO_ROOT:-"$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"}

EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}
SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
READER_USER=${READER_USER:-obsidian-ai-reader}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}

LOCK_DIR="$AI_ROOT/24-Locks"
DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
LOCK_PATH="$LOCK_DIR/$DIGEST.lock"

[[ -d "$LOCK_DIR" && ! -L "$LOCK_DIR" ]] || {
  echo "ERROR: unsafe or missing lock directory: $LOCK_DIR" >&2
  exit 1
}
[[ -d "$REPO_ROOT/src/obsidian_automation" ]] || {
  echo "ERROR: repository source tree not found: $REPO_ROOT" >&2
  exit 1
}

for command in runuser python3 getfacl; do
  command -v "$command" >/dev/null || {
    echo "ERROR: required command not found: $command" >&2
    exit 1
  }
done

cleanup() {
  rm -f -- "$LOCK_PATH"
}
trap cleanup EXIT
cleanup

failures=0
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }

probe_lock() {
  local user=$1 expected=$2 label=$3
  if runuser -u "$user" -- env \
    PYTHONPATH="$REPO_ROOT/src" \
    python3 - "$LOCK_DIR" "$DIGEST" <<'PY'
import sys
from pathlib import Path

from obsidian_automation.production_orchestrator import _production_lock

with _production_lock(Path(sys.argv[1]), sys.argv[2]):
    pass
PY
  then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected lock open succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected lock open failed)"
  fi
}

# The first actor creates the per-mutation lock. The next two actors must be
# able to open that exact inode; this reproduces the production handoff from
# Executor -> Sync Transport -> Human recovery.
probe_lock "$EXECUTOR_USER" allow "Executor creates production mutation lock"

[[ -f "$LOCK_PATH" && ! -L "$LOCK_PATH" ]] || {
  echo "ERROR: Executor did not create a safe lock file" >&2
  exit 1
}

# Keep the ACL in CI output because a mode such as 0600 can silently mask the
# inherited named-user entries even when the parent directory ACL is correct.
getfacl -p "$LOCK_PATH"

probe_lock "$SYNC_USER" allow "Sync opens Executor-created production mutation lock"
probe_lock "$REVIEWER_USER" allow "Reviewer opens Executor-created production mutation lock"

# Shared operational lock access must not leak to semantic-only identities.
probe_lock "$VALIDATOR_USER" deny "Validator cannot open production mutation lock"
probe_lock "$READER_USER" deny "Reader cannot open production mutation lock"
probe_lock "$GENERATOR_USER" deny "Generator cannot open production mutation lock"

if (( failures != 0 )); then
  echo "Shared production lock Gate FAILED: $failures probe(s) failed." >&2
  exit 1
fi

echo "Shared production lock Gate PASSED."
