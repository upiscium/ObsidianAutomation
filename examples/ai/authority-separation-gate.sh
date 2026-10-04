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
EMBEDDER_USER=${EMBEDDER_USER:-obsidian-ai-embedder}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
EVALUATOR_USER=${EVALUATOR_USER:-obsidian-ai-evaluator}
STATUS_USER=${STATUS_USER:-obsidian-ai-status}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}

VAULT_MARKER="$VAULT_ROOT/.obsidian-ai-disposable-fixture"
DEFAULT_AI_ROOT="$VAULT_ROOT/20-AI"
STATE_MARKER="$AI_ROOT/.obsidian-ai-disposable-state"
DAILY="$VAULT_ROOT/00-DailyNote"
IDEAS="$VAULT_ROOT/05-Idea"
KNOWLEDGE="$VAULT_ROOT/11-Knowledge"
UNTRUSTED="$AI_ROOT/00-Untrusted"
ORCHESTRATION="$AI_ROOT/02-Orchestration"
SEMANTIC_SELECTIONS="$ORCHESTRATION/semantic-selections"
STATUS_DIR="$ORCHESTRATION/status"
INDEX="$AI_ROOT/04-Index"
SEMANTIC_CORPUS="$INDEX/semantic-corpus"
EMBEDDING_REQUESTS="$INDEX/semantic-embedding-requests"
EMBEDDING_PLANS="$INDEX/semantic-embedding-plans"
EMBEDDING_RESULTS="$INDEX/semantic-embedding-results"
EMBEDDING_RESULT_SETS="$INDEX/semantic-embedding-result-sets"
SEMANTIC_INDEX="$INDEX/semantic-index"
SEMANTIC_REFRESH_READER="$INDEX/semantic-refresh-reader"
SEMANTIC_REFRESH_EMBEDDER="$INDEX/semantic-refresh-embedder"
CONTEXT="$AI_ROOT/05-Context"
VALIDATION="$AI_ROOT/10-Validation"
EVALUATION_REQUEST="$AI_ROOT/12-Evaluation-Request"
EVALUATION_CONTEXT="$AI_ROOT/14-Evaluation-Context"
EVALUATION="$AI_ROOT/15-Evaluation"
PROJECTION="$AI_ROOT/16-Human-Projection"
PROJECTION_RESULT="$AI_ROOT/17-Human-Projection-Result"
REVIEW="$AI_ROOT/20-Review"
LOCKS="$AI_ROOT/24-Locks"
READ_VIEW_LOCKS="$LOCKS/read-view"
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
  "$EMBEDDER_USER" \
  "$GENERATOR_USER" \
  "$VALIDATOR_USER" \
  "$EVALUATOR_USER" \
  "$STATUS_USER" \
  "$REVIEWER_USER" \
  "$EXECUTOR_USER"; do
  id "$user" >/dev/null 2>&1 || {
    echo "ERROR: required user does not exist: $user" >&2
    exit 1
  }
done

for directory in \
  "$DAILY" "$IDEAS" "$KNOWLEDGE" "$UNTRUSTED" "$ORCHESTRATION" "$SEMANTIC_SELECTIONS" "$STATUS_DIR" "$INDEX" \
  "$SEMANTIC_CORPUS" "$EMBEDDING_REQUESTS" "$EMBEDDING_PLANS" "$EMBEDDING_RESULTS" "$EMBEDDING_RESULT_SETS" "$SEMANTIC_INDEX" \
  "$SEMANTIC_REFRESH_READER" "$SEMANTIC_REFRESH_EMBEDDER" \
  "$CONTEXT" "$VALIDATION" \
  "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" "$PROJECTION" "$PROJECTION_RESULT" \
  "$PROJECTION/reader" "$PROJECTION/generator" "$PROJECTION/validator" "$PROJECTION/evaluator" \
  "$PROJECTION/reviewer" "$PROJECTION/executor" "$PROJECTION/sync" \
  "$REVIEW" "$LOCKS" "$READ_VIEW_LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
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

