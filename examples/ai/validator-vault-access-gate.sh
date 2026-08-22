#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${VAULT_ROOT:?set VAULT_ROOT to the disposable Vault root}"

READER_USER=${READER_USER:-obsidian-ai-reader}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}

failures=0
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }

probe_list() {
  local user=$1 expected=$2 label=$3
  if runuser -u "$user" -- ls -A -- "$VAULT_ROOT" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected listing succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected listing failed)"
  fi
}

# validate_create_note performs case-fold collision checks while walking the
# canonical path, so deterministic Validator/Executor need root listing.
probe_list "$VALIDATOR_USER" allow "Validator can list Vault root for collision checks"
probe_list "$EXECUTOR_USER" allow "Executor can list Vault root for effect-boundary revalidation"

# These actors do not need Vault root enumeration.
probe_list "$READER_USER" deny "Reader cannot enumerate Vault root"
probe_list "$GENERATOR_USER" deny "Generator cannot enumerate Vault root"
probe_list "$REVIEWER_USER" deny "Reviewer cannot enumerate Vault root"

if (( failures != 0 )); then
  echo "Validator Vault access Gate FAILED: $failures probe(s) failed." >&2
  exit 1
fi

echo "Validator Vault access Gate PASSED."
