#!/bin/sh
set -eu

COMPACTOR_USER=obsidian-github-compactor
COMPACTOR_GROUP=obsidian-github-compactor
PIPELINE_GROUP=obsidian-github-pipeline
REQUEST_DIR=/var/lib/obsidian-github-pipeline/25-Execution
RESULT_DIR=/var/lib/obsidian-github-pipeline/27-Transport

# Run the POSIX shell's builtin test as the service UID. Ubuntu 26.04's
# external GNU test can reject valid named-user directory POSIX ACLs.
# Keep flag and path as positional parameters, not evaluated shell code.
probe_role_access() {
  # Only an executed, well-formed DENY can satisfy a negative access gate.
  # A failed runuser/POSIX shell/probe must stop the whole script, not pass.
  obsidian_access_verdict=$(runuser -u "$1" -- sh -c '
    test "$1" "$2"
    probe_rc=$?
    case "$probe_rc" in
      0) printf "ALLOW\n" ;;
      1) printf "DENY\n" ;;
      *) exit 64 ;;
    esac
  ' sh "$2" "$3") || {
    echo "authority access probe execution failed" >&2
    exit 1
  }
  case "$obsidian_access_verdict" in
    ALLOW) return 0 ;;
    DENY) return 1 ;;
    *) echo "invalid authority access probe verdict" >&2; exit 1 ;;
  esac
}

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi

if ! command -v setfacl >/dev/null 2>&1; then
  echo "setfacl is required (install the acl package)" >&2
  exit 1
fi

for dir in "$REQUEST_DIR" "$RESULT_DIR"; do
  if [ ! -d "$dir" ] || [ -L "$dir" ]; then
    echo "required pipeline directory is missing or unsafe: $dir" >&2
    exit 1
  fi
done

if ! getent group "$PIPELINE_GROUP" >/dev/null 2>&1; then
  echo "pipeline group does not exist: $PIPELINE_GROUP" >&2
  exit 1
fi

if ! getent group "$COMPACTOR_GROUP" >/dev/null 2>&1; then
  groupadd --system "$COMPACTOR_GROUP"
fi

if ! id "$COMPACTOR_USER" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "$COMPACTOR_GROUP" \
    --home-dir /var/lib/obsidian-github-compactor \
    --create-home \
    --shell /usr/sbin/nologin \
    "$COMPACTOR_USER"
fi

usermod -aG "$PIPELINE_GROUP" "$COMPACTOR_USER"
setfacl -m "u:$COMPACTOR_USER:rwx" "$REQUEST_DIR"

if ! probe_role_access "$COMPACTOR_USER" -r "$REQUEST_DIR"; then
  echo "compactor cannot read request directory" >&2
  exit 1
fi
if ! probe_role_access "$COMPACTOR_USER" -w "$REQUEST_DIR"; then
  echo "compactor cannot delete from request directory" >&2
  exit 1
fi
if ! probe_role_access "$COMPACTOR_USER" -r "$RESULT_DIR"; then
  echo "compactor cannot read result directory" >&2
  exit 1
fi
if probe_role_access "$COMPACTOR_USER" -w "$RESULT_DIR"; then
  echo "compactor unexpectedly has result write authority" >&2
  exit 1
fi

echo "PASS: compactor local authority configured"
