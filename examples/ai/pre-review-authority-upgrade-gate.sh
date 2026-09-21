#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${REPO_ROOT:?set REPO_ROOT to this repository checkout}"
: "${VAULT_ROOT:?set VAULT_ROOT to a disposable Vault root}"
: "${AI_ROOT:?set AI_ROOT to a disposable state root}"

REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
READER_USER=${READER_USER:-obsidian-ai-reader}
UNRELATED_USER=${UNRELATED_USER:-obsidian-ai-generator}
SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
EVALUATOR_USER=${EVALUATOR_USER:-obsidian-ai-evaluator}
EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}

FIXTURE="$REPO_ROOT/examples/ai/apply-authority-fixture-acls.sh"
BOOTSTRAP="$REPO_ROOT/examples/ai/bootstrap-pre-review-authority.sh"
PROJECTION_DIR="$AI_ROOT/16-Human-Projection/evaluator"
RESULT_DIR="$AI_ROOT/17-Human-Projection-Result"
REVIEW_DIR="$AI_ROOT/20-Review"
RECEIPT_DIR="$AI_ROOT/30-Receipts"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

assert_readable() {
  local user=$1 path=$2
  runuser -u "$user" -- test -r "$path" || fail "$user cannot read $path"
}

assert_not_readable() {
  local user=$1 path=$2
  if runuser -u "$user" -- test -r "$path"; then
    fail "$user unexpectedly reads $path"
  fi
}

assert_not_writable() {
  local user=$1 path=$2
  if runuser -u "$user" -- test -w "$path"; then
    fail "$user unexpectedly writes $path"
  fi
}

install -d -m 0700 "$VAULT_ROOT" "$AI_ROOT" \
  "$PROJECTION_DIR" "$RESULT_DIR" "$REVIEW_DIR" "$RECEIPT_DIR"
touch "$VAULT_ROOT/.obsidian-ai-disposable-fixture"
touch "$AI_ROOT/.obsidian-ai-disposable-state"

# These artifacts deliberately predate the reader defaults added by #147.
projection="$PROJECTION_DIR/old.projection.json"
result="$RESULT_DIR/old.projection-result.json"
review="$REVIEW_DIR/old.approval.json"
receipt="$RECEIPT_DIR/old.receipt.json"
printf '%s\n' '{"kind":"old-projection"}' > "$projection"
printf '%s\n' '{"kind":"old-result"}' > "$result"
printf '%s\n' '{"kind":"old-review"}' > "$review"
printf '%s\n' '{"kind":"old-receipt"}' > "$receipt"
setfacl -m "u:$EVALUATOR_USER:rw-,u:$SYNC_USER:r--" "$projection"
setfacl -m "u:$SYNC_USER:rw-" "$result"
setfacl -m "u:$REVIEWER_USER:rw-,u:$SYNC_USER:r--,u:$EXECUTOR_USER:r--" "$review"
setfacl -m "u:$EXECUTOR_USER:rw-,u:$REVIEWER_USER:r--" "$receipt"
# A latent write bit hidden by this mask must not be activated by migration.
setfacl -m m::r-- "$result"

for directory in "$PROJECTION_DIR" "$RESULT_DIR" "$REVIEW_DIR" "$RECEIPT_DIR"; do
  printf '%s\n' '{"kind":"unrelated"}' > "$directory/unrelated.json"
done

content_before=$(sha256sum "$projection" "$result" "$review" "$receipt")
ownership_before=$(stat -c '%n %u:%g' "$projection" "$result" "$review" "$receipt")

env VAULT_ROOT="$VAULT_ROOT" AI_ROOT="$AI_ROOT" bash "$FIXTURE"

# Directory/default ACLs are current now, but old files still lack new readers.
assert_not_readable "$REVIEWER_USER" "$projection"
assert_not_readable "$REVIEWER_USER" "$result"
assert_not_readable "$READER_USER" "$review"
assert_not_readable "$READER_USER" "$receipt"
assert_not_writable "$SYNC_USER" "$result"

# New files prove the fresh-host default ACL contract remains effective.
fresh_projection="$PROJECTION_DIR/fresh.projection.json"
fresh_result="$RESULT_DIR/fresh.projection-result.json"
fresh_review="$REVIEW_DIR/fresh.approval.json"
fresh_receipt="$RECEIPT_DIR/fresh.receipt.json"
printf '{}\n' > "$fresh_projection"
printf '{}\n' > "$fresh_result"
printf '{}\n' > "$fresh_review"
printf '{}\n' > "$fresh_receipt"
assert_readable "$REVIEWER_USER" "$fresh_projection"
assert_readable "$REVIEWER_USER" "$fresh_result"
assert_readable "$READER_USER" "$fresh_review"
assert_readable "$READER_USER" "$fresh_receipt"

env AI_ROOT="$AI_ROOT" sh "$BOOTSTRAP"

assert_readable "$REVIEWER_USER" "$projection"
assert_readable "$REVIEWER_USER" "$result"
assert_readable "$READER_USER" "$review"
assert_readable "$READER_USER" "$receipt"
assert_not_writable "$REVIEWER_USER" "$projection"
assert_not_writable "$REVIEWER_USER" "$result"
assert_not_writable "$READER_USER" "$review"
assert_not_writable "$READER_USER" "$receipt"
assert_not_writable "$SYNC_USER" "$result"

for artifact in "$projection" "$result" "$review" "$receipt"; do
  assert_not_readable "$UNRELATED_USER" "$artifact"
done
assert_not_readable "$REVIEWER_USER" "$PROJECTION_DIR/unrelated.json"
assert_not_readable "$REVIEWER_USER" "$RESULT_DIR/unrelated.json"
assert_not_readable "$READER_USER" "$REVIEW_DIR/unrelated.json"
assert_not_readable "$READER_USER" "$RECEIPT_DIR/unrelated.json"

[[ $(sha256sum "$projection" "$result" "$review" "$receipt") == "$content_before" ]] || \
  fail "migration changed artifact contents"
[[ $(stat -c '%n %u:%g' "$projection" "$result" "$review" "$receipt") == "$ownership_before" ]] || \
  fail "migration changed artifact ownership"

acl_before_repeat=$(getfacl -cp "$projection" "$result" "$review" "$receipt")
env AI_ROOT="$AI_ROOT" sh "$BOOTSTRAP"
acl_after_repeat=$(getfacl -cp "$projection" "$result" "$review" "$receipt")
[[ "$acl_after_repeat" == "$acl_before_repeat" ]] || fail "repeat migration changed ACLs"

# Matching symlinks and non-regular entries must stop the migration, not be
# followed or silently skipped.
outside_target="$AI_ROOT/outside-target"
printf 'outside\n' > "$outside_target"
outside_acl=$(getfacl -cp "$outside_target")
ln -s "$outside_target" "$PROJECTION_DIR/unsafe.projection.json"
if env AI_ROOT="$AI_ROOT" sh "$BOOTSTRAP"; then
  fail "matching symlink did not block migration"
fi
[[ $(getfacl -cp "$outside_target") == "$outside_acl" ]] || fail "symlink target ACL changed"
rm "$PROJECTION_DIR/unsafe.projection.json"

mkfifo "$RESULT_DIR/unsafe.projection-result.json"
if env AI_ROOT="$AI_ROOT" sh "$BOOTSTRAP"; then
  fail "matching non-regular entry did not block migration"
fi
rm "$RESULT_DIR/unsafe.projection-result.json"

echo "PASS: pre-review authority upgrade migration is narrow, idempotent, and fail-closed"
