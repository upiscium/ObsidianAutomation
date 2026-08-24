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
EVALUATOR_USER=${EVALUATOR_USER:-obsidian-ai-evaluator}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}

VAULT_MARKER="$VAULT_ROOT/.obsidian-ai-disposable-fixture"
DEFAULT_AI_ROOT="$VAULT_ROOT/20-AI"
STATE_MARKER="$AI_ROOT/.obsidian-ai-disposable-state"
KNOWLEDGE="$VAULT_ROOT/11-Knowledge"
UNTRUSTED="$AI_ROOT/00-Untrusted"
INDEX="$AI_ROOT/04-Index"
CONTEXT="$AI_ROOT/05-Context"
VALIDATION="$AI_ROOT/10-Validation"
EVALUATION_REQUEST="$AI_ROOT/12-Evaluation-Request"
EVALUATION_CONTEXT="$AI_ROOT/14-Evaluation-Context"
EVALUATION="$AI_ROOT/15-Evaluation"
REVIEW="$AI_ROOT/20-Review"
LOCKS="$AI_ROOT/24-Locks"
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
  "$EVALUATOR_USER" \
  "$REVIEWER_USER" \
  "$EXECUTOR_USER"; do
  id "$user" >/dev/null 2>&1 || {
    echo "ERROR: required user does not exist: $user" >&2
    exit 1
  }
done

for directory in \
  "$KNOWLEDGE" "$UNTRUSTED" "$INDEX" "$CONTEXT" "$VALIDATION" \
  "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" \
  "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
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
index_seed="$INDEX/.authority-gate-index"
context_seed="$CONTEXT/.authority-gate-context"
validation_seed="$VALIDATION/.authority-gate-validation"
evaluation_request_seed="$EVALUATION_REQUEST/.authority-gate-evaluation-request"
evaluation_context_seed="$EVALUATION_CONTEXT/.authority-gate-evaluation-context"
evaluation_seed="$EVALUATION/.authority-gate-evaluation"
review_seed="$REVIEW/.authority-gate-review"
lock_seed="$LOCKS/.authority-gate-lock"
execution_seed="$EXECUTION/.authority-gate-execution"
transport_seed="$TRANSPORT/.authority-gate-transport"
receipts_seed="$RECEIPTS/.authority-gate-receipt"

create_seed "$SYNC_USER" "$knowledge_seed"
create_seed "$GENERATOR_USER" "$untrusted_seed"
create_seed "$READER_USER" "$index_seed"
create_seed "$READER_USER" "$context_seed"
create_seed "$VALIDATOR_USER" "$validation_seed"
create_seed "$VALIDATOR_USER" "$evaluation_request_seed"
create_seed "$READER_USER" "$evaluation_context_seed"
create_seed "$EVALUATOR_USER" "$evaluation_seed"
create_seed "$REVIEWER_USER" "$review_seed"
create_seed "$EXECUTOR_USER" "$lock_seed"
create_seed "$EXECUTOR_USER" "$execution_seed"
create_seed "$SYNC_USER" "$transport_seed"
create_seed "$EXECUTOR_USER" "$receipts_seed"

# Positive reads.
probe_read "$READER_USER" "$knowledge_seed" allow "Reader reads canonical Knowledge"
probe_read "$READER_USER" "$index_seed" allow "Reader reads Index"
probe_read "$READER_USER" "$evaluation_request_seed" allow "Reader reads Evaluation Request"
probe_read "$GENERATOR_USER" "$untrusted_seed" allow "Generator reads Untrusted"
probe_read "$GENERATOR_USER" "$context_seed" allow "Generator reads Context"
probe_read "$VALIDATOR_USER" "$untrusted_seed" allow "Validator reads Untrusted"
probe_read "$VALIDATOR_USER" "$knowledge_seed" allow "Validator reads canonical Knowledge"
probe_read "$VALIDATOR_USER" "$evaluation_request_seed" allow "Validator reads Evaluation Request"
probe_read "$EVALUATOR_USER" "$untrusted_seed" allow "Evaluator reads Untrusted provenance/proposal"
probe_read "$EVALUATOR_USER" "$context_seed" allow "Evaluator reads original Generator Context"
probe_read "$EVALUATOR_USER" "$validation_seed" allow "Evaluator reads accepted Validation"
probe_read "$EVALUATOR_USER" "$evaluation_context_seed" allow "Evaluator reads Evaluation Context"
probe_read "$EVALUATOR_USER" "$evaluation_seed" allow "Evaluator reads its Evaluation"
probe_read "$REVIEWER_USER" "$validation_seed" allow "Reviewer reads Validation"
probe_read "$REVIEWER_USER" "$evaluation_seed" allow "Reviewer reads Evaluation"
probe_read "$REVIEWER_USER" "$execution_seed" allow "Reviewer reads Execution request"
probe_read "$REVIEWER_USER" "$transport_seed" allow "Reviewer reads Transport result"
probe_read "$REVIEWER_USER" "$receipts_seed" allow "Reviewer reads Receipts"
probe_read "$EXECUTOR_USER" "$validation_seed" allow "Executor reads Validation"
probe_read "$EXECUTOR_USER" "$review_seed" allow "Executor reads Review"
probe_read "$EXECUTOR_USER" "$transport_seed" allow "Executor reads Transport result"
probe_read "$SYNC_USER" "$validation_seed" allow "Sync reads Validation"
probe_read "$SYNC_USER" "$review_seed" allow "Sync reads Review"
probe_read "$SYNC_USER" "$execution_seed" allow "Sync reads Execution request"