create_control_seed() {
  create_seed "$1" "$2"
  # Match semantic_refresh._atomic_store's final mode, including ACL mask.
  runuser -u "$1" -- chmod 0660 -- "$2"
}

probe_rewrite() {
  local user=$1 path=$2 expected=$3 label=$4
  if runuser -u "$user" -- sh -c 'printf "gate\n" >> "$1"' sh "$path" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected rewrite succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected rewrite failed)"
  fi
}

probe_replace() {
  local user=$1 path=$2 writable_directory=$3 expected=$4 label=$5
  local source="$writable_directory/.authority-gate-replace-${$}-${RANDOM}"
  created+=("$source")
  # The source is created where this identity is allowed to write, so denial
  # proves that it cannot publish over the other identity's existing control.
  if runuser -u "$user" -- sh -c 'printf "gate\n" > "$1" && mv -f -- "$1" "$2"' sh "$source" "$path" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected replacement succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected replacement failed)"
  fi
}

probe_read() {
  local user=$1 path=$2 expected=$3 label=$4
  if runuser -u "$user" -- cat -- "$path" >/dev/null 2>&1; then
    [[ $expected == allow ]] && pass "$label" || fail "$label (unexpected read succeeded)"
  else
    [[ $expected == deny ]] && pass "$label" || fail "$label (expected read failed)"
  fi
}

daily_seed="$DAILY/.authority-gate-daily"
idea_seed="$IDEAS/.authority-gate-idea"
knowledge_seed="$KNOWLEDGE/.authority-gate-knowledge"
untrusted_seed="$UNTRUSTED/.authority-gate-untrusted"
index_seed="$INDEX/.authority-gate-index"
semantic_corpus_seed="$SEMANTIC_CORPUS/.authority-gate-semantic-corpus"
embedding_request_seed="$EMBEDDING_REQUESTS/.authority-gate-embedding-request"
embedding_plan_seed="$EMBEDDING_PLANS/.authority-gate-embedding-plan"
embedding_result_seed="$EMBEDDING_RESULTS/.authority-gate-embedding-result"
embedding_result_set_seed="$EMBEDDING_RESULT_SETS/.authority-gate-embedding-result-set"
semantic_index_seed="$SEMANTIC_INDEX/.authority-gate-semantic-index"
semantic_refresh_reader_seed="$SEMANTIC_REFRESH_READER/.authority-gate-refresh-reader"
semantic_refresh_embedder_seed="$SEMANTIC_REFRESH_EMBEDDER/.authority-gate-refresh-embedder"
semantic_active_seed="$INDEX/.authority-gate-active-binding"
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
status_seed="$STATUS_DIR/.authority-gate-status"
cadence_seed="$ORCHESTRATION/.authority-gate-planner-cadence"
semantic_selection_seed="$SEMANTIC_SELECTIONS/.authority-gate-semantic-selection"
projection_generator_seed="$PROJECTION/generator/.authority-gate-projection-generator"
projection_evaluator_seed="$PROJECTION/evaluator/.authority-gate-projection-evaluator"
projection_result_seed="$PROJECTION_RESULT/.authority-gate-projection-result"

create_seed "$SYNC_USER" "$daily_seed"
create_seed "$SYNC_USER" "$idea_seed"
create_seed "$SYNC_USER" "$knowledge_seed"
create_seed "$GENERATOR_USER" "$untrusted_seed"
create_seed "$READER_USER" "$index_seed"
create_seed "$READER_USER" "$semantic_corpus_seed"
create_seed "$READER_USER" "$embedding_request_seed"
create_seed "$READER_USER" "$embedding_plan_seed"
create_seed "$EMBEDDER_USER" "$embedding_result_seed"
create_seed "$EMBEDDER_USER" "$embedding_result_set_seed"
create_seed "$READER_USER" "$semantic_index_seed"
create_control_seed "$READER_USER" "$semantic_refresh_reader_seed"
create_control_seed "$EMBEDDER_USER" "$semantic_refresh_embedder_seed"
create_control_seed "$READER_USER" "$semantic_active_seed"
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
create_seed "$STATUS_USER" "$status_seed"
create_seed "$READER_USER" "$cadence_seed"
create_seed "$READER_USER" "$semantic_selection_seed"
create_seed "$GENERATOR_USER" "$projection_generator_seed"
create_seed "$EVALUATOR_USER" "$projection_evaluator_seed"
create_seed "$SYNC_USER" "$projection_result_seed"

