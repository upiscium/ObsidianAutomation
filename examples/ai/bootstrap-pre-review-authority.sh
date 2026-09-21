#!/bin/sh
set -eu

AI_ROOT=${AI_ROOT:-/var/lib/obsidian-ai/state}
SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
READER_USER=${READER_USER:-obsidian-ai-reader}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
EVALUATOR_USER=${EVALUATOR_USER:-obsidian-ai-evaluator}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
STATUS_USER=${STATUS_USER:-obsidian-ai-status}
STATUS_GROUP=${STATUS_GROUP:-obsidian-ai-status}

ORCHESTRATION="$AI_ROOT/02-Orchestration"
RECIPES="$ORCHESTRATION/recipes"
STATUS_DIR="$ORCHESTRATION/status"
DB="$ORCHESTRATION/pre-review-jobs.sqlite3"
LOCKS="$AI_ROOT/24-Locks"
READ_VIEW="$LOCKS/read-view"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi

for command in setfacl getfacl install id getent groupadd useradd runuser find; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "required command is missing: $command" >&2
    exit 1
  }
done

if [ -d /etc/obsidian-ai ] && [ ! -L /etc/obsidian-ai ]; then
  setfacl -m "u:$REVIEWER_USER:--x" /etc/obsidian-ai
fi

for dir in "$AI_ROOT" "$LOCKS"; do
  if [ ! -d "$dir" ] || [ -L "$dir" ]; then
    echo "required state directory is missing or unsafe: $dir" >&2
    exit 1
  fi
done

for user in   "$SYNC_USER"   "$READER_USER"   "$GENERATOR_USER"   "$VALIDATOR_USER"   "$EVALUATOR_USER"   "$REVIEWER_USER"; do
  id "$user" >/dev/null 2>&1 || {
    echo "required production identity does not exist: $user" >&2
    exit 1
  }
done

if ! getent group "$STATUS_GROUP" >/dev/null 2>&1; then
  groupadd --system "$STATUS_GROUP"
fi
if ! id "$STATUS_USER" >/dev/null 2>&1; then
  useradd     --system     --gid "$STATUS_GROUP"     --no-create-home     --home-dir /nonexistent     --shell /usr/sbin/nologin     "$STATUS_USER"
fi

install -d -o root -g root -m 0700 "$ORCHESTRATION"
install -d -o root -g root -m 0700 "$RECIPES"
install -d -o root -g root -m 0700 "$STATUS_DIR"
install -d -o root -g root -m 0700 "$READ_VIEW"

# Status needs only the orchestration metadata DB, never semantic lifecycle stages.
setfacl -m "u:$STATUS_USER:--x" "$AI_ROOT"

# Review Intake reads evaluator projection requests/results; Reader observes only
# authoritative terminal Review/Receipt artifacts for scheduler reconciliation.
setfacl -m "u:$REVIEWER_USER:r-x" "$AI_ROOT/16-Human-Projection/evaluator"
setfacl -m "u:$REVIEWER_USER:r-x" "$AI_ROOT/17-Human-Projection-Result"
setfacl -m "u:$READER_USER:r-x" "$AI_ROOT/20-Review"
setfacl -m "u:$READER_USER:r-x" "$AI_ROOT/30-Receipts"

# Reader needs traverse-only access to 24-Locks and rw only in read-view.
setfacl -m "u:$READER_USER:--x" "$LOCKS"

reset_acl_dir() {
  dir=$1
  shift
  setfacl -b "$dir"
  setfacl -k "$dir" 2>/dev/null || true
  setfacl -m u::rwx,g::---,o::---,m::rwx "$dir"
  setfacl -m d:u::rwx,d:g::---,d:o::---,d:m::rwx "$dir"
  for entry in "$@"; do
    setfacl -m "$entry" "$dir"
    setfacl -m "d:$entry" "$dir"
  done
}

reset_acl_dir "$ORCHESTRATION"   "u:$READER_USER:rwx"   "u:$GENERATOR_USER:rwx"   "u:$VALIDATOR_USER:rwx"   "u:$EVALUATOR_USER:rwx"   "u:$STATUS_USER:r-x"   "u:$REVIEWER_USER:--x"

reset_acl_dir "$RECIPES"   "u:$READER_USER:rwx"   "u:$GENERATOR_USER:r-x"   "u:$VALIDATOR_USER:r-x"   "u:$EVALUATOR_USER:r-x"

reset_acl_dir "$STATUS_DIR"   "u:$STATUS_USER:rwx"   "u:$REVIEWER_USER:r-x"

reset_acl_dir "$READ_VIEW"   "u:$SYNC_USER:rwx"   "u:$READER_USER:rwx"

if [ -e "$DB" ]; then
  if [ ! -f "$DB" ] || [ -L "$DB" ]; then
    echo "existing orchestration DB is unsafe: $DB" >&2
    exit 1
  fi
  setfacl -m     "u:$READER_USER:rw-,u:$GENERATOR_USER:rw-,u:$VALIDATOR_USER:rw-,u:$EVALUATOR_USER:rw-,u:$STATUS_USER:r--"     "$DB"
fi

# Existing immutable recipes may predate the new default ACL.
find "$RECIPES" -maxdepth 1 -type f -name '*.recipe.json' -exec   setfacl -m     "u:$READER_USER:rw-,u:$GENERATOR_USER:r--,u:$VALIDATOR_USER:r--,u:$EVALUATOR_USER:r--"     {} +

STATUS_FILE="$STATUS_DIR/pre-review-status.json"
if [ -e "$STATUS_FILE" ]; then
  if [ ! -f "$STATUS_FILE" ] || [ -L "$STATUS_FILE" ]; then
    echo "existing status projection is unsafe: $STATUS_FILE" >&2
    exit 1
  fi
  setfacl -m "u:$STATUS_USER:rw-,u:$REVIEWER_USER:r--" "$STATUS_FILE"
fi

# Negative authority checks are deliberate: status has metadata-only access.
if runuser -u "$STATUS_USER" -- test -w "$ORCHESTRATION"; then
  echo "status identity unexpectedly has orchestration directory write authority" >&2
  exit 1
fi
if ! runuser -u "$STATUS_USER" -- test -w "$STATUS_DIR"; then
  echo "status identity cannot write status projection directory" >&2
  exit 1
fi
if runuser -u "$REVIEWER_USER" -- test -w "$STATUS_DIR"; then
  echo "reviewer unexpectedly has status projection write authority" >&2
  exit 1
fi
if runuser -u "$STATUS_USER" -- test -r "$AI_ROOT/05-Context" 2>/dev/null; then
  echo "status identity unexpectedly reads Context stage" >&2
  exit 1
fi
if runuser -u "$STATUS_USER" -- test -r "$AI_ROOT/00-Untrusted" 2>/dev/null; then
  echo "status identity unexpectedly reads Untrusted stage" >&2
  exit 1
fi

echo "PASS: pre-review production authority configured"
echo "AI root: $AI_ROOT"
echo "Status identity: $STATUS_USER"
