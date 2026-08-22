#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${VAULT_ROOT:?set VAULT_ROOT to a disposable Vault root}"
AI_ROOT=${AI_ROOT:-"$VAULT_ROOT/20-AI"}

SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
READER_USER=${READER_USER:-obsidian-ai-reader}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}

VAULT_MARKER="$VAULT_ROOT/.obsidian-ai-disposable-fixture"
DEFAULT_AI_ROOT="$VAULT_ROOT/20-AI"
STATE_MARKER="$AI_ROOT/.obsidian-ai-disposable-state"
KNOWLEDGE="$VAULT_ROOT/11-Knowledge"
UNTRUSTED="$AI_ROOT/00-Untrusted"
VALIDATION="$AI_ROOT/10-Validation"
REVIEW="$AI_ROOT/20-Review"
EXECUTION="$AI_ROOT/25-Execution"
TRANSPORT="$AI_ROOT/27-Transport"
RECEIPTS="$AI_ROOT/30-Receipts"

if [[ ! -f "$VAULT_MARKER" ]]; then
  echo "ERROR: refusing permission probes without $VAULT_MARKER" >&2
  exit 1
fi
if [[ "$AI_ROOT" != "$DEFAULT_AI_ROOT" && ! -f "$STATE_MARKER" ]]; then
  echo "ERROR: refusing separate state probes without $STATE_MARKER" >&2
  exit 1
fi

for command in runuser id getfacl; do
  command -v "$command" >/dev/null || {
    echo "ERROR: required command not found: $command" >&2
    exit 1
  }
done

for user in \
  "$SYNC_USER" \
  "$READER_USER" \
  "$GENERATOR_USER" \
  "$VALIDATOR_USER" \
  "$REVIEWER_USER" \
  "$EXECUTOR_USER"; do
  id "$user" >/dev/null 2>&1 || {
    echo "ERROR: required user does not exist: $user" >&2
    exit 1
  }
done

for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  [[ -d "$directory" && ! -L "$directory" ]] || {
    echo "ERROR: unsafe or missing fixture directory: $directory" >&2
    exit 1
  }
done

failures=0
created=()

cleanup() {
  local path
  for path in "${created[@]}"; do
    rm -f -- "$path" || true
  done
}
trap cleanup EXIT

pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }

probe_write() {
  local user=$1 directory=$2 expected=$3 label=$4
  local path="$directory/.authority-gate-write-${$}-${RANDOM}"
  created+=("$path")
  if runuser -u "$user" -- sh -c 'printf "gate\n" > "$1" && rm -f -- "$1"' sh "$path" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected write succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected write failed)"
  fi
}

create_seed() {
  local user=$1 path=$2
  created+=("$path")
  runuser -u "$user" -- sh -c 'printf "authority-gate-seed\n" > "$1"' sh "$path"
}

probe_read() {
  local user=$1 path=$2 expected=$3 label=$4
  if runuser -u "$user" -- cat -- "$path" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected read succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected read failed)"
  fi
}

knowledge_seed="$KNOWLEDGE/.authority-gate-knowledge"
untrusted_seed="$UNTRUSTED/.authority-gate-untrusted"
validation_seed="$VALIDATION/.authority-gate-validation"
review_seed="$REVIEW/.authority-gate-review"
execution_seed="$EXECUTION/.authority-gate-execution"
transport_seed="$TRANSPORT/.authority-gate-transport"
receipts_seed="$RECEIPTS/.authority-gate-receipt"

create_seed "$SYNC_USER" "$knowledge_seed"
create_seed "$GENERATOR_USER" "$untrusted_seed"
create_seed "$VALIDATOR_USER" "$validation_seed"
create_seed "$REVIEWER_USER" "$review_seed"
create_seed "$EXECUTOR_USER" "$execution_seed"
create_seed "$SYNC_USER" "$transport_seed"
create_seed "$EXECUTOR_USER" "$receipts_seed"