# Positive reads.
probe_read "$READER_USER" "$daily_seed" allow "Reader reads Daily semantic corpus"
probe_read "$READER_USER" "$idea_seed" allow "Reader reads Idea semantic corpus"
probe_read "$READER_USER" "$knowledge_seed" allow "Reader reads canonical Knowledge"
probe_read "$READER_USER" "$index_seed" allow "Reader reads Index"
probe_read "$READER_USER" "$semantic_corpus_seed" allow "Reader reads Semantic Corpus manifest"
probe_read "$READER_USER" "$embedding_request_seed" allow "Reader reads embedding request"
probe_read "$READER_USER" "$embedding_plan_seed" allow "Reader reads embedding plan"
probe_read "$READER_USER" "$embedding_result_seed" allow "Reader reads embedding result"
probe_read "$READER_USER" "$embedding_result_set_seed" allow "Reader reads embedding result set"
probe_read "$READER_USER" "$semantic_index_seed" allow "Reader reads semantic index"
probe_read "$EMBEDDER_USER" "$embedding_request_seed" allow "Embedder reads bounded embedding request"
probe_read "$EMBEDDER_USER" "$embedding_plan_seed" allow "Embedder reads embedding plan"
probe_read "$EMBEDDER_USER" "$semantic_refresh_reader_seed" allow "Embedder reads Reader refresh control with production file mode"
probe_read "$READER_USER" "$semantic_refresh_embedder_seed" allow "Reader reads Embedder refresh control with production file mode"
probe_read "$READER_USER" "$semantic_active_seed" allow "Reader reads active Semantic Index binding"
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
probe_read "$REVIEWER_USER" "$projection_evaluator_seed" allow "Reviewer reads Evaluator review projection request"
probe_read "$REVIEWER_USER" "$projection_result_seed" allow "Reviewer reads projection transport attestation"
probe_read "$READER_USER" "$review_seed" allow "Reader reads authoritative Review for scheduler reconciliation"
probe_read "$READER_USER" "$receipts_seed" allow "Reader reads authoritative Receipt for scheduler reconciliation"
probe_read "$READER_USER" "$semantic_selection_seed" allow "Reader reads Semantic Selection Record"
probe_read "$REVIEWER_USER" "$execution_seed" allow "Reviewer reads Execution request"
probe_read "$REVIEWER_USER" "$transport_seed" allow "Reviewer reads Transport result"
probe_read "$REVIEWER_USER" "$receipts_seed" allow "Reviewer reads Receipts"
probe_read "$EXECUTOR_USER" "$validation_seed" allow "Executor reads Validation"
probe_read "$EXECUTOR_USER" "$review_seed" allow "Executor reads Review"
probe_read "$EXECUTOR_USER" "$transport_seed" allow "Executor reads Transport result"
probe_read "$STATUS_USER" "$status_seed" allow "Status identity reads aggregate status projection"
probe_read "$STATUS_USER" "$cadence_seed" allow "Status identity reads Planner cadence metadata"
probe_read "$REVIEWER_USER" "$status_seed" allow "Reviewer reads aggregate status projection"
probe_read "$SYNC_USER" "$validation_seed" allow "Sync reads Validation"
probe_read "$SYNC_USER" "$review_seed" allow "Sync reads Review"
probe_read "$SYNC_USER" "$execution_seed" allow "Sync reads Execution request"
probe_read "$SYNC_USER" "$projection_generator_seed" allow "Sync reads human projection request"
probe_read "$SYNC_USER" "$projection_result_seed" allow "Sync reads human projection result"
probe_read "$SYNC_USER" "$receipts_seed" allow "Sync reads Receipts for terminal projection cleanup"