# Negative reads protecting trust boundaries.
probe_read "$GENERATOR_USER" "$knowledge_seed" deny "Generator cannot read canonical Knowledge directly"
probe_read "$GENERATOR_USER" "$index_seed" deny "Generator cannot read Index"
probe_read "$GENERATOR_USER" "$validation_seed" deny "Generator cannot read Validation"
probe_read "$READER_USER" "$untrusted_seed" deny "Reader cannot read Untrusted proposals"
probe_read "$READER_USER" "$validation_seed" deny "Reader cannot read Validation"
probe_read "$EVALUATOR_USER" "$knowledge_seed" deny "Evaluator cannot read canonical Knowledge directly"
probe_read "$EVALUATOR_USER" "$index_seed" deny "Evaluator cannot inspect Reader Index"
probe_read "$EVALUATOR_USER" "$evaluation_request_seed" deny "Evaluator cannot read Evaluation Request"
probe_read "$EVALUATOR_USER" "$review_seed" deny "Evaluator cannot read Human Review"
probe_read "$EVALUATOR_USER" "$execution_seed" deny "Evaluator cannot read Execution"
probe_read "$EVALUATOR_USER" "$transport_seed" deny "Evaluator cannot read Transport"
probe_read "$EVALUATOR_USER" "$receipts_seed" deny "Evaluator cannot read Receipts"
probe_read "$REVIEWER_USER" "$knowledge_seed" deny "Reviewer has no canonical Knowledge access"
probe_read "$EXECUTOR_USER" "$untrusted_seed" deny "Executor cannot read Untrusted proposals directly"
probe_read "$SYNC_USER" "$untrusted_seed" deny "Sync cannot read Untrusted proposals"
probe_read "$SYNC_USER" "$evaluation_seed" deny "Sync cannot read Evaluation"
probe_read "$SYNC_USER" "$receipts_seed" deny "Sync cannot read Receipts"

# Positive writes: one semantic writer per stage; Locks are deliberately shared operational state.
probe_write "$SYNC_USER" "$KNOWLEDGE" allow "Sync writes local Vault mirror"
probe_write "$READER_USER" "$INDEX" allow "Reader writes Index"
probe_write "$READER_USER" "$CONTEXT" allow "Reader writes Context"
probe_write "$READER_USER" "$EVALUATION_CONTEXT" allow "Reader writes Evaluation Context"
probe_write "$GENERATOR_USER" "$UNTRUSTED" allow "Generator writes Untrusted"
probe_write "$VALIDATOR_USER" "$VALIDATION" allow "Validator writes Validation"
probe_write "$VALIDATOR_USER" "$EVALUATION_REQUEST" allow "Validator writes Evaluation Request"
probe_write "$EVALUATOR_USER" "$EVALUATION" allow "Evaluator writes Evaluation"
probe_write "$REVIEWER_USER" "$REVIEW" allow "Reviewer writes Review / recovery"
probe_write "$SYNC_USER" "$LOCKS" allow "Sync writes shared operational Locks"
probe_write "$REVIEWER_USER" "$LOCKS" allow "Reviewer writes shared operational Locks"
probe_write "$EXECUTOR_USER" "$LOCKS" allow "Executor writes shared operational Locks"
probe_write "$EXECUTOR_USER" "$EXECUTION" allow "Executor writes Execution request"
probe_write "$SYNC_USER" "$TRANSPORT" allow "Sync writes Transport result"
probe_write "$EXECUTOR_USER" "$RECEIPTS" allow "Executor writes Receipts"

# Reader writes only derived retrieval state and cannot write semantic authority stages.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION" "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$READER_USER" "$directory" deny "Reader denied write: ${directory}"
done

# Generator writes only Untrusted.
for directory in "$KNOWLEDGE" "$INDEX" "$CONTEXT" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$GENERATOR_USER" "$directory" deny "Generator denied write: ${directory}"
done

# Validator writes Validation and Evaluation Request only.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$INDEX" "$CONTEXT" "$EVALUATION_CONTEXT" "$EVALUATION" "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$VALIDATOR_USER" "$directory" deny "Validator denied write: ${directory}"
done

# Evaluator writes only advisory Evaluation.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$INDEX" "$CONTEXT" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$EVALUATOR_USER" "$directory" deny "Evaluator denied write: ${directory}"
done

# Reviewer writes Review and Locks, but not canonical or machine-produced stages.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$INDEX" "$CONTEXT" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$REVIEWER_USER" "$directory" deny "Reviewer denied write: ${directory}"
done

# Executor cannot write the mirror or forge earlier stages / transport attestation.
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$INDEX" "$CONTEXT" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" "$REVIEW" "$TRANSPORT"; do
  probe_write "$EXECUTOR_USER" "$directory" deny "Executor denied write: ${directory}"
done

# Sync owns the mirror and Transport only, plus non-authoritative Locks.
for directory in "$UNTRUSTED" "$INDEX" "$CONTEXT" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
  probe_write "$SYNC_USER" "$directory" deny "Sync denied write: ${directory}"
done

if (( failures != 0 )); then
  echo "Authority separation Gate FAILED: $failures probe(s) failed." >&2
  exit 1
fi

echo "Authority separation Gate PASSED."
