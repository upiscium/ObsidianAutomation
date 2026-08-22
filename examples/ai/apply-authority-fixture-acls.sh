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
  echo "ERROR: refusing ACL mutation without $MARKER" >&2
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
  "$REVIEWER_USER" \
  "$EXECUTOR_USER"; do
  id "$user" >/dev/null 2>&1 || {
    echo "ERROR: required user does not exist: $user" >&2
    exit 1
  }
done

SYNC_GROUP=$(id -gn "$SYNC_USER")

install -d -o "$SYNC_USER" -g "$SYNC_GROUP" -m 0700 "$VAULT_ROOT"
install -d -o "$SYNC_USER" -g "$SYNC_GROUP" -m 0700 "$AI_ROOT"
for directory in "$KNOWLEDGE" "$UNTRUSTED" "$VALIDATION" "$REVIEW" "$EXECUTION" "$RECEIPTS"; do
  install -d -o "$SYNC_USER" -g "$SYNC_GROUP" -m 0700 "$directory"
  setfacl -b "$directory"
  setfacl -k "$directory" || true
done

# Traversal only on container directories. Stage-specific permissions are below.
setfacl -b "$VAULT_ROOT"
setfacl -k "$VAULT_ROOT" || true
for entry in \
  "u:$READER_USER:--x" \
  "u:$GENERATOR_USER:--x" \
  "u:$VALIDATOR_USER:--x" \
  "u:$REVIEWER_USER:--x" \
  "u:$EXECUTOR_USER:--x"; do
  setfacl -m "$entry" "$VAULT_ROOT"
done

setfacl -b "$AI_ROOT"
setfacl -k "$AI_ROOT" || true
for entry in \
  "u:$GENERATOR_USER:--x" \
  "u:$VALIDATOR_USER:--x" \
  "u:$REVIEWER_USER:--x" \
  "u:$EXECUTOR_USER:--x"; do
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

apply_directory_acl "$KNOWLEDGE" \
  "u:$READER_USER:r-x" \
  "u:$VALIDATOR_USER:r-x" \
  "u:$EXECUTOR_USER:rwx"

apply_directory_acl "$UNTRUSTED" \
  "u:$GENERATOR_USER:rwx" \
  "u:$VALIDATOR_USER:r-x"

apply_directory_acl "$VALIDATION" \
  "u:$VALIDATOR_USER:rwx" \
  "u:$REVIEWER_USER:r-x" \
  "u:$EXECUTOR_USER:r-x"

apply_directory_acl "$REVIEW" \
  "u:$REVIEWER_USER:rwx" \
  "u:$EXECUTOR_USER:r-x"

apply_directory_acl "$EXECUTION" \
  "u:$EXECUTOR_USER:rwx"

apply_directory_acl "$RECEIPTS" \
  "u:$REVIEWER_USER:r-x" \
  "u:$EXECUTOR_USER:rwx"

echo "Applied disposable AI authority ACL fixture to: $VAULT_ROOT"
echo "Sync owner: $SYNC_USER:$SYNC_GROUP"
