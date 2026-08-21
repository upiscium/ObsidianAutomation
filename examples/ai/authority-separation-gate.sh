#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${VAULT_ROOT:?set VAULT_ROOT to a disposable Vault root}"

SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
READER_USER=${READER_USER:-obsidian-ai-reader}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}

MARKER="$VAULT_ROOT/.obsidian-ai-disposable-fixture"
AI_ROOT="$VAULT_ROOT/20-AI"
KNOWLEDGE="$VAULT_ROOT/11-Knowledge"
UNTRUSTED="$AI_ROOT/00-Untrusted"
VALIDATION="$AI_ROOT/10-Validation"
REVIEW="$AI_ROOT/20-Review"
EXECUTION="$AI_ROOT/25-Execution"
RECEIPTS="$AI_ROOT/30-Receipts"

if [[ ! -f "$MARKER" ]]; then
  echo "ERROR: refusing permission probes without $MARKER" >&2
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

for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
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

pass() {
  printf 'PASS: %s\n' "$1"
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  failures=$((failures + 1))
}

probe_write() {
  local user=$1
  local directory=$2
  local expected=$3
  local label=$4
  local path="$directory/.authority-gate-write-${$}-${RANDOM}"
  created+=("$path")

  if runuser -u "$user" -- sh -c 'printf "gate\n" > "$1" && rm -f -- "$1"' sh "$path" >/dev/null 2>&1; then
    if [[ $expected == allow ]]; then
      pass "$label"
    else
      fail "$label (unexpected write succeeded)"
    fi
  else
    if [[ $expected == deny ]]; then
      pass "$label"
    else
      fail "$label (expected write failed)"
    fi
  fi
}

create_seed() {
  local path=$1
  created+=("$path")
  runuser -u "$SYNC_USER" -- sh -c 'printf "authority-gate-seed\n" > "$1"' sh "$path"
}

probe_read() {
  local user=$1
  local path=$2
  local expected=$3
  local label=$4

  if runuser -u "$user" -- cat -- "$path" >/dev/null 2>&1; then
    if [[ $expected == allow ]]; then
      pass "$label"
    else
      fail "$label (unexpected read succeeded)"
    fi
  else
    if [[ $expected == deny ]]; then
      pass "$label"
    else
      fail "$label (expected read failed)"
    fi
  fi
}

knowledge_seed="$KNOWLEDGE/.authority-gate-knowledge"
untrusted_seed="$UNTRUSTED/.authority-gate-untrusted"
validation_seed="$VALIDATION/.authority-gate-validation"
review_seed="$REVIEW/.authority-gate-review"
execution_seed="$EXECUTION/.authority-gate-execution"
receipts_seed="$RECEIPTS/.authority-gate-receipt"

create_seed "$knowledge_seed"
create_seed "$untrusted_seed"
create_seed "$validation_seed"
create_seed "$review_seed"
create_seed "$execution_seed"
create_seed "$receipts_seed"

# Positive read capabilities.
probe_read "$READER_USER" "$knowledge_seed" allow "Reader reads canonical Knowledge"
probe_read "$GENERATOR_USER" "$untrusted_seed" allow "Generator reads Untrusted"
probe_read "$VALIDATOR_USER" "$untrusted_seed" allow "Validator reads Untrusted"
probe_read "$VALIDATOR_USER" "$knowledge_seed" allow "Validator reads canonical Knowledge"
probe_read "$REVIEWER_USER" "$validation_seed" allow "Reviewer reads Validation"
probe_read "$REVIEWER_USER" "$receipts_seed" allow "Reviewer reads Receipts"
probe_read "$EXECUTOR_USER" "$validation_seed" allow "Executor reads Validation"
probe_read "$EXECUTOR_USER" "$review_seed" allow "Executor reads Review"

# Negative read capabilities that materially protect the canonical Vault or stage boundaries.
probe_read "$GENERATOR_USER" "$knowledge_seed" deny "Generator cannot read canonical Knowledge directly"
probe_read "$GENERATOR_USER" "$validation_seed" deny "Generator cannot read Validation"
probe_read "$READER_USER" "$untrusted_seed" deny "Reader has no direct Untrusted access"
probe_read "$REVIEWER_USER" "$knowledge_seed" deny "Reviewer has no canonical Knowledge access via AI host"
probe_read "$REVIEWER_USER" "$execution_seed" deny "Reviewer cannot read Execution journal"
probe_read "$EXECUTOR_USER" "$untrusted_seed" deny "Executor cannot read Untrusted proposals directly"

# Positive write capabilities.
probe_write "$GENERATOR_USER" "$UNTRUSTED" allow "Generator writes Untrusted only"
probe_write "$VALIDATOR_USER" "$VALIDATION" allow "Validator writes Validation"
probe_write "$REVIEWER_USER" "$REVIEW" allow "Reviewer writes Review"
probe_write "$EXECUTOR_USER" "$EXECUTION" allow "Executor writes Execution journal"
probe_write "$EXECUTOR_USER" "$RECEIPTS" allow "Executor writes Receipts"
probe_write "$EXECUTOR_USER" "$KNOWLEDGE" allow "Executor writes canonical Knowledge"

# Generator negative writes.
for directory in "$KNOWLEDGE" "$VALIDATION" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
  probe_write "$GENERATOR_USER" "$directory" deny "Generator denied write: ${directory#"$VAULT_ROOT/"}"
done

# Validator negative writes.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
  probe_write "$VALIDATOR_USER" "$directory" deny "Validator denied write: ${directory#"$VAULT_ROOT/"}"
done

# Reviewer negative writes.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$EXECUTION" "$RECEIPTS"; do
  probe_write "$REVIEWER_USER" "$directory" deny "Reviewer denied write: ${directory#"$VAULT_ROOT/"}"
done

# Reader must remain write-free.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
  probe_write "$READER_USER" "$directory" deny "Reader denied write: ${directory#"$VAULT_ROOT/"}"
done

# Executor cannot forge earlier lifecycle stages.
for directory in "$UNTRUSTED" "$VALIDATION" "$REVIEW"; do
  probe_write "$EXECUTOR_USER" "$directory" deny "Executor denied write: ${directory#"$VAULT_ROOT/"}"
done

# Sync transport is intentionally broad and must remain isolated from LLM-facing processes.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
  probe_write "$SYNC_USER" "$directory" allow "Sync transport writes: ${directory#"$VAULT_ROOT/"}"
done

if (( failures != 0 )); then
  echo "Authority separation Gate FAILED: $failures probe(s) failed." >&2
  exit 1
fi

echo "Authority separation Gate PASSED."
