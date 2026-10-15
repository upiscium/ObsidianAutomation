"""Combine private, independently generated Issue/PR C5 drafts into ONE Daily view.

There is no additional LLM stage, reduction across entities, filesystem write,
network request or Writer/Timer privilege here. C4 rebuilds its own source- and
sentence-Grounding-validated private preview from original source CAS.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from . import github_daily_narrative_runtime as rt
from . import github_daily_witnessed_narrative as c4
from .artifact_lifecycle import ArtifactLifecycleError


VERSION = 1
STAGE = "github_daily_private_individual_digest_v1"
MAX_REPORT_BYTES = 128 * 1024


class IndividualDigestError(ValueError):
    """Unbound, duplicated or mixed-source entity progress cannot be displayed."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PrivateIndividualDigest:
    canonical_bytes: bytes
    preview: c4.NarrativePreview

    @property
    def sha256(self) -> str:
        return _sha(self.canonical_bytes)

    @property
    def markdown(self) -> str:
        """Exactly the original C4 unapproved, source-derived Issue/PR Markdown."""
        return self.preview.markdown

    def report(self) -> dict:
        return json.loads(self.canonical_bytes)


def compose_private_individual_digest(
        capture_sha256: str,
        partial_replies: tuple[c4.roll.PartialReply, ...],
        grounding_replies: tuple[c4.roll.GroundReply, ...],
        materiality_decisions: tuple[c4.c3.MaterialityDecision, ...],
        individual_runs: tuple[rt.NarrativeRun, ...],
        *, max_context_bytes: int = c4.roll.c1a.MAX_CONTEXT_BYTES,
        ) -> PrivateIndividualDigest:
    """Reuse exact C4 drafts from independent single-entity C5 runs.

    Every relevant selected entity is displayed. A failed/unrequested model
    result remains visible as a source-bound pending item, never inactivity.
    C5 runs are not proof of model identity; native model/semantic/human flags
    remain advisory and publication stays explicitly forbidden.
    """
    if (type(individual_runs) is not tuple
            or len(individual_runs) > c4.MAX_ENTITIES
            or any(type(r) is not rt.NarrativeRun for r in individual_runs)):
        raise IndividualDigestError("invalid bounded single-entity run collection")
    if type(capture_sha256) is not str:
        raise IndividualDigestError("invalid capture identity")
    args = dict(capture_sha256=capture_sha256,
                partial_replies=partial_replies, grounding_replies=grounding_replies,
                materiality_decisions=materiality_decisions,
                max_context_bytes=max_context_bytes)
    try:
        # The original C4 path revalidates source/CAS, attribution and selection;
        # external report counts/identity do not establish authenticity.
        source = c4.build_witnessed_narrative_contexts(**args)
        source_rows = source.report()["contexts"]
        targets = {
            (row["context"]["entity"]["repository"],
             row["context"]["entity"]["entity_type"],
             row["context"]["entity"]["number"]): row["context_sha256"]
            for row in source_rows
        }
        if len(targets) != len(source_rows):
            raise IndividualDigestError("duplicate verified selected source identity")
        seen = set()
        editorials = []
        verdicts = []
        for run in individual_runs:
            report = run.report()
            if type(report) is not dict or rt._json(report) != run.canonical_bytes:
                raise IndividualDigestError("noncanonical private run report")
            target = report.get("target_entity")
            if type(target) is not dict or set(target) != {
                    "repository", "entity_type", "number"}:
                raise IndividualDigestError("missing exact single-entity target")
            key = (target["repository"], target["entity_type"], target["number"])
            if (any(type(v) is not t for v, t in zip(key, (str, str, int)))
                    or key in seen or key not in targets):
                raise IndividualDigestError("duplicate or unselected target")
            seen.add(key)
            if (report.get("capture_sha256") != capture_sha256
                    or report.get("context_set_sha256") != source.sha256
                    or report.get("run_scope") != "single_entity"
                    or type(report.get("entity_count")) is not int
                    or report["entity_count"] != 1
                    or type(report.get("selected_entity_count")) is not int
                    or report["selected_entity_count"] != len(source_rows)
                    or report.get("preview_sha256") != run.preview.sha256
                    or report.get("source_review_sha256") != run.source_review.sha256
                    or report.get("publication_authorized") is not False
                    or report.get("semantic_coverage_verified") is not False
                    or report.get("model_execution_attested") is not False):
                raise IndividualDigestError("run source or authority identity changed")
            review = run.source_review.report()
            if (review.get("preview_sha256") != run.preview.sha256
                    or review.get("source_context_set_sha256") != source.sha256
                    or review.get("capture_sha256") != capture_sha256
                    or review.get("publication_authorized") is not False):
                raise IndividualDigestError("source-review companion is not bound")
            if type(run.narrative_replies) is not tuple or len(run.narrative_replies) > 1:
                raise IndividualDigestError("single-entity run has wrong editorial set")
            for item in run.narrative_replies:
                if type(item) is not c4.NarrativeReply or item.context_sha256 != targets[key]:
                    raise IndividualDigestError("foreign editorial")
                editorials.append(item)
            if type(run.grounding_replies) is not tuple or len(run.grounding_replies) > c4.MAX_SENTENCES:
                raise IndividualDigestError("unbounded model grounding set")
            for item in run.grounding_replies:
                if type(item) is not c4.NarrativeGroundReply:
                    raise IndividualDigestError("untyped grounding reply")
                verdicts.append(item)
        preview = c4.build_witnessed_narrative_preview(
            **args, narrative_replies=tuple(editorials),
            narrative_grounding_replies=tuple(verdicts))
        report = preview.report()
        if (report.get("source_context_set_sha256") != source.sha256
                or report.get("capture_sha256") != capture_sha256
                or report.get("publication_authorized") is not False
                or len(report["entities"]) != len(source_rows)):
            raise IndividualDigestError("source-bound composed preview changed")
        selected = []
        for row in report["entities"]:
            entity = row["entity"]
            key = (entity["repository"], entity["entity_type"], entity["number"])
            if key not in targets:
                raise IndividualDigestError("foreign entity in composed preview")
            supported = sum(s["verdict"] == "supported" for s in row["sentences"])
            selected.append({
                "entity": entity,
                "requested_for_inference": key in seen,
                "display_state": "ai_draft_pending_human_review"
                                  if supported else "source_item_pending_summary",
                "supported_sentence_count": supported,
                "pending_source_event_count": len(row["pending_evidence_ids"]),
            })
        payload = {
            "record_version": VERSION, "stage": STAGE,
            "capture_sha256": capture_sha256,
            "source_context_set_sha256": source.sha256,
            "preview_sha256": preview.sha256,
            "date": report["date"],
            "collection_mode": report["collection_mode"],
            "selected_entity_count": len(selected),
            "requested_entity_count": len(seen),
            "unrequested_entity_count": len(selected) - len(seen),
            "pending_summary_entity_count": sum(
                row["display_state"] == "source_item_pending_summary" for row in selected),
            "entities": selected,
            "new_model_calls": 0,
            "model_execution_attested": False,
            "semantic_coverage_verified": False,
            "factuality_verified": False,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }
        raw = rt._json(payload)
        if len(raw) > MAX_REPORT_BYTES:
            raise IndividualDigestError("private daily report exceeds budget")
        return PrivateIndividualDigest(raw, preview)
    except IndividualDigestError:
        raise
    except (c4.DailyNarrativeError, rt.NarrativeRuntimeError,
            ArtifactLifecycleError, KeyError, TypeError, ValueError,
            AttributeError, IndexError, OverflowError, RecursionError):
        raise IndividualDigestError("private individual daily source unavailable") from None
