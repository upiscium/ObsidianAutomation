#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${VAULT_ROOT:?set VAULT_ROOT to a disposable Vault root}"
: "${AI_ROOT:?set AI_ROOT to the disposable AI state root}"

READER_USER=${READER_USER:-obsidian-ai-reader}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}
SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}

KNOWLEDGE="$VAULT_ROOT/11-Knowledge"
CONTEXT="$AI_ROOT/05-Context"
UNTRUSTED="$AI_ROOT/00-Untrusted"

for directory in "$KNOWLEDGE" "$CONTEXT" "$UNTRUSTED"; do
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

probe_read() {
  local user=$1 path=$2 expected=$3 label=$4
  if runuser -u "$user" -- cat -- "$path" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected read succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected read failed)"
  fi
}

probe_write() {
  local user=$1 directory=$2 expected=$3 label=$4
  local path="$directory/.reader-context-gate-${$}-${RANDOM}"
  created+=("$path")
  if runuser -u "$user" -- sh -c 'printf "gate\n" > "$1" && rm -f -- "$1"' sh "$path" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected write succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected write failed)"
  fi
}

knowledge_seed="$KNOWLEDGE/.reader-context-gate-knowledge"
context_seed="$CONTEXT/.reader-context-gate-context"
untrusted_seed="$UNTRUSTED/.reader-context-gate-untrusted"
created+=("$knowledge_seed" "$context_seed" "$untrusted_seed")

runuser -u "$SYNC_USER" -- sh -c 'printf "knowledge\n" > "$1"' sh "$knowledge_seed"
runuser -u "$READER_USER" -- sh -c 'printf "context\n" > "$1"' sh "$context_seed"
runuser -u "$GENERATOR_USER" -- sh -c 'printf "proposal\n" > "$1"' sh "$untrusted_seed"

probe_read "$READER_USER" "$knowledge_seed" allow "Reader reads canonical Knowledge"
probe_read "$READER_USER" "$context_seed" allow "Reader reads its Context bundle"
probe_read "$GENERATOR_USER" "$context_seed" allow "Generator reads Reader-produced Context"
probe_read "$GENERATOR_USER" "$knowledge_seed" deny "Generator cannot bypass Reader and read Knowledge"
probe_read "$READER_USER" "$untrusted_seed" deny "Reader cannot read Generator proposals"

for user in "$SYNC_USER" "$VALIDATOR_USER" "$REVIEWER_USER" "$EXECUTOR_USER"; do
  probe_read "$user" "$context_seed" deny "$user cannot read Context"
done

probe_write "$READER_USER" "$CONTEXT" allow "Reader writes Context"
probe_write "$GENERATOR_USER" "$CONTEXT" deny "Generator cannot rewrite Context"
for user in "$SYNC_USER" "$VALIDATOR_USER" "$REVIEWER_USER" "$EXECUTOR_USER"; do
  probe_write "$user" "$CONTEXT" deny "$user cannot write Context"
done

probe_write "$READER_USER" "$UNTRUSTED" deny "Reader cannot write Untrusted proposals"
probe_write "$GENERATOR_USER" "$UNTRUSTED" allow "Generator remains sole Untrusted writer"

if (( failures != 0 )); then
  echo "Reader context boundary Gate FAILED: $failures probe(s) failed." >&2
  exit 1
fi

echo "Reader context boundary Gate PASSED."
