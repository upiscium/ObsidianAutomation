#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

: "${AI_ROOT:?set AI_ROOT to the disposable AI state root}"
REPO_ROOT=${REPO_ROOT:-"$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"}

EXECUTOR_USER=${EXECUTOR_USER:-obsidian-ai-executor}
SYNC_USER=${SYNC_USER:-obsidian-ai-sync}
REVIEWER_USER=${REVIEWER_USER:-obsidian-ai-reviewer}
VALIDATOR_USER=${VALIDATOR_USER:-obsidian-ai-validator}
READER_USER=${READER_USER:-obsidian-ai-reader}
GENERATOR_USER=${GENERATOR_USER:-obsidian-ai-generator}
EVALUATOR_USER=${EVALUATOR_USER:-obsidian-ai-evaluator}

LOCK_DIR="$AI_ROOT/24-Locks"
DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
LOCK_PATH="$LOCK_DIR/$DIGEST.lock"
READ_VIEW_LOCK_DIR="$LOCK_DIR/read-view"
READ_VIEW_LOCK_PATH="$READ_VIEW_LOCK_DIR/mirror-read.lock"

[[ -d "$LOCK_DIR" && ! -L "$LOCK_DIR" ]] || {
  echo "ERROR: unsafe or missing lock directory: $LOCK_DIR" >&2
  exit 1
}
[[ -d "$REPO_ROOT/src/obsidian_automation" ]] || {
  echo "ERROR: repository source tree not found: $REPO_ROOT" >&2
  exit 1
}

for command in runuser python3 getfacl mktemp cp chmod; do
  command -v "$command" >/dev/null || {
    echo "ERROR: required command not found: $command" >&2
    exit 1
  }
done

# GitHub Actions checkout parents are not necessarily traversable by the
# fixture system users. Copy only the package under test to a disposable,
# read-only-for-actors import root so the Gate tests ACLs rather than checkout
# directory permissions.
PYTHON_ROOT=$(mktemp -d)
chmod 0755 "$PYTHON_ROOT"
cp -a "$REPO_ROOT/src/obsidian_automation" "$PYTHON_ROOT/obsidian_automation"
chmod -R a+rX "$PYTHON_ROOT/obsidian_automation"

cleanup() {
  rm -f -- "$LOCK_PATH" "$READ_VIEW_LOCK_PATH"
  rm -rf -- "$PYTHON_ROOT"
}
trap cleanup EXIT
rm -f -- "$LOCK_PATH"

failures=0
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }

probe_lock() {
  local user=$1 expected=$2 label=$3
  local output
  output=$(mktemp)

  if runuser -u "$user" -- env \
    PYTHONPATH="$PYTHON_ROOT" \
    python3 - "$LOCK_DIR" "$DIGEST" >"$output" 2>&1 <<'PY'
import sys
from pathlib import Path

from obsidian_automation.production_orchestrator import _production_lock

with _production_lock(Path(sys.argv[1]), sys.argv[2]):
    pass
PY
  then
    if [[ $expected == allow ]]; then
      pass "$label"
    else
      fail "$label (unexpected lock open succeeded)"
      cat "$output" >&2
    fi
  else
    if [[ $expected == deny ]]; then
      pass "$label"
    else
      fail "$label (expected lock open failed)"
      cat "$output" >&2
    fi
  fi

  rm -f -- "$output"
}

probe_read_view_lock() {
  local user=$1 expected=$2 label=$3
  local output
  output=$(mktemp)

  if runuser -u "$user" -- env \
    PYTHONPATH="$PYTHON_ROOT" \
    python3 - "$AI_ROOT" >"$output" 2>&1 <<'PY'
import sys
from pathlib import Path

from obsidian_automation.production_io import mirror_read_lock

with mirror_read_lock(Path(sys.argv[1])):
    pass
PY
  then
    if [[ $expected == allow ]]; then
      pass "$label"
    else
      fail "$label (unexpected mirror read-view lock succeeded)"
      cat "$output" >&2
    fi
  else
    if [[ $expected == deny ]]; then
      pass "$label"
    else
      fail "$label (expected mirror read-view lock failed)"
      cat "$output" >&2
    fi
  fi

  rm -f -- "$output"
}

[[ -d "$READ_VIEW_LOCK_DIR" && ! -L "$READ_VIEW_LOCK_DIR" ]] || {
  echo "ERROR: unsafe or missing read-view lock directory: $READ_VIEW_LOCK_DIR" >&2
  exit 1
}

probe_read_view_lock "$SYNC_USER" allow "Sync opens mirror read-view lock"
probe_read_view_lock "$READER_USER" allow "Reader opens mirror read-view lock"
probe_read_view_lock "$GENERATOR_USER" deny "Generator cannot open mirror read-view lock"
probe_read_view_lock "$VALIDATOR_USER" deny "Validator cannot open mirror read-view lock"
probe_read_view_lock "$EVALUATOR_USER" deny "Evaluator cannot open mirror read-view lock"
probe_read_view_lock "$REVIEWER_USER" deny "Reviewer cannot open mirror read-view lock"
probe_read_view_lock "$EXECUTOR_USER" deny "Executor cannot open mirror read-view lock"

# The first actor creates the per-mutation lock. The next two actors must be
# able to open that exact inode; this reproduces the production handoff from
# Executor -> Sync Transport -> Human recovery.
probe_lock "$EXECUTOR_USER" allow "Executor creates production mutation lock"

[[ -f "$LOCK_PATH" && ! -L "$LOCK_PATH" ]] || {
  echo "ERROR: Executor did not create a safe lock file" >&2
  exit 1
}

