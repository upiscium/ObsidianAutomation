#!/bin/sh
set -eu

COMPACTOR_USER=obsidian-github-compactor
COMPACTOR_GROUP=obsidian-github-compactor
PIPELINE_GROUP=obsidian-github-pipeline
REQUEST_DIR=/var/lib/obsidian-github-pipeline/25-Execution
RESULT_DIR=/var/lib/obsidian-github-pipeline/27-Transport

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

if ! runuser -u "$COMPACTOR_USER" -- test -r "$REQUEST_DIR"; then
  echo "compactor cannot read request directory" >&2
  exit 1
fi
if ! runuser -u "$COMPACTOR_USER" -- test -w "$REQUEST_DIR"; then
  echo "compactor cannot delete from request directory" >&2
  exit 1
fi
if ! runuser -u "$COMPACTOR_USER" -- test -r "$RESULT_DIR"; then
  echo "compactor cannot read result directory" >&2
  exit 1
fi
if runuser -u "$COMPACTOR_USER" -- test -w "$RESULT_DIR"; then
  echo "compactor unexpectedly has result write authority" >&2
  exit 1
fi

echo "PASS: compactor local authority configured"