# Positive reads.
probe_read "$READER_USER" "$knowledge_seed" allow "Reader reads canonical Knowledge"
probe_read "$GENERATOR_USER" "$untrusted_seed" allow "Generator reads Untrusted"
probe_read "$VALIDATOR_USER" "$untrusted_seed" allow "Validator reads Untrusted"
probe_read "$VALIDATOR_USER" "$knowledge_seed" allow "Validator reads canonical Knowledge"
probe_read "$REVIEWER_USER" "$validation_seed" allow "Reviewer reads Validation"
probe_read "$REVIEWER_USER" "$receipts_seed" allow "Reviewer reads Receipts"
probe_read "$EXECUTOR_USER" "$validation_seed" allow "Executor reads Validation"
probe_read "$EXECUTOR_USER" "$review_seed" allow "Executor reads Review"
probe_read "$EXECUTOR_USER" "$transport_seed" allow "Executor reads Transport result"
probe_read "$SYNC_USER" "$validation_seed" allow "Sync reads Validation"
probe_read "$SYNC_USER" "$review_seed" allow "Sync reads Review"
probe_read "$SYNC_USER" "$execution_seed" allow "Sync reads Execution request"

# Negative reads protecting trust boundaries.
probe_read "$GENERATOR_USER" "$knowledge_seed" deny "Generator cannot read canonical Knowledge directly"
probe_read "$GENERATOR_USER" "$validation_seed" deny "Generator cannot read Validation"
probe_read "$READER_USER" "$untrusted_seed" deny "Reader has no AI state access"
probe_read "$REVIEWER_USER" "$knowledge_seed" deny "Reviewer has no canonical Knowledge access"
probe_read "$REVIEWER_USER" "$execution_seed" deny "Reviewer cannot read Execution journal"
probe_read "$REVIEWER_USER" "$transport_seed" deny "Reviewer cannot read Transport journal"
probe_read "$EXECUTOR_USER" "$untrusted_seed" deny "Executor cannot read Untrusted proposals directly"
probe_read "$SYNC_USER" "$untrusted_seed" deny "Sync cannot read Untrusted proposals"
probe_read "$SYNC_USER" "$receipts_seed" deny "Sync cannot read Receipts"

# Positive writes: exactly one writer authority per stage, except the Vault mirror owned by Sync.
probe_write "$SYNC_USER" "$KNOWLEDGE" allow "Sync writes local Vault mirror"
probe_write "$GENERATOR_USER" "$UNTRUSTED" allow "Generator writes Untrusted"
probe_write "$VALIDATOR_USER" "$VALIDATION" allow "Validator writes Validation"
probe_write "$REVIEWER_USER" "$REVIEW" allow "Reviewer writes Review"
probe_write "$EXECUTOR_USER" "$EXECUTION" allow "Executor writes Execution request"
probe_write "$SYNC_USER" "$TRANSPORT" allow "Sync writes Transport result"
probe_write "$EXECUTOR_USER" "$RECEIPTS" allow "Executor writes Receipts"

# Reader is read-only everywhere.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$READER_USER" "$directory" deny "Reader denied write: ${directory}"
done

# Generator writes only Untrusted.
for directory in "$KNOWLEDGE" "$VALIDATION" "$REVIEW" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$GENERATOR_USER" "$directory" deny "Generator denied write: ${directory}"
done

# Validator writes only Validation.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$REVIEW" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$VALIDATOR_USER" "$directory" deny "Validator denied write: ${directory}"
done

# Reviewer writes only Review.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$REVIEWER_USER" "$directory" deny "Reviewer denied write: ${directory}"
done

# Executor cannot write the mirror or forge earlier stages / transport attestation.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$TRANSPORT"; do
  probe_write "$EXECUTOR_USER" "$directory" deny "Executor denied write: ${directory}"
done

# Sync owns the mirror and Transport only; it cannot forge semantic authority stages or Receipts.
for directory in "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
  probe_write "$SYNC_USER" "$directory" deny "Sync denied write: ${directory}"
done

if (( failures != 0 )); then
  echo "Authority separation Gate FAILED: $failures probe(s) failed." >&2
  exit 1
fi

echo "Authority separation Gate PASSED."
