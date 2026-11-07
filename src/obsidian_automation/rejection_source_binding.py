"""Verify the exact original producers of rejected-case Human projections.

Called only by read-only Phase A preflight. All source bytes are loaded using
the bounded nofollow file reader supplied by the caller.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .artifact_lifecycle import _decode_json_object, _require_safe_directory, _require_sha256, sha256_bytes
from .context_bundle import parse_context_bundle
from .generation_artifact import parse_generation_record
from .human_projection import (
    ProjectionRequest, _code_fence, _inline_code, _projection_markdown,
)
from .semantic_objective import parse_objective_context
from .semantic_selection import parse_semantic_selection

SafeReader = Callable[..., bytes]


class ProducerLinkageError(ValueError):
    """One of the selected inputs does not match its immutable producer."""


def _source(
    root: Path, dirname: str, digest: str, suffix: str,
    *, read: SafeReader, cap: int = 2 * 1024 * 1024,
) -> bytes:
    source_sha = _require_sha256(digest, label="producer source SHA")
    path = root / dirname
    _require_safe_directory(path, create=False)
    data = read(path / f"{source_sha}.{suffix}", cap=cap)
    if sha256_bytes(data) != source_sha:
        raise ProducerLinkageError("original producer source digest mismatch")
    return data


def proposal_fields(
    root: Path, proposal_sha256: str, *, read: SafeReader,
) -> tuple[str, str]:
    data = _source(root, "00-Untrusted", proposal_sha256,
                   "proposal.json", read=read, cap=768 * 1024)
    v = _decode_json_object(data, label="original proposal")
    if (
        set(v) != {"contract_version", "operation", "mutation_id", "target", "content"}
        or type(v["contract_version"]) is not int
        or v["contract_version"] != 1
        or v["operation"] != "create_note"
        or not isinstance(v["mutation_id"], str)
        or not v["mutation_id"]
        or not isinstance(v["target"], dict)
        or set(v["target"]) != {"path"}
        or not isinstance(v["target"]["path"], str)
        or not isinstance(v["content"], str)
        or not v["content"]
    ):
        raise ProducerLinkageError("original proposal is not a bounded create_note")
    return v["target"]["path"], v["content"]


def _rebuild_input(
    *, case: str, selection_sha: str, policy: str, objective: str,
    epoch: int, cycle: int, selected: list[tuple[str, str]], created: str,
) -> str:
    if (
        type(epoch) is not int or not 0 <= epoch <= 10**9
        or type(cycle) is not int or not 0 <= cycle <= 10**9
        or not 1 <= len(selected) <= 64
    ):
        raise ProducerLinkageError("Input selection presentation is invalid")
    body = [
        f"Selection policy: {_inline_code(policy)}",
        f"Objective: {_inline_code(objective)}",
        f"Coverage epoch: {epoch}",
        f"Scheduler cycle: {cycle}",
        "", "## Sources",
    ]
    body.extend(f"- {_inline_code(path)} — {kind}" for path, kind in selected)
    return _projection_markdown(
        case_id=case, stage="input", source_kind="input_selection",
        source_sha256=selection_sha, created_at=created,
        title="AI Input", body="\n".join(body),
    )


def verify_original_producers(
    root: Path, case: str, context_sha: str, proposal_sha: str,
    generation_sha: str, originals: dict[str, tuple[str, ProjectionRequest, str]],
    *, read: SafeReader,
) -> None:
    """Reconstruct and compare the original Input, Context and Generation bytes."""
    inp = originals["input"][1]
    ctx_req = originals["context"][1]
    gen_req = originals["generation"][1]
    if (
        inp.source_kind != "input_selection"
        or ctx_req.source_sha256 != context_sha
        or gen_req.source_kind != "generation_record"
        or gen_req.source_sha256 != generation_sha
    ):
        raise ProducerLinkageError("original producer stage source identity mismatch")

    semantic = ctx_req.source_kind == "semantic_objective_context"
    if ctx_req.source_kind == "context_bundle":
        context = parse_context_bundle(
            _source(root, "05-Context", context_sha, "context.json", read=read)
        )
        body = ["## Query", "", _code_fence(context.query, "text"),
                "", "## Exact Sources"]
        body.extend(
            f"- {_inline_code(row.path)} — {_inline_code(row.content_sha256)}"
            for row in context.sources
        )
        ctx_expected = _projection_markdown(
            case_id=case, stage="context", source_kind="context_bundle",
            source_sha256=context_sha, created_at=context.created_at,
            title="Generation Context", body="\n".join(body),
        )
    elif semantic:
        context = parse_objective_context(
            _source(root, "05-Context", context_sha,
                    "objective-context.json", read=read)
        )
        body = [
            f"Objective: {_inline_code(context.objective_policy)}",
            f"Selection policy: {_inline_code(context.selection_policy)}",
            f"Selection: {_inline_code(context.selection_sha256)}",
            f"Semantic index: {_inline_code(context.semantic_index_sha256)}",
            "", "## Exact Selected Chunks",
        ]
        body.extend(
            f"- {_inline_code(row.path)} — {row.source_kind} — "
            f"chunk {_inline_code(row.chunk_id)} — "
            f"content {_inline_code(row.content_sha256)}"
            for row in context.sources
        )
        ctx_expected = _projection_markdown(
            case_id=case, stage="context",
            source_kind="semantic_objective_context",
            source_sha256=context_sha, created_at=context.created_at,
            title="Semantic Generation Context", body="\n".join(body),
            extra_frontmatter={
                "objective_policy": context.objective_policy,
                "selection_policy": context.selection_policy,
                "selection_sha256": context.selection_sha256,
                "semantic_index_sha256": context.semantic_index_sha256,
                "objective_context_sha256": context_sha,
            },
        )
    else:
        raise ProducerLinkageError("unsupported producer Context kind")
    if ctx_req.content != ctx_expected or ctx_req.created_at != context.created_at:
        raise ProducerLinkageError("Context content differs from selected source")

    selection_sha = inp.source_sha256
    if semantic:
        if selection_sha != context.selection_sha256:
            raise ProducerLinkageError("Input is bound to another semantic Selection")
        selection = parse_semantic_selection(
            _source(root, "02-Orchestration/semantic-selections",
                    selection_sha, "semantic-selection.json", read=read)
        )
        if (
            selection.novelty.decision != "selected"
            or selection.selection_policy != context.selection_policy
            or selection.semantic_index_sha256 != context.semantic_index_sha256
            or len(selection.selected) != len(context.sources)
        ):
            raise ProducerLinkageError("semantic Selection does not match Context")
        for row, source in zip(selection.selected, context.sources):
            if (
                row.source_path != source.path
                or row.source_kind != source.source_kind
                or row.chunk_id != source.chunk_id
                or row.source_sha256 != source.source_sha256
                or row.content_sha256 != source.content_sha256
                or row.rank != source.rank or row.role != source.role
            ):
                raise ProducerLinkageError("semantic selected chunks mismatch")
        selected = [(row.source_path, row.source_kind) for row in selection.selected]
        policy = selection.selection_policy
        objective = context.objective_policy
        # Epoch and cycle are display-only for semantic selections; the
        # cleared planner pending-intent is not a durable authority for them.
        # Reconstruct the complete original Input around those bounded values.
        m = re.search(
            r"(?m)^Coverage epoch: ([0-9]{1,10})\nScheduler cycle: ([0-9]{1,10})$",
            inp.content,
        )
        if m is None:
            raise ProducerLinkageError("semantic selection presentation missing")
        epoch, cycle = int(m[1]), int(m[2])
    else:
        data = _source(root, "02-Orchestration/input-selections",
                       selection_sha, "selection.json", read=read)
        v = _decode_json_object(data, label="original Input selection")
        if (
            set(v) != {"record_version", "selection_policy", "objective_policy",
                       "catalog_sha256", "epoch", "cycle", "seed", "selected"}
            or type(v["record_version"]) is not int or v["record_version"] != 1
            or not isinstance(v["selection_policy"], str)
            or not isinstance(v["objective_policy"], str)
            or not isinstance(v["selected"], list)
            or not 1 <= len(v["selected"]) <= 64
            or len(v["selected"]) != len(context.sources)
        ):
            raise ProducerLinkageError("Input selection schema does not match Context")
        selected = []
        for row, source in zip(v["selected"], context.sources):
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("path"), str)
                or not isinstance(row.get("source_kind"), str)
                or row["path"] != source.path
                or row.get("content_sha256") != source.content_sha256
                or row.get("byte_size") != len(source.content.encode("utf-8"))
            ):
                raise ProducerLinkageError("Input selected source is not in Context")
            selected.append((row["path"], row["source_kind"]))
        policy, objective = v["selection_policy"], v["objective_policy"]
        epoch, cycle = v["epoch"], v["cycle"]

    expected_inp = _rebuild_input(
        case=case, selection_sha=selection_sha, policy=policy,
        objective=objective, epoch=epoch, cycle=cycle,
        selected=selected, created=context.created_at,
    )
    if inp.content != expected_inp or inp.created_at != context.created_at:
        raise ProducerLinkageError("Input differs from selected producer output")

    gen = parse_generation_record(_source(
        root, "00-Untrusted", generation_sha, "generation.json",
        read=read, cap=64 * 1024,
    ))
    if (
        gen.context_sha256 != context_sha
        or gen.proposal_sha256 != proposal_sha
        or (gen.context_kind == "semantic-objective") != semantic
    ):
        raise ProducerLinkageError("Generation does not bind selected Context/proposal")
    target, candidate = proposal_fields(root, proposal_sha, read=read)
    extras: dict[str, str | None] = {
        "proposal_sha256": proposal_sha,
        "target_path": target,
    }
    title = "Generated Knowledge Candidate"
    if semantic:
        meta = gen.semantic_objective
        if (
            meta is None
            or meta.selection_sha256 != selection_sha
            or meta.semantic_index_sha256 != context.semantic_index_sha256
            or meta.objective_policy != context.objective_policy
        ):
            raise ProducerLinkageError("semantic Generation metadata mismatch")
        extras.update({
            "objective_policy": meta.objective_policy,
            "selection_sha256": meta.selection_sha256,
            "semantic_index_sha256": meta.semantic_index_sha256,
            "objective_generation_sha256": meta.objective_generation_sha256,
            "objective_candidate_sha256": meta.objective_candidate_sha256,
        })
        title = "Generated Deep Knowledge Candidate"
    expected_gen = _projection_markdown(
        case_id=case, stage="generation", source_kind="generation_record",
        source_sha256=generation_sha, created_at=gen.generated_at,
        title=title,
        body="\n".join([
            f"Target: {_inline_code(target)}",
            "", "## Candidate", "", _code_fence(candidate),
        ]),
        extra_frontmatter=extras,
    )
    if gen_req.content != expected_gen or gen_req.created_at != gen.generated_at:
        raise ProducerLinkageError("Generation differs from selected producer output")
