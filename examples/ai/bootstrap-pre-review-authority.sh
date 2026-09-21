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
EVALUATOR_PROJECTIONS="$AI_ROOT/16-Human-Projection/evaluator"
PROJECTION_RESULTS="$AI_ROOT/17-Human-Projection-Result"
REVIEWS="$AI_ROOT/20-Review"
RECEIPTS="$AI_ROOT/30-Receipts"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi

for command in setfacl getfacl install id getent groupadd useradd runuser find python3; do
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

# Files created before the post-review readers were introduced do not inherit
# the current default ACLs. The helper opens namespaces and artifacts with
# O_NOFOLLOW, then applies ACLs through pinned /proc/self/fd descriptors. This
# prevents a writer from swapping a validated artifact for a symlink before the
# root-owned setfacl call. Existing effective ACL authority is preserved even
# when an old ACL mask hid permissions that must not become effective.
python3 - \
  "$EVALUATOR_PROJECTIONS" '.projection.json' "$REVIEWER_USER" \
  "$PROJECTION_RESULTS" '.projection-result.json' "$REVIEWER_USER" \
  "$REVIEWS" '.approval.json' "$READER_USER" \
  "$RECEIPTS" '.receipt.json' "$READER_USER" <<'PY'
import os
import pwd
import stat
import subprocess
import sys


def permission_bits(value: str) -> int:
    return sum(bit for flag, bit in zip(value, (4, 2, 1)) if flag != "-")


def permission_text(value: int) -> str:
    return "".join(flag if value & bit else "-" for flag, bit in zip("rwx", (4, 2, 1)))


def read_acl(fd: int) -> list[tuple[str, str, int]]:
    result = subprocess.run(
        ("getfacl", "-c", "-n", "-p", f"/proc/self/fd/{fd}"),
        check=True,
        pass_fds=(fd,),
        capture_output=True,
        text=True,
    )
    entries = []
    for line in result.stdout.splitlines():
        if not line or line.startswith("#") or line.startswith("default:"):
            continue
        # getfacl annotates masked entries with an inline
        # "#effective:<permissions>" comment; raw permissions remain before it.
        line = line.partition("#")[0].rstrip()
        parts = line.split(":", 2)
        if len(parts) != 3 or parts[0] not in {"user", "group", "mask", "other"}:
            raise RuntimeError(f"unexpected ACL entry on pinned artifact: {line!r}")
        entries.append((parts[0], parts[1], permission_bits(parts[2])))
    return entries


def grant_pinned_read(fd: int, user: str) -> None:
    entries = read_acl(fd)
    target_uid = str(pwd.getpwnam(user).pw_uid)
    values = {(kind, qualifier): permissions for kind, qualifier, permissions in entries}
    old_mask = values.get(("mask", ""), values[("group", "")])
    owner = values[("user", "")]
    owning_group = values[("group", "")] & old_mask
    other = values[("other", "")]

    named_users = []
    named_groups = []
    for kind, qualifier, permissions in entries:
        if kind == "user" and qualifier and qualifier != target_uid:
            named_users.append((qualifier, permissions & old_mask))
        elif kind == "group" and qualifier:
            named_groups.append((qualifier, permissions & old_mask))

    # Strip permissions that were latent behind the old mask before adding the
    # read bit to that mask. This preserves every existing effective boundary.
    acl = [f"user::{permission_text(owner)}"]
    acl.extend(f"user:{qualifier}:{permission_text(permissions)}" for qualifier, permissions in named_users)
    acl.append(f"user:{target_uid}:r--")
    acl.append(f"group::{permission_text(owning_group)}")
    acl.extend(f"group:{qualifier}:{permission_text(permissions)}" for qualifier, permissions in named_groups)
    acl.append(f"mask::{permission_text(old_mask | 4)}")
    acl.append(f"other::{permission_text(other)}")
    subprocess.run(
        ("setfacl", "--set-file=-", f"/proc/self/fd/{fd}"),
        input="\n".join(acl) + "\n",
        text=True,
        check=True,
        pass_fds=(fd,),
    )


specifications = list(zip(sys.argv[1::3], sys.argv[2::3], sys.argv[3::3]))
namespaces = []
try:
    # Preflight every namespace and matching entry before any ACL mutation.
    for directory, suffix, user in specifications:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        names = []
        for name in os.listdir(directory_fd):
            if not name.endswith(suffix):
                continue
            artifact_fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                if not stat.S_ISREG(os.fstat(artifact_fd).st_mode):
                    raise RuntimeError(f"unsafe non-regular artifact blocks ACL backfill: {directory}/{name}")
            finally:
                os.close(artifact_fd)
            names.append(name)
        namespaces.append((directory, directory_fd, names, user))

    for directory, directory_fd, names, user in namespaces:
        for name in names:
            artifact_fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                if not stat.S_ISREG(os.fstat(artifact_fd).st_mode):
                    raise RuntimeError(f"unsafe non-regular artifact blocks ACL backfill: {directory}/{name}")
                grant_pinned_read(artifact_fd, user)
            finally:
                os.close(artifact_fd)
finally:
    for _, directory_fd, _, _ in namespaces:
        os.close(directory_fd)
PY

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