# Keep the ACL in CI output because a mode such as 0600 can silently mask the
# inherited named-user entries even when the parent directory ACL is correct.
getfacl -p "$LOCK_PATH"

probe_lock "$SYNC_USER" allow "Sync opens Executor-created production mutation lock"
probe_lock "$REVIEWER_USER" allow "Reviewer opens Executor-created production mutation lock"

# Shared operational lock access must not leak to semantic-only identities.
probe_lock "$VALIDATOR_USER" deny "Validator cannot open production mutation lock"
probe_lock "$READER_USER" deny "Reader cannot open production mutation lock"
probe_lock "$GENERATOR_USER" deny "Generator cannot open production mutation lock"

# The shared pre-review SQLite file is created by Reader but must remain
# writable by the four machine-stage identities only. Exercise the actual
# SQLite transaction path across identities so a mode/ACL mask regression
# cannot hide behind directory-only probes.
if runuser -u "$READER_USER" -- env PYTHONPATH="$PYTHON_ROOT" python3 - "$AI_ROOT" <<'PY'
import json
import sys
from pathlib import Path

from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generator_contract import (
    PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as generator_prompt_sha256,
)
from obsidian_automation.openai_evaluator import ADAPTER_VERSION as EVAL_ADAPTER, EVALUATION_STRATEGY
from obsidian_automation.openai_generator import ADAPTER_VERSION as GEN_ADAPTER
from obsidian_automation.pre_review_job import parse_recipe, submit_job

root = Path(sys.argv[1])
context_sha, _ = store_context_bundle(
    root,
    ContextBundle(
        query="authority fixture pre-review job",
        created_at="2026-09-19T00:00:00Z",
        sources=(),
    ),
)
recipe = {
    "record_version": 1,
    "pipeline": "knowledge-pre-review-v0",
    "generator": {
        "implementation_revision": "a" * 40,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "prompt_template_sha256": generator_prompt_sha256(),
        "provider": "openai-compatible",
        "model_identifier": "fixture-generator",
        "model_revision": "identifier:fixture-generator",
        "model_config": {
            "adapter_version": GEN_ADAPTER,
            "identity_binding": "identifier-only",
            "options": {"temperature": 0},
        },
    },
    "validator": {"policy": "knowledge-note-v0"},
    "evaluation_context": {
        "selection_policy": "bm25-topk-recall-v0",
        "top_k": 5,
    },
    "evaluator": {
        "implementation_revision": "a" * 40,
        "prompt_template_version": EVALUATOR_PROMPT_TEMPLATE_VERSION,
        "prompt_template_sha256": evaluator_prompt_sha256(),
        "provider": "openai-compatible",
        "model_identifier": "fixture-evaluator",
        "model_revision": "identifier:fixture-evaluator",
        "model_config": {
            "adapter_version": EVAL_ADAPTER,
            "identity_binding": "identifier-only",
            "strategy": EVALUATION_STRATEGY,
            "options": {"temperature": 0},
        },
    },
}
parsed = parse_recipe((json.dumps(recipe, separators=(",", ":")) + "\n").encode())
result = submit_job(root, context_sha256=context_sha, recipe=parsed)
assert result["created"] is True
PY
then
  pass "Reader creates shared pre-review SQLite job"
else
  fail "Reader creates shared pre-review SQLite job"
fi

run_stage() {
  local user=$1 stage=$2 label=$3
  if runuser -u "$user" -- env PYTHONPATH="$PYTHON_ROOT" python3 - "$AI_ROOT" "$stage" <<'PY'
import sys
from pathlib import Path

from obsidian_automation.pre_review_job import (
    claim_next_attempt,
    complete_attempt,
    stage_output,
)

root = Path(sys.argv[1])
stage = sys.argv[2]
work = claim_next_attempt(root, stage)
assert work is not None

if stage == "generation":
    output = {
        "proposal_sha256": "1" * 64,
        "generation_sha256": "2" * 64,
    }
elif stage == "validation":
    previous = stage_output(root, work.generation_id, "generation")
    assert previous is not None
    output = {
        **previous,
        "mutation_sha256": "3" * 64,
        "request_sha256": "4" * 64,
    }
elif stage == "evaluation_context":
    previous = stage_output(root, work.generation_id, "validation")
    assert previous is not None
    output = {
        **previous,
        "index_sha256": "5" * 64,
        "evaluation_context_sha256": "6" * 64,
    }
elif stage == "evaluation":
    previous = stage_output(root, work.generation_id, "evaluation_context")
    assert previous is not None
    output = {
        **previous,
        "evaluation_sha256": "7" * 64,
        "recommendation": "manual_review",
    }
else:
    raise AssertionError(stage)

complete_attempt(root, work.attempt_id, outcome="succeeded", output=output)
PY
  then
    pass "$label"
  else
    fail "$label"
  fi
}

run_stage "$GENERATOR_USER" generation "Generator updates Reader-created orchestration DB"
run_stage "$VALIDATOR_USER" validation "Validator updates shared orchestration DB"
run_stage "$READER_USER" evaluation_context "Reader updates shared orchestration DB"
run_stage "$EVALUATOR_USER" evaluation "Evaluator updates shared orchestration DB"

ORCHESTRATION_DB="$AI_ROOT/02-Orchestration/pre-review-jobs.sqlite3"
[[ -f "$ORCHESTRATION_DB" && ! -L "$ORCHESTRATION_DB" ]] || {
  fail "Pre-review orchestration DB is a safe regular file"
}
getfacl -p "$ORCHESTRATION_DB"

if (( failures != 0 )); then
  echo "Shared production lock Gate FAILED: $failures probe(s) failed." >&2
  exit 1
fi

echo "Shared production lock Gate PASSED."
