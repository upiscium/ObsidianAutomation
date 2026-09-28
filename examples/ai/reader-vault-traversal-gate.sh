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
DAILY="$VAULT_ROOT/00-DailyNote"
IDEAS="$VAULT_ROOT/05-Idea"
KNOWLEDGE="$VAULT_ROOT/11-Knowledge"
PROJECTS="$VAULT_ROOT/10-Project"
INDEX="$AI_ROOT/04-Index"
CONTEXT="$AI_ROOT/05-Context"

for directory in "$VAULT_ROOT" "$DAILY" "$IDEAS" "$KNOWLEDGE" "$PROJECTS" "$INDEX" "$CONTEXT"; do
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

DAILY_NOTE="$DAILY/ReaderTraversalGate-${$}.md"
IDEA_NOTE="$IDEAS/ReaderTraversalGate-${$}.md"
NOTE="$KNOWLEDGE/ReaderTraversalGate-${$}.md"
PROJECT_DIR="$PROJECTS/ReaderTraversalGate-${$}"
PROJECT_ENTRY="$PROJECT_DIR/ReaderTraversalGate-${$}.md"
PROJECT_NOTE="$PROJECT_DIR/Design.md"
INDEX_SHA=""
CONTEXT_SHA=""
SEMANTIC_SHA=""

cleanup() {
  rm -f -- "$DAILY_NOTE" "$IDEA_NOTE" "$NOTE"
  rm -rf -- "$PROJECT_DIR"
  [[ -z "$INDEX_SHA" ]] || rm -f -- "$INDEX/$INDEX_SHA.index.json"
  [[ -z "$CONTEXT_SHA" ]] || rm -f -- "$CONTEXT/$CONTEXT_SHA.context.json"
  [[ -z "$SEMANTIC_SHA" ]] || rm -f -- "$INDEX/semantic-corpus/$SEMANTIC_SHA.semantic-corpus.json"
  rm -rf -- "$PYTHON_ROOT"
}
trap cleanup EXIT

runuser -u "$SYNC_USER" -- sh -c 'cat > "$1"' sh "$DAILY_NOTE" <<'EOF'
---
type: daily-review
---
# Note
Reader may use this Daily semantic content.
# Tasks
- [ ] ignored by Semantic Corpus
EOF

runuser -u "$SYNC_USER" -- sh -c 'cat > "$1"' sh "$IDEA_NOTE" <<'EOF'
---
type: idea
title: Reader traversal Idea
created: 2026-09-28
workspace: "[[03-Workspace/Test/Test|Test]]"
project:
status: active
tags: []
---
# Reader traversal Idea
Reader may use this active Idea as semantic corpus material.
EOF

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

runuser -u "$SYNC_USER" -- mkdir -p -- "$PROJECT_DIR"
runuser -u "$SYNC_USER" -- sh -c 'cat > "$1"' sh "$PROJECT_ENTRY" <<'EOF'
---
type: project
workspace: "[[03-Workspace/Test/Test|Test]]"
status: running
priority: medium
---
# Project Summary
Reader semantic Project anchor.
EOF

runuser -u "$SYNC_USER" -- sh -c 'cat > "$1"' sh "$PROJECT_NOTE" <<EOF
---
type: project-note
lifecycle: active
project: "[[10-Project/ReaderTraversalGate-$/ReaderTraversalGate-$|ReaderTraversalGate]]"
workspace: "[[03-Workspace/Test/Test|Test]]"
---
# Reader Project Note traversal Gate

Reader may use this active Project Note as Generation context.
EOF

OUTPUT=$(runuser -u "$READER_USER" -- env \
  PYTHONPATH="$PYTHON_ROOT" \
  python3 - "$VAULT_ROOT" "$AI_ROOT" \
    "00-DailyNote/$(basename "$DAILY_NOTE")" \
    "05-Idea/$(basename "$IDEA_NOTE")" \
    "11-Knowledge/$(basename "$NOTE")" \
    "10-Project/$(basename "$PROJECT_DIR")/Design.md" <<'PY'
import sys
from pathlib import Path

from obsidian_automation.context_bundle import build_context_bundle, store_context_bundle
from obsidian_automation.knowledge_index import build_knowledge_index, store_knowledge_index
from obsidian_automation.semantic_corpus import build_semantic_corpus, store_semantic_corpus_manifest

vault = Path(sys.argv[1])
state = Path(sys.argv[2])
daily_source = sys.argv[3]
idea_source = sys.argv[4]
source = sys.argv[5]
project_source = sys.argv[6]

index = build_knowledge_index(vault)
index_sha, _ = store_knowledge_index(state, index)
assert any(doc.path == source for doc in index.documents)

bundle = build_context_bundle(
    vault,
    query="Reader traversal Gate",
    source_paths=[source, project_source],
    created_at="2026-08-22T00:00:00Z",
)
context_sha, _ = store_context_bundle(state, bundle)
assert {item.path for item in bundle.sources} == {source, project_source}

semantic = build_semantic_corpus(vault)
by_path = {item.path: item for item in semantic.sources}
assert by_path[daily_source].source_kind == "daily"
assert by_path[idea_source].source_kind == "idea"
assert by_path[project_source].source_kind == "project-note"
assert by_path[source].source_kind == "knowledge"
semantic_sha, _ = store_semantic_corpus_manifest(state, semantic)

print(index_sha)
print(context_sha)
print(semantic_sha)
PY
)

INDEX_SHA=$(printf '%s\n' "$OUTPUT" | sed -n '1p')
CONTEXT_SHA=$(printf '%s\n' "$OUTPUT" | sed -n '2p')
SEMANTIC_SHA=$(printf '%s\n' "$OUTPUT" | sed -n '3p')

[[ "$INDEX_SHA" =~ ^[0-9a-f]{64}$ && -f "$INDEX/$INDEX_SHA.index.json" ]] || {
  echo "FAIL: Reader did not persist a valid Index artifact" >&2
  exit 1
}
[[ "$CONTEXT_SHA" =~ ^[0-9a-f]{64}$ && -f "$CONTEXT/$CONTEXT_SHA.context.json" ]] || {
  echo "FAIL: Reader did not persist a valid Context artifact" >&2
  exit 1
}
[[ "$SEMANTIC_SHA" =~ ^[0-9a-f]{64}$ && -f "$INDEX/semantic-corpus/$SEMANTIC_SHA.semantic-corpus.json" ]] || {
  echo "FAIL: Reader did not persist a valid Semantic Corpus artifact" >&2
  exit 1
}

echo "PASS: Reader builds Index through execute-only Vault root"
echo "PASS: Reader builds Context through execute-only Vault root"
echo "PASS: Reader builds Daily/Idea/Project/Knowledge Semantic Corpus through execute-only Vault root"
echo "Reader Vault traversal Gate PASSED."
