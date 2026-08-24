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
EVALUATION_REQUEST="$AI_ROOT/12-Evaluation-Request"
EVALUATION_CONTEXT="$AI_ROOT/14-Evaluation-Context"
EVALUATION="$AI_ROOT/15-Evaluation"
VALIDATION="$AI_ROOT/10-Validation"
REVIEW="$AI_ROOT/20-Review"
LOCKS="$AI_ROOT/24-Locks"
EXECUTION="$AI_ROOT/25-Execution"
TRANSPORT="$AI_ROOT/27-Transport"
RECEIPTS="$AI_ROOT/30-Receipts"

if [[ ! -f "$VAULT_MARKER" ]]; then
  echo "ERROR: refusing ACL mutation without $VAULT_MARKER" >&2
  exit 1
fi
if [[ "$AI_ROOT" != "$DEFAULT_AI_ROOT" && ! -f "$STATE_MARKER" ]]; then
  echo "ERROR: refusing separate state ACL mutation without $STATE_MARKER" >&2
  exit 1
fi

for command in setfacl getfacl id install; do
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

SYNC_GROUP=$(id -gn "$SYNC_USER")

install -d -o "$SYNC_USER" -g "$SYNC_GROUP" -m 0700 "$VAULT_ROOT"
install -d -o "$SYNC_USER" -g "$SYNC_GROUP" -m 0700 "$KNOWLEDGE"
setfacl -b "$VAULT_ROOT"
setfacl -k "$VAULT_ROOT" || true
for entry in \
  "u:$READER_USER:--x" \
  "u:$VALIDATOR_USER:r-x" \
  "u:$EXECUTOR_USER:r-x"; do
  setfacl -m "$entry" "$VAULT_ROOT"
done

setfacl -b "$KNOWLEDGE"
setfacl -k "$KNOWLEDGE" || true
setfacl -m u::rwx,g::---,o::---,m::rwx "$KNOWLEDGE"
for entry in \
  "u:$READER_USER:r-x" \
  "u:$VALIDATOR_USER:r-x" \
  "u:$EXECUTOR_USER:r-x"; do
  setfacl -m "$entry" "$KNOWLEDGE"
done
setfacl -m d:u::rwx,d:g::---,d:o::---,d:m::rwx "$KNOWLEDGE"
for entry in \
  "u:$READER_USER:r-x" \
  "u:$VALIDATOR_USER:r-x" \
  "u:$EXECUTOR_USER:r-x"; do
  setfacl -m "d:$entry" "$KNOWLEDGE"
done

install -d -o root -g root -m 0700 "$AI_ROOT"
for directory in \
  "$UNTRUSTED" "$INDEX" "$CONTEXT" "$VALIDATION" \
  "$EVALUATION_REQUEST" "$EVALUATION_CONTEXT" "$EVALUATION" \
  "$REVIEW" "$LOCKS" "$EXECUTION" "$TRANSPORT" "$RECEIPTS"; do
  install -d -o root -g root -m 0700 "$directory"
  setfacl -b "$directory"
  setfacl -k "$directory" || true
done

setfacl -b "$AI_ROOT"
setfacl -k "$AI_ROOT" || true
for entry in \
  "u:$SYNC_USER:r-x" \
  "u:$READER_USER:--x" \
  "u:$GENERATOR_USER:--x" \
  "u:$VALIDATOR_USER:--x" \
  "u:$EVALUATOR_USER:--x" \
  "u:$REVIEWER_USER:r-x" \
  "u:$EXECUTOR_USER:r-x"; do
  setfacl -m "$entry" "$AI_ROOT"
done

apply_directory_acl() {
  local directory=$1
  shift
  setfacl -m u::rwx,g::---,o::---,m::rwx "$directory"
  local entry
  for entry in "$@"; do
    setfacl -m "$entry" "$directory"
  done
  setfacl -m d:u::rwx,d:g::---,d:o::---,d:m::rwx "$directory"
  for entry in "$@"; do
    setfacl -m "d:$entry" "$directory"
  done
}

apply_directory_acl "$UNTRUSTED" \
  "u:$GENERATOR_USER:rwx" \
  "u:$VALIDATOR_USER:r-x" \
  "u:$EVALUATOR_USER:r-x"

apply_directory_acl "$INDEX" \
  "u:$READER_USER:rwx"

apply_directory_acl "$CONTEXT" \
  "u:$READER_USER:rwx" \
  "u:$GENERATOR_USER:r-x" \
  "u:$EVALUATOR_USER:r-x"

apply_directory_acl "$VALIDATION" \
  "u:$SYNC_USER:r-x" \
  "u:$VALIDATOR_USER:rwx" \
  "u:$EVALUATOR_USER:r-x" \
  "u:$REVIEWER_USER:r-x" \
  "u:$EXECUTOR_USER:r-x"

apply_directory_acl "$EVALUATION_REQUEST" \
  "u:$VALIDATOR_USER:rwx" \
  "u:$READER_USER:r-x"

apply_directory_acl "$EVALUATION_CONTEXT" \
  "u:$READER_USER:rwx" \
  "u:$EVALUATOR_USER:r-x"

apply_directory_acl "$EVALUATION" \
  "u:$EVALUATOR_USER:rwx" \
  "u:$REVIEWER_USER:r-x"

apply_directory_acl "$REVIEW" \
  "u:$SYNC_USER:r-x" \
  "u:$REVIEWER_USER:rwx" \
  "u:$EXECUTOR_USER:r-x"

apply_directory_acl "$LOCKS" \
  "u:$SYNC_USER:rwx" \
  "u:$REVIEWER_USER:rwx" \
  "u:$EXECUTOR_USER:rwx"

apply_directory_acl "$EXECUTION" \
  "u:$SYNC_USER:r-x" \
  "u:$REVIEWER_USER:r-x" \
  "u:$EXECUTOR_USER:rwx"

apply_directory_acl "$TRANSPORT" \
  "u:$SYNC_USER:rwx" \
  "u:$REVIEWER_USER:r-x" \
  "u:$EXECUTOR_USER:r-x"

apply_directory_acl "$RECEIPTS" \
  "u:$REVIEWER_USER:r-x" \
  "u:$EXECUTOR_USER:rwx"

echo "Applied disposable AI authority ACL fixture"
echo "Vault mirror: $VAULT_ROOT"
echo "AI state:     $AI_ROOT"
echo "Sync identity: $SYNC_USER:$SYNC_GROUP"
