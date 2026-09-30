from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _store_immutable,
    sha256_bytes,
)
from .semantic_corpus import (
    SemanticCorpusError,
    load_semantic_corpus_manifest,
    verify_semantic_corpus_current,
)
from .semantic_index import (
    SemanticIndexError,
    load_semantic_index_manifest,
)
from .semantic_retrieval import (
    DEFAULT_RETRIEVAL_PROFILE,
    RETRIEVAL_PROFILES,
    SemanticRetrievalError,
    evaluate_semantic_benchmark,
    load_benchmark_set,
)
from .semantic_selection import (
    POLICIES,
    SemanticSelectionError,
    build_semantic_selection,
    store_semantic_selection,
)
from .planner_cadence import load_cadence_state


RECEIPT_VERSION = 1
RECEIPT_SUFFIX = "semantic-production-acceptance"
DEFAULT_APP_ROOT = Path("/opt/obsidian-automation/app")
DEFAULT_AI_ROOT = Path("/var/lib/obsidian-ai/state")
DEFAULT_VAULT_ROOT = Path("/var/lib/obsidian-ai/vault")
DEFAULT_RECEIPT_DIR = Path(
    "/var/lib/obsidian-ai/deployments/semantic-planner"
)
DEFAULT_REVISION_ENV = Path("/etc/obsidian-ai/pre-review-revision.env")
DEFAULT_INPUT_ENV = Path("/etc/obsidian-ai/pre-review-input.env")
DEFAULT_PLANNER_UNIT = Path(
    "/etc/systemd/system/obsidian-ai-input-planner.service"
)
TIMER_UNIT = "obsidian-pre-review.timer"
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SemanticProductionAcceptanceError(ArtifactLifecycleError):
    """Raised when Semantic Planner production acceptance fails closed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class AcceptanceReceipt:
    stage: str
    result: str
    payload: Mapping[str, object]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "receipt_version": RECEIPT_VERSION,
                "stage": self.stage,
                "result": self.result,
                "payload": dict(self.payload),
            }
        )


def _default_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _require_sha(value: object, *, label: str, full: bool = False) -> str:
    if not isinstance(value, str):
        raise SemanticProductionAcceptanceError(f"{label} must be a string")
    pattern = _SHA256_RE if full else _SHA_RE
    if pattern.fullmatch(value) is None:
        raise SemanticProductionAcceptanceError(f"{label} is invalid")
    return value


def _require_receipt_dir(path: Path) -> Path:
    root = path.absolute()
    _require_safe_directory(root, create=True)
    return root


def _receipt_path(receipt_dir: Path, digest: str) -> Path:
    return receipt_dir / f"{digest}.{RECEIPT_SUFFIX}.json"


def store_receipt(
    receipt_dir: Path,
    receipt: AcceptanceReceipt,
) -> tuple[str, Path]:
    directory = _require_receipt_dir(receipt_dir)
    data = receipt.to_json_bytes()
    parsed = parse_receipt(data)
    if parsed != receipt:
        raise SemanticProductionAcceptanceError(
            "semantic production receipt canonical round-trip mismatch"
        )
    digest = sha256_bytes(data)
    path = _receipt_path(directory, digest)
    return digest, _store_immutable(path, data)


def parse_receipt(data: bytes) -> AcceptanceReceipt:
    value = _decode_json_object(
        data,
        label="semantic production acceptance receipt",
    )
    if set(value) != {"receipt_version", "stage", "result", "payload"}:
        raise SemanticProductionAcceptanceError(
            "semantic production receipt properties do not match contract"
        )
    if value["receipt_version"] != RECEIPT_VERSION:
        raise SemanticProductionAcceptanceError(
            "unsupported semantic production receipt version"
        )
    stage = value["stage"]
    result = value["result"]
    payload = value["payload"]
    if (
        not isinstance(stage, str)
        or not stage
        or not isinstance(result, str)
        or result not in {"passed", "failed"}
        or not isinstance(payload, dict)
    ):
        raise SemanticProductionAcceptanceError(
            "semantic production receipt fields are invalid"
        )
    return AcceptanceReceipt(
        stage=stage,
        result=result,
        payload=payload,
    )


def load_receipt(
    receipt_dir: Path,
    digest: str,
    *,
    expected_stage: str | None = None,
) -> AcceptanceReceipt:
    sha = _require_sha(digest, label="receipt SHA", full=True)
    path = _receipt_path(receipt_dir.absolute(), sha)
    data = _read_exact_file(path)
    if sha256_bytes(data) != sha:
        raise SemanticProductionAcceptanceError(
            "semantic production receipt hash mismatch"
        )
    receipt = parse_receipt(data)
    if expected_stage is not None and receipt.stage != expected_stage:
        raise SemanticProductionAcceptanceError(
            f"receipt stage mismatch: expected {expected_stage}"
        )
    return receipt


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SemanticProductionAcceptanceError(
            f"{label} does not exist"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SemanticProductionAcceptanceError(
            f"{label} must be a non-symlink regular file"
        )


def _read_revision_env(path: Path) -> str:
    _require_regular_file(path, label="pre-review revision env")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SemanticProductionAcceptanceError(
            "cannot read pre-review revision env"
        ) from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticProductionAcceptanceError(
            "pre-review revision env is not UTF-8"
        ) from exc
    lines = [line for line in text.splitlines() if line]
    prefix = "OBSIDIAN_AUTOMATION_REVISION="
    if len(lines) != 1 or not lines[0].startswith(prefix):
        raise SemanticProductionAcceptanceError(
            "pre-review revision env must contain exactly one revision"
        )
    return _require_sha(
        lines[0][len(prefix) :],
        label="deployed revision",
    )


def _read_input_mode(path: Path) -> tuple[str, str | None, str | None]:
    if not os.path.lexists(path):
        return "absent", None, None
    _require_regular_file(path, label="pre-review input env")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SemanticProductionAcceptanceError(
            "cannot read pre-review input env"
        ) from exc
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SemanticProductionAcceptanceError(
                "pre-review input env contains malformed assignment"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in values:
            raise SemanticProductionAcceptanceError(
                f"duplicate pre-review input env key: {key}"
            )
        values[key] = value
    return (
        values.get("AI_INPUT_MODE", "legacy"),
        values.get("AI_INPUT_SEMANTIC_INDEX_SHA"),
        values.get("AI_INPUT_SEMANTIC_SELECTION_POLICY"),
    )


def _runner_ok(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    label: str,
) -> str:
    result = runner(tuple(argv))
    if result.returncode != 0:
        raise SemanticProductionAcceptanceError(
            f"{label} failed with exit status {result.returncode}"
        )
    return result.stdout.strip()


def preflight_acceptance(
    *,
    expected_revision: str,
    app_root: Path = DEFAULT_APP_ROOT,
    revision_env: Path = DEFAULT_REVISION_ENV,
    input_env: Path = DEFAULT_INPUT_ENV,
    planner_unit: Path = DEFAULT_PLANNER_UNIT,
    ai_root: Path = DEFAULT_AI_ROOT,
    runner: CommandRunner = _default_runner,
) -> AcceptanceReceipt:
    revision = _require_sha(
        expected_revision,
        label="expected revision",
    )
    deployed = _read_revision_env(revision_env)
    if deployed != revision:
        raise SemanticProductionAcceptanceError(
            "deployed revision env does not match expected revision"
        )
    head = _runner_ok(
        runner,
        ("git", "-C", str(app_root), "rev-parse", "HEAD"),
        label="read production HEAD",
    )
    if head != revision:
        raise SemanticProductionAcceptanceError(
            "production checkout HEAD does not match expected revision"
        )
    branch = _runner_ok(
        runner,
        ("git", "-C", str(app_root), "branch", "--show-current"),
        label="read production branch",
    )
    if branch != "main":
        raise SemanticProductionAcceptanceError(
            "production checkout is not on main"
        )
    dirty = _runner_ok(
        runner,
        ("git", "-C", str(app_root), "status", "--porcelain"),
        label="read production worktree status",
    )
    if dirty:
        raise SemanticProductionAcceptanceError(
            "production checkout is dirty"
        )

    _require_regular_file(planner_unit, label="Input Planner unit")
    try:
        unit_text = planner_unit.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SemanticProductionAcceptanceError(
            "cannot read Input Planner unit"
        ) from exc
    if "Environment=AI_INPUT_MODE=legacy" not in unit_text:
        raise SemanticProductionAcceptanceError(
            "Input Planner unit does not default to legacy mode"
        )

    input_mode, configured_index, configured_policy = _read_input_mode(input_env)
    if input_mode not in {"absent", "legacy"}:
        raise SemanticProductionAcceptanceError(
            "Semantic Planner is already enabled before acceptance"
        )

    selection_dir = ai_root.absolute() / "02-Orchestration" / "semantic-selections"
    try:
        info = selection_dir.lstat()
    except FileNotFoundError as exc:
        raise SemanticProductionAcceptanceError(
            "Reader-only Semantic Selection Store is not provisioned"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SemanticProductionAcceptanceError(
            "Semantic Selection Store path is unsafe"
        )

    access_checks = (
        ("obsidian-ai-reader", "-r", True),
        ("obsidian-ai-reader", "-w", True),
        ("obsidian-ai-generator", "-r", False),
        ("obsidian-ai-generator", "-w", False),
        ("obsidian-ai-validator", "-r", False),
        ("obsidian-ai-evaluator", "-r", False),
    )
    for user, flag, expected in access_checks:
        result = runner(
            (
                "runuser",
                "-u",
                user,
                "--",
                "test",
                flag,
                str(selection_dir),
            )
        )
        actual = result.returncode == 0
        if actual != expected:
            raise SemanticProductionAcceptanceError(
                f"Semantic Selection Store authority check failed: "
                f"{user} {flag}"
            )

    timer_enabled = _runner_ok(
        runner,
        ("systemctl", "is-enabled", TIMER_UNIT),
        label="read pre-review timer enablement",
    )
    timer_active = _runner_ok(
        runner,
        ("systemctl", "is-active", TIMER_UNIT),
        label="read pre-review timer activity",
    )
    if timer_enabled != "enabled" or timer_active != "active":
        raise SemanticProductionAcceptanceError(
            "pre-review timer must be enabled and active for rollout acceptance"
        )

    return AcceptanceReceipt(
        stage="preflight",
        result="passed",
        payload={
            "expected_revision": revision,
            "production_head": head,
            "production_branch": branch,
            "production_clean": True,
            "input_mode": input_mode,
            "configured_semantic_index_sha256": configured_index,
            "configured_semantic_selection_policy": configured_policy,
            "planner_unit_default_mode": "legacy",
            "semantic_selection_store": str(selection_dir),
            "semantic_selection_store_reader_only": True,
            "pre_review_timer_enabled": True,
            "pre_review_timer_active": True,
        },
    )


def verify_index_acceptance(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
) -> AcceptanceReceipt:
    index_sha = _require_sha(
        semantic_index_sha256,
        label="semantic index SHA",
        full=True,
    )
    try:
        index = load_semantic_index_manifest(ai_root, index_sha)
        corpus = load_semantic_corpus_manifest(
            ai_root,
            index.corpus_manifest_sha256,
        )
        verify_semantic_corpus_current(vault_root, corpus)
    except (
        ArtifactLifecycleError,
        SemanticCorpusError,
        SemanticIndexError,
        OSError,
    ) as exc:
        raise SemanticProductionAcceptanceError(str(exc)) from exc

    return AcceptanceReceipt(
        stage="verify-index",
        result="passed",
        payload={
            "semantic_index_sha256": index_sha,
            "corpus_manifest_sha256": index.corpus_manifest_sha256,
            "embedding_plan_sha256": index.embedding_plan_sha256,
            "embedding_result_set_sha256": index.embedding_result_set_sha256,
            "chunk_policy": index.chunk_policy,
            "provider": index.provider,
            "adapter_version": index.adapter_version,
            "model_identifier": index.model_identifier,
            "model_revision": index.model_revision,
            "vector_dimension": index.vector_dimension,
            "vector_encoding": index.vector_encoding,
            "source_kind_counts": dict(
                sorted(index.source_kind_counts.items())
            ),
            "vector_count": len(index.vectors),
            "current_vault_binding": True,
        },
    )


def benchmark_acceptance(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
    benchmark_path: Path,
    benchmark_plan_sha256: str,
    benchmark_result_set_sha256: str,
    top_k: int = 3,
    retrieval_profile: str = DEFAULT_RETRIEVAL_PROFILE,
) -> AcceptanceReceipt:
    index_sha = _require_sha(
        semantic_index_sha256,
        label="semantic index SHA",
        full=True,
    )
    try:
        benchmark = load_benchmark_set(benchmark_path)
        report = evaluate_semantic_benchmark(
            ai_root,
            vault_root,
            semantic_index_sha256=index_sha,
            benchmark=benchmark,
            plan_sha256=benchmark_plan_sha256,
            result_set_sha256=benchmark_result_set_sha256,
            top_k=top_k,
            retrieval_profile=retrieval_profile,
        )
    except (
        ArtifactLifecycleError,
        SemanticRetrievalError,
        OSError,
    ) as exc:
        raise SemanticProductionAcceptanceError(str(exc)) from exc

    acceptance = report["acceptance"]
    passed = bool(acceptance["passed"])
    payload = {
        "semantic_index_sha256": index_sha,
        "benchmark_name": report["name"],
        "benchmark_sha256": report["benchmark_sha256"],
        "benchmark_plan_sha256": report["benchmark_plan_sha256"],
        "benchmark_result_set_sha256": report[
            "benchmark_result_set_sha256"
        ],
        "top_k": report["top_k"],
        "retrieval_profile": report["retrieval_profile"],
        "lexical_weight": report["lexical_weight"],
        "vector_weight": report["vector_weight"],
        "metrics": report["metrics"],
        "acceptance": acceptance,
    }
    return AcceptanceReceipt(
        stage="benchmark",
        result="passed" if passed else "failed",
        payload=payload,
    )


def observe_selection_acceptance(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
    selection_policy: str,
) -> AcceptanceReceipt:
    index_sha = _require_sha(
        semantic_index_sha256,
        label="semantic index SHA",
        full=True,
    )
    if selection_policy not in POLICIES:
        raise SemanticProductionAcceptanceError(
            "unsupported semantic selection policy"
        )
    before = load_cadence_state(ai_root)
    try:
        selection = build_semantic_selection(
            ai_root,
            vault_root,
            semantic_index_sha256=index_sha,
            policy=selection_policy,
        )
        selection_sha, _ = store_semantic_selection(ai_root, selection)
    except (
        ArtifactLifecycleError,
        SemanticSelectionError,
        OSError,
    ) as exc:
        raise SemanticProductionAcceptanceError(str(exc)) from exc
    after = load_cadence_state(ai_root)
    if before != after:
        raise SemanticProductionAcceptanceError(
            "selection observation mutated Planner cadence state"
        )

    if (
        selection.selection_policy == "semantic-project-distill-v1"
        and selection.policy_observations.get("retrieval_profile")
        != "semantic-retrieval-v1"
    ):
        raise SemanticProductionAcceptanceError(
            "Selection Record retrieval profile is invalid"
        )

    source_kinds: dict[str, int] = {}
    for item in selection.selected:
        source_kinds[item.source_kind] = (
            source_kinds.get(item.source_kind, 0) + 1
        )
    return AcceptanceReceipt(
        stage="observe-selection",
        result="passed",
        payload={
            "semantic_index_sha256": index_sha,
            "selection_policy": selection.selection_policy,
            "retrieval_profile": (
                selection.policy_observations.get("retrieval_profile")
                if selection.selection_policy == "semantic-project-distill-v1"
                else DEFAULT_RETRIEVAL_PROFILE
            ),
            "selection_sha256": selection_sha,
            "decision": selection.novelty.decision,
            "skip_reason": selection.novelty.skip_reason,
            "selected_count": len(selection.selected),
            "selected_source_kinds": dict(sorted(source_kinds.items())),
            "cluster_coherence": selection.novelty.cluster_coherence,
            "recent_context_max_similarity": (
                selection.novelty.recent_context_max_similarity
            ),
            "knowledge_max_similarity": (
                selection.novelty.knowledge_max_similarity
            ),
            "cadence_state_unchanged": True,
        },
    )


def plan_canary_acceptance(
    receipt_dir: Path,
    *,
    preflight_receipt_sha256: str,
    index_receipt_sha256: str,
    benchmark_receipt_sha256: str,
    selection_receipt_sha256: str,
) -> AcceptanceReceipt:
    preflight = load_receipt(
        receipt_dir,
        preflight_receipt_sha256,
        expected_stage="preflight",
    )
    index = load_receipt(
        receipt_dir,
        index_receipt_sha256,
        expected_stage="verify-index",
    )
    benchmark = load_receipt(
        receipt_dir,
        benchmark_receipt_sha256,
        expected_stage="benchmark",
    )
    selection = load_receipt(
        receipt_dir,
        selection_receipt_sha256,
        expected_stage="observe-selection",
    )
    for receipt in (preflight, index, benchmark, selection):
        if receipt.result != "passed":
            raise SemanticProductionAcceptanceError(
                f"{receipt.stage} acceptance receipt did not pass"
            )

    index_sha = index.payload.get("semantic_index_sha256")
    if (
        index_sha != benchmark.payload.get("semantic_index_sha256")
        or index_sha != selection.payload.get("semantic_index_sha256")
    ):
        raise SemanticProductionAcceptanceError(
            "acceptance receipts do not bind the same Semantic Index"
        )
    policy = selection.payload.get("selection_policy")
    if not isinstance(policy, str) or policy not in POLICIES:
        raise SemanticProductionAcceptanceError(
            "selection receipt policy is invalid"
        )
    benchmark_profile = benchmark.payload.get("retrieval_profile")
    selection_profile = selection.payload.get("retrieval_profile")
    if (
        not isinstance(benchmark_profile, str)
        or benchmark_profile not in RETRIEVAL_PROFILES
        or benchmark_profile != selection_profile
    ):
        raise SemanticProductionAcceptanceError(
            "benchmark and selection retrieval profiles do not match"
        )
    acceptance = benchmark.payload.get("acceptance")
    if not isinstance(acceptance, dict) or acceptance.get("passed") is not True:
        raise SemanticProductionAcceptanceError(
            "benchmark acceptance is not passed"
        )
    revision = preflight.payload.get("expected_revision")
    if not isinstance(revision, str):
        raise SemanticProductionAcceptanceError(
            "preflight receipt revision is invalid"
        )

    env_lines = [
        "AI_INPUT_MODE=semantic-deep-knowledge",
        f"AI_INPUT_SEMANTIC_INDEX_SHA={index_sha}",
        f"AI_INPUT_SEMANTIC_SELECTION_POLICY={policy}",
    ]
    return AcceptanceReceipt(
        stage="plan-canary",
        result="passed",
        payload={
            "expected_revision": revision,
            "semantic_index_sha256": index_sha,
            "semantic_selection_policy": policy,
            "retrieval_profile": benchmark_profile,
            "selection_observation_decision": selection.payload.get(
                "decision"
            ),
            "preflight_receipt_sha256": preflight_receipt_sha256,
            "index_receipt_sha256": index_receipt_sha256,
            "benchmark_receipt_sha256": benchmark_receipt_sha256,
            "selection_receipt_sha256": selection_receipt_sha256,
            "env_plan": env_lines,
            "mutation_performed": False,
        },
    )


def _emit_and_store(
    receipt_dir: Path,
    receipt: AcceptanceReceipt,
) -> dict[str, object]:
    digest, path = store_receipt(receipt_dir, receipt)
    return {
        "event": "semantic-production-acceptance",
        "stage": receipt.stage,
        "result": receipt.result,
        "receipt_sha256": digest,
        "receipt_path": str(path),
        "payload": dict(receipt.payload),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-semantic-production-acceptance"
    )
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=DEFAULT_RECEIPT_DIR,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--expected-revision", required=True)
    preflight.add_argument("--app-root", type=Path, default=DEFAULT_APP_ROOT)
    preflight.add_argument(
        "--revision-env",
        type=Path,
        default=DEFAULT_REVISION_ENV,
    )
    preflight.add_argument(
        "--input-env",
        type=Path,
        default=DEFAULT_INPUT_ENV,
    )
    preflight.add_argument(
        "--planner-unit",
        type=Path,
        default=DEFAULT_PLANNER_UNIT,
    )
    preflight.add_argument("--ai-root", type=Path, default=DEFAULT_AI_ROOT)

    verify_index = subparsers.add_parser("verify-index")
    verify_index.add_argument("--ai-root", type=Path, default=DEFAULT_AI_ROOT)
    verify_index.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
    )
    verify_index.add_argument("--semantic-index-sha", required=True)

    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--ai-root", type=Path, default=DEFAULT_AI_ROOT)
    benchmark.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
    )
    benchmark.add_argument("--semantic-index-sha", required=True)
    benchmark.add_argument("--benchmark", type=Path, required=True)
    benchmark.add_argument("--plan-sha", required=True)
    benchmark.add_argument("--result-set-sha", required=True)
    benchmark.add_argument("--top-k", type=int, default=3)
    benchmark.add_argument(
        "--retrieval-profile",
        choices=tuple(RETRIEVAL_PROFILES),
        default=DEFAULT_RETRIEVAL_PROFILE,
    )

    observe = subparsers.add_parser("observe-selection")
    observe.add_argument("--ai-root", type=Path, default=DEFAULT_AI_ROOT)
    observe.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
    )
    observe.add_argument("--semantic-index-sha", required=True)
    observe.add_argument("--policy", choices=POLICIES, required=True)

    canary = subparsers.add_parser("plan-canary")
    canary.add_argument("--preflight-receipt-sha", required=True)
    canary.add_argument("--index-receipt-sha", required=True)
    canary.add_argument("--benchmark-receipt-sha", required=True)
    canary.add_argument("--selection-receipt-sha", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            receipt = preflight_acceptance(
                expected_revision=args.expected_revision,
                app_root=args.app_root,
                revision_env=args.revision_env,
                input_env=args.input_env,
                planner_unit=args.planner_unit,
                ai_root=args.ai_root,
            )
        elif args.command == "verify-index":
            receipt = verify_index_acceptance(
                args.ai_root,
                args.vault_root,
                semantic_index_sha256=args.semantic_index_sha,
            )
        elif args.command == "benchmark":
            receipt = benchmark_acceptance(
                args.ai_root,
                args.vault_root,
                semantic_index_sha256=args.semantic_index_sha,
                benchmark_path=args.benchmark,
                benchmark_plan_sha256=args.plan_sha,
                benchmark_result_set_sha256=args.result_set_sha,
                top_k=args.top_k,
                retrieval_profile=args.retrieval_profile,
            )
        elif args.command == "observe-selection":
            receipt = observe_selection_acceptance(
                args.ai_root,
                args.vault_root,
                semantic_index_sha256=args.semantic_index_sha,
                selection_policy=args.policy,
            )
        else:
            receipt = plan_canary_acceptance(
                args.receipt_dir,
                preflight_receipt_sha256=args.preflight_receipt_sha,
                index_receipt_sha256=args.index_receipt_sha,
                benchmark_receipt_sha256=args.benchmark_receipt_sha,
                selection_receipt_sha256=args.selection_receipt_sha,
            )
        result = _emit_and_store(args.receipt_dir, receipt)
    except (
        ArtifactLifecycleError,
        SemanticProductionAcceptanceError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if receipt.stage == "benchmark" and receipt.result != "passed":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