# Negative reads protecting trust boundaries.
for user in "$GENERATOR_USER" "$VALIDATOR_USER" "$EVALUATOR_USER" "$REVIEWER_USER" "$EXECUTOR_USER"; do
  probe_read "$user" "$daily_seed" deny "$user cannot read Daily semantic corpus directly"
  probe_read "$user" "$idea_seed" deny "$user cannot read Idea semantic corpus directly"
done
probe_read "$EMBEDDER_USER" "$daily_seed" deny "Embedder cannot read Daily semantic corpus directly"
probe_read "$EMBEDDER_USER" "$idea_seed" deny "Embedder cannot read Idea semantic corpus directly"
probe_read "$EMBEDDER_USER" "$knowledge_seed" deny "Embedder cannot read canonical Knowledge directly"
probe_read "$EMBEDDER_USER" "$index_seed" deny "Embedder cannot list/read generic Index"
probe_read "$EMBEDDER_USER" "$semantic_corpus_seed" deny "Embedder cannot read Semantic Corpus manifest"
probe_read "$EMBEDDER_USER" "$semantic_index_seed" deny "Embedder cannot read finalized semantic index"
probe_read "$EMBEDDER_USER" "$semantic_active_seed" deny "Embedder cannot read active Semantic Index binding"
probe_read "$GENERATOR_USER" "$semantic_refresh_reader_seed" deny "Generator cannot read Reader refresh control"
probe_read "$GENERATOR_USER" "$semantic_refresh_embedder_seed" deny "Generator cannot read Embedder refresh control"
probe_read "$EMBEDDER_USER" "$context_seed" deny "Embedder cannot read Generator Context"
probe_read "$GENERATOR_USER" "$semantic_selection_seed" deny "Generator cannot read Semantic Selection Store"
probe_read "$VALIDATOR_USER" "$semantic_selection_seed" deny "Validator cannot read Semantic Selection Store"
probe_read "$EVALUATOR_USER" "$semantic_selection_seed" deny "Evaluator cannot read Semantic Selection Store"
probe_read "$REVIEWER_USER" "$semantic_selection_seed" deny "Reviewer cannot read Semantic Selection Store"
probe_read "$EXECUTOR_USER" "$semantic_selection_seed" deny "Executor cannot read Semantic Selection Store"
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
probe_read "$VALIDATOR_USER" "$projection_generator_seed" deny "Validator cannot read Generator projection request"
probe_read "$STATUS_USER" "$projection_generator_seed" deny "Status cannot read human projection request payload"
for path in "$knowledge_seed" "$untrusted_seed" "$index_seed" "$context_seed" "$validation_seed" "$evaluation_request_seed" "$evaluation_context_seed" "$evaluation_seed" "$review_seed" "$execution_seed" "$transport_seed" "$receipts_seed"; do
  probe_read "$STATUS_USER" "$path" deny "Status identity denied semantic/authority artifact: ${path}"
done

