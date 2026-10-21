"""Reusable, Source-neutral Validator and fresh-Retrieval artifact operations.

Legacy Vault/Semantic Jobs and External ChatGPT Source Jobs share these exact
deterministic operations. Context-specific admission and review policy remain
separate: this module never submits a Job, grants approval or mutates the Vault.
"""
from __future__ import annotations

from pathlib import Path

from .evaluation_artifact import (
    build_evaluation_context,
    create_evaluation_request,
    load_evaluation_context,
    store_evaluation_context,
)
from .knowledge_index import build_knowledge_index, store_knowledge_index
from .pre_review_job import PreReviewJobError
from .production_io import mirror_read_lock


def create_bound_evaluation_request(
    ai_root: Path, *,
    proposal_sha256: str,
    mutation_sha256: str,
) -> str:
    """Use the existing accepted Validation→Evaluation Request binding."""
    request_sha, _, request = create_evaluation_request(ai_root, proposal_sha256)
    if (
        request.proposal_sha256 != proposal_sha256
        or request.mutation_sha256 != mutation_sha256
    ):
        raise PreReviewJobError("evaluation request does not match validation")
    return request_sha


def build_bound_fresh_retrieval(
    ai_root: Path,
    vault_root: Path,
    *,
    proposal_sha256: str,
    mutation_sha256: str,
    request_sha256: str,
    top_k: int,
) -> tuple[str, str]:
    """Apply one existing fresh Vault-index+EvaluationContext procedure.

    Caller remains responsible for its input Context/Job authority and for
    committing the selected stage under its own Job state contract.
    """
    with mirror_read_lock(ai_root):
        index = build_knowledge_index(vault_root)
    index_sha, _ = store_knowledge_index(ai_root, index)
    context = build_evaluation_context(
        ai_root,
        vault_root,
        request_sha256=request_sha256,
        index_sha256=index_sha,
        top_k=top_k,
    )
    context_sha, _ = store_evaluation_context(ai_root, context)
    loaded = load_evaluation_context(ai_root, context_sha)
    if (
        loaded.proposal_sha256 != proposal_sha256
        or loaded.mutation_sha256 != mutation_sha256
        or loaded.request_sha256 != request_sha256
    ):
        raise PreReviewJobError("evaluation context binding mismatch")
    return index_sha, context_sha
