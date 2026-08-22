#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${VAULT_ROOT:?set VAULT_ROOT to the disposable Vault root}"
: "${AI_ROOT:?set AI_ROOT to the disposable AI state root}"
REPO_ROOT=${REPO_ROOT:-"$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"}

READER_USER=${READER_USER:-obsidian-ai-reader}
SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
KNOWLEDGE="$VAULT_ROOT/11-Knowledge"
INDEX="$AI_ROOT/04-Index"
CONTEXT="$AI_ROOT/05-Context"

for directory in "$VAULT_ROOT" "$KNOWLEDGE" "$INDEX" "$CONTEXT"; do
  [[ -d "$directory" && ! -L "$directory" ]] || {
    echo "ERROR: unsafe or missing fixture directory: $directory" >&2
    exit 1
  }
done
[[ -d "$REPO_ROOT/src/obsidian_automation" ]] || {
  echo "ERROR: repository source tree not found: $REPO_ROOT" >&2
  exit 1
}

for command in runuser python3 mktemp cp chmod; do
  command -v "$command" >/dev/null || {
    echo "ERROR: required command not found: $command" >&2
    exit 1
  }
done

if runuser -u "$READER_USER" -- ls -A -- "$VAULT_ROOT" >/dev/null 2>&1; then
  echo "FAIL: Reader can list Vault root; traversal boundary is broader than --x" >&2
  exit 1
fi
echo "PASS: Reader cannot list Vault root"

PYTHON_ROOT=$(mktemp -d)
chmod 0755 "$PYTHON_ROOT"
cp -a "$REPO_ROOT/src/obsidian_automation" "$PYTHON_ROOT/obsidian_automation"
chmod -R a+rX "$PYTHON_ROOT/obsidian_automation"

NOTE="$KNOWLEDGE/ReaderTraversalGate-${$}.md"
INDEX_SHA=""
CONTEXT_SHA=""

cleanup() {
  rm -f -- "$NOTE"
  [[ -z "$INDEX_SHA" ]] || rm -f -- "$INDEX/$INDEX_SHA.index.json"
  [[ -z "$CONTEXT_SHA" ]] || rm -f -- "$CONTEXT/$CONTEXT_SHA.context.json"
  rm -rf -- "$PYTHON_ROOT"
}
trap cleanup EXIT

runuser -u "$SYNC_USER" -- sh -c 'cat > "$1"' sh "$NOTE" <<'EOF'
---
type: knowledge-note
status: active
category: summary
maturity: draft
source_type: self
---
# Reader traversal Gate

Reader must reach this note without Vault-root listing permission.
EOF

OUTPUT=$(runuser -u "$READER_USER" -- env \
  PYTHONPATH="$PYTHON_ROOT" \
  python3 - "$VAULT_ROOT" "$AI_ROOT" "11-Knowledge/$(basename "$NOTE")" <<'PY'
import sys
from pathlib import Path

from obsidian_automation.context_bundle import build_context_bundle, store_context_bundle
from obsidian_automation.knowledge_index import build_knowledge_index, store_knowledge_index

vault = Path(sys.argv[1])
state = Path(sys.argv[2])
source = sys.argv[3]

index = build_knowledge_index(vault)
index_sha, _ = store_knowledge_index(state, index)
assert any(doc.path == source for doc in index.documents)

bundle = build_context_bundle(
    vault,
    query="Reader traversal Gate",
    source_paths=[source],
    created_at="2026-08-22T00:00:00Z",
)
context_sha, _ = store_context_bundle(state, bundle)
assert bundle.sources[0].path == source

print(index_sha)
print(context_sha)
PY
)

INDEX_SHA=$(printf '%s\n' "$OUTPUT" | sed -n '1p')
CONTEXT_SHA=$(printf '%s\n' "$OUTPUT" | sed -n '2p')

[[ "$INDEX_SHA" =~ ^[0-9a-f]{64}$ && -f "$INDEX/$INDEX_SHA.index.json" ]] || {
  echo "FAIL: Reader did not persist a valid Index artifact" >&2
  exit 1
}
[[ "$CONTEXT_SHA" =~ ^[0-9a-f]{64}$ && -f "$CONTEXT/$CONTEXT_SHA.context.json" ]] || {
  echo "FAIL: Reader did not persist a valid Context artifact" >&2
  exit 1
}

echo "PASS: Reader builds Index through execute-only Vault root"
echo "PASS: Reader builds Context through execute-only Vault root"
echo "Reader Vault traversal Gate PASSED."