# Positive writes: one semantic writer per stage; Locks are deliberately shared operational state.
probe_write "$SYNC_USER" "$DAILY" allow "Sync writes Daily mirror corpus"
probe_write "$SYNC_USER" "$IDEAS" allow "Sync writes Idea mirror corpus"
probe_write "$SYNC_USER" "$KNOWLEDGE" allow "Sync writes local Vault mirror"
probe_write "$READER_USER" "$INDEX" allow "Reader writes Index"
probe_write "$READER_USER" "$SEMANTIC_CORPUS" allow "Reader writes Semantic Corpus manifest"
probe_write "$READER_USER" "$EMBEDDING_REQUESTS" allow "Reader writes embedding requests"
probe_write "$READER_USER" "$EMBEDDING_PLANS" allow "Reader writes embedding plans"
probe_write "$READER_USER" "$EMBEDDING_RESULTS" deny "Reader cannot write embedding results"
probe_write "$READER_USER" "$EMBEDDING_RESULT_SETS" deny "Reader cannot write embedding result sets"
probe_write "$READER_USER" "$SEMANTIC_INDEX" allow "Reader writes finalized semantic index"
probe_write "$EMBEDDER_USER" "$EMBEDDING_RESULTS" allow "Embedder writes embedding results"
probe_write "$EMBEDDER_USER" "$EMBEDDING_RESULT_SETS" allow "Embedder writes embedding result sets"
probe_write "$EMBEDDER_USER" "$EMBEDDING_REQUESTS" deny "Embedder cannot rewrite embedding requests"
probe_write "$EMBEDDER_USER" "$EMBEDDING_PLANS" deny "Embedder cannot rewrite embedding plans"
probe_write "$EMBEDDER_USER" "$SEMANTIC_CORPUS" deny "Embedder cannot write Semantic Corpus manifest"
probe_write "$EMBEDDER_USER" "$SEMANTIC_INDEX" deny "Embedder cannot write finalized semantic index"
probe_write "$READER_USER" "$SEMANTIC_REFRESH_READER" allow "Reader writes Reader refresh control directory"
probe_write "$EMBEDDER_USER" "$SEMANTIC_REFRESH_READER" deny "Embedder cannot write Reader refresh control directory"
probe_write "$EMBEDDER_USER" "$SEMANTIC_REFRESH_EMBEDDER" allow "Embedder writes Embedder refresh control directory"
probe_write "$READER_USER" "$SEMANTIC_REFRESH_EMBEDDER" deny "Reader cannot write Embedder refresh control directory"
probe_rewrite "$READER_USER" "$semantic_refresh_reader_seed" allow "Reader updates its refresh control"
probe_rewrite "$EMBEDDER_USER" "$semantic_refresh_embedder_seed" allow "Embedder updates its refresh control"
probe_rewrite "$EMBEDDER_USER" "$semantic_refresh_reader_seed" deny "Embedder cannot rewrite Reader refresh control"
probe_rewrite "$READER_USER" "$semantic_refresh_embedder_seed" deny "Reader cannot rewrite Embedder refresh control"
probe_replace "$EMBEDDER_USER" "$semantic_refresh_reader_seed" "$SEMANTIC_REFRESH_EMBEDDER" deny "Embedder cannot replace Reader refresh control"
probe_replace "$READER_USER" "$semantic_refresh_embedder_seed" "$SEMANTIC_REFRESH_READER" deny "Reader cannot replace Embedder refresh control"
probe_write "$EMBEDDER_USER" "$INDEX" deny "Embedder cannot write active binding parent directory"
probe_rewrite "$EMBEDDER_USER" "$semantic_active_seed" deny "Embedder cannot rewrite active Semantic Index binding"
probe_replace "$EMBEDDER_USER" "$semantic_active_seed" "$SEMANTIC_REFRESH_EMBEDDER" deny "Embedder cannot replace active Semantic Index binding"
probe_write "$READER_USER" "$CONTEXT" allow "Reader writes Context"
probe_write "$READER_USER" "$EVALUATION_CONTEXT" allow "Reader writes Evaluation Context"
probe_write "$GENERATOR_USER" "$UNTRUSTED" allow "Generator writes Untrusted"
probe_write "$READER_USER" "$ORCHESTRATION" allow "Reader writes orchestration metadata"
probe_write "$READER_USER" "$SEMANTIC_SELECTIONS" allow "Reader writes Semantic Selection Store"
probe_write "$GENERATOR_USER" "$SEMANTIC_SELECTIONS" deny "Generator cannot write Semantic Selection Store"
probe_write "$VALIDATOR_USER" "$SEMANTIC_SELECTIONS" deny "Validator cannot write Semantic Selection Store"
probe_write "$EVALUATOR_USER" "$SEMANTIC_SELECTIONS" deny "Evaluator cannot write Semantic Selection Store"
probe_write "$GENERATOR_USER" "$ORCHESTRATION" allow "Generator writes orchestration metadata"
probe_write "$VALIDATOR_USER" "$ORCHESTRATION" allow "Validator writes orchestration metadata"
probe_write "$EVALUATOR_USER" "$ORCHESTRATION" allow "Evaluator writes orchestration metadata"
probe_write "$STATUS_USER" "$STATUS_DIR" allow "Status identity writes aggregate status projection"
probe_write "$STATUS_USER" "$ORCHESTRATION" deny "Status identity cannot rewrite Planner orchestration metadata"
probe_write "$REVIEWER_USER" "$STATUS_DIR" deny "Reviewer cannot rewrite aggregate status projection"
probe_write "$VALIDATOR_USER" "$VALIDATION" allow "Validator writes Validation"
probe_write "$VALIDATOR_USER" "$EVALUATION_REQUEST" allow "Validator writes Evaluation Request"
probe_write "$EVALUATOR_USER" "$EVALUATION" allow "Evaluator writes Evaluation"
probe_write "$REVIEWER_USER" "$REVIEW" allow "Reviewer writes Review / recovery"
probe_write "$SYNC_USER" "$LOCKS" allow "Sync writes shared operational Locks"
probe_write "$REVIEWER_USER" "$LOCKS" allow "Reviewer writes shared operational Locks"
probe_write "$EXECUTOR_USER" "$LOCKS" allow "Executor writes shared operational Locks"
probe_write "$SYNC_USER" "$READ_VIEW_LOCKS" allow "Sync writes mirror read-view Locks"
probe_write "$READER_USER" "$READ_VIEW_LOCKS" allow "Reader writes mirror read-view Locks"
probe_write "$EXECUTOR_USER" "$EXECUTION" allow "Executor writes Execution request"
probe_write "$SYNC_USER" "$TRANSPORT" allow "Sync writes Transport result"
probe_write "$EXECUTOR_USER" "$RECEIPTS" allow "Executor writes Receipts"
probe_write "$READER_USER" "$PROJECTION/reader" allow "Reader writes own human projection request"
probe_write "$GENERATOR_USER" "$PROJECTION/generator" allow "Generator writes own human projection request"
probe_write "$VALIDATOR_USER" "$PROJECTION/validator" allow "Validator writes own human projection request"
probe_write "$EVALUATOR_USER" "$PROJECTION/evaluator" allow "Evaluator writes own human projection request"
probe_write "$REVIEWER_USER" "$PROJECTION/reviewer" allow "Reviewer writes own human projection request"
probe_write "$EXECUTOR_USER" "$PROJECTION/executor" allow "Executor writes own human projection request"
probe_write "$SYNC_USER" "$PROJECTION/sync" allow "Sync writes own human projection request"
probe_write "$SYNC_USER" "$PROJECTION_RESULT" allow "Sync writes human projection result"
probe_write "$GENERATOR_USER" "$PROJECTION/validator" deny "Generator cannot forge Validator projection request"
probe_write "$SYNC_USER" "$PROJECTION/generator" deny "Sync cannot forge Generator projection request"


for user in "$GENERATOR_USER" "$VALIDATOR_USER" "$EVALUATOR_USER" "$REVIEWER_USER" "$EXECUTOR_USER"; do
  probe_write "$user" "$READ_VIEW_LOCKS" deny "$user cannot write mirror read-view Locks"
done

for user in "$SYNC_USER" "$REVIEWER_USER" "$EXECUTOR_USER"; do
  probe_write "$user" "$ORCHESTRATION" deny "$user cannot write pre-review orchestration metadata"
done

# Reader writes only derived retrieval state and cannot write semantic authority stages.
for directory in "$DAILY" "$IDEAS" "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION" "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$READER_USER" "$directory" deny "Reader denied write: ${directory}"
done

# Embedder writes only bounded embedding result artifacts.
for directory in "$DAILY" "$IDEAS" "$KNOWLEDGE" "$UNTRUSTED" "$ORCHESTRATION" "$CONTEXT" "$VALIDATION" "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  probe_write "$EMBEDDER_USER" "$directory" deny "Embedder denied write: ${directory}"
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
