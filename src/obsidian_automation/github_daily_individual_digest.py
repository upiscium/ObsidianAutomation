"""Combine private, independently generated Issue/PR C5 drafts into ONE Daily view.

There is no additional LLM stage, reduction across entities, filesystem write,
network request or Writer/Timer privilege here. C4 rebuilds its own source- and
sentence-Grounding-validated private preview from original source CAS.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic as _monotonic
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
        original_rows = {}
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
            # The run's *original* attempts are an immutable comparison
            # witness. A structurally valid replacement supported Grounding
            # answer must never upgrade the original unsupported/pending row.
            if (type(run.attempts) is not tuple
                    or len(run.attempts) > 1 + c4.MAX_SENTENCES
                    or type(report.get("attempts")) is not list
                    or len(report["attempts"]) != len(run.attempts)):
                raise IndividualDigestError("incomplete original model attempt set")
            accepted_editorials, accepted_grounds = {}, {}
            for attempt, recorded in zip(run.attempts, report["attempts"]):
                if (type(attempt) is not rt.NarrativeAttempt
                        or type(attempt.context_sha256) is not str
                        or attempt.stage not in ("editorial", "grounding")
                        or attempt.state not in ("accepted", "deadline_pending",
                                                  "inference_or_validation_failed")
                        or type(recorded) is not dict):
                    raise IndividualDigestError("invalid recorded model attempt")
                raw = attempt.raw_response_bytes
                receipt = attempt.receipt_bytes
                raw_hash = _sha(raw) if type(raw) is bytes else None
                receipt_hash = _sha(receipt) if type(receipt) is bytes else None
                if (type(raw) not in (bytes, type(None))
                        or type(receipt) not in (bytes, type(None))
                        or recorded.get("stage") != attempt.stage
                        or recorded.get("context_sha256") != attempt.context_sha256
                        or recorded.get("state") != attempt.state
                        or recorded.get("raw_response_sha256") != raw_hash
                        or recorded.get("receipt_sha256") != receipt_hash
                        or recorded.get("admitted_response_sha256") !=
                        attempt.admitted_response_sha256):
                    raise IndividualDigestError("original model attempt bytes changed")
                if attempt.state != "accepted":
                    if attempt.admitted_response_sha256 is not None:
                        raise IndividualDigestError("failed reply appeared admitted")
                    continue
                if (raw_hash is None or receipt_hash is None
                        or type(attempt.admitted_response_sha256) is not str):
                    raise IndividualDigestError("missing accepted model bytes")
                receipt_meta = rt._decode_json_object(
                    receipt, label="individual model receipt")
                if (receipt_meta.get("context_sha256") != attempt.context_sha256
                        or receipt_meta.get("raw_response_sha256") != raw_hash
                        or receipt_meta.get("publication_authorized") is not False
                        or receipt_meta.get("model_execution_attested") is not False):
                    raise IndividualDigestError("foreign accepted model receipt")
                group = (accepted_editorials if attempt.stage == "editorial"
                         else accepted_grounds)
                if attempt.context_sha256 in group:
                    raise IndividualDigestError("duplicate admitted model reply")
                group[attempt.context_sha256] = attempt.admitted_response_sha256
            if type(run.narrative_replies) is not tuple or len(run.narrative_replies) > 1:
                raise IndividualDigestError("single-entity run has wrong editorial set")
            admitted_editorials = {}
            for item in run.narrative_replies:
                if type(item) is not c4.NarrativeReply or item.context_sha256 != targets[key]:
                    raise IndividualDigestError("foreign editorial")
                admitted_editorials[item.context_sha256] = _sha(item.response_bytes)
                editorials.append(item)
            if admitted_editorials != accepted_editorials:
                raise IndividualDigestError("editorial differs from accepted C5 attempt")
            if type(run.grounding_replies) is not tuple or len(run.grounding_replies) > c4.MAX_SENTENCES:
                raise IndividualDigestError("unbounded model grounding set")
            admitted_groundings = {}
            for item in run.grounding_replies:
                if type(item) is not c4.NarrativeGroundReply:
                    raise IndividualDigestError("untyped grounding reply")
                if item.ground_context_sha256 in admitted_groundings:
                    raise IndividualDigestError("duplicate emitted grounding")
                admitted_groundings[item.ground_context_sha256] = _sha(item.response_bytes)
                verdicts.append(item)
            if admitted_groundings != accepted_grounds:
                raise IndividualDigestError("grounding differs from accepted C5 attempt")
            original = run.preview.report()
            if (original.get("source_context_set_sha256") != source.sha256
                    or original.get("capture_sha256") != capture_sha256
                    or type(original.get("entities")) is not list):
                raise IndividualDigestError("individual preview changed before composition")
            original_matches = [row for row in original["entities"]
                if type(row) is dict
                and (row["entity"]["repository"], row["entity"]["entity_type"],
                     row["entity"]["number"]) == key]
            if len(original_matches) != 1 or original_matches[0].get(
                    "context_sha256") != targets[key]:
                raise IndividualDigestError("missing original per-entity preview")
            original_rows[key] = rt._json(original_matches[0])
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
            if key in original_rows and rt._json(row) != original_rows[key]:
                # The public C4 revalidation must reproduce *exactly* the
                # originally displayed row, including unsupported verdicts.
                raise IndividualDigestError("composed row changed original C5 preview")
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



MAX_COMPLETE_DAY_ENTITIES = c4.MAX_ENTITIES
MAX_COMPLETE_DAY_C5_CALLS = 4 * MAX_COMPLETE_DAY_ENTITIES
MAX_COMPLETE_DAY_SECONDS = 3600


@dataclass(frozen=True)
class CompletePrivateDaily:
    """Full selected-day private result, including source-pending skipped items."""
    canonical_bytes: bytes
    digest: PrivateIndividualDigest
    individual_runs: tuple[rt.NarrativeRun, ...]

    @property
    def markdown(self):
        return self.digest.markdown

    def report(self):
        return json.loads(self.canonical_bytes)


def run_complete_private_daily(
        capture_sha256: str,
        partial_replies: tuple[c4.roll.PartialReply, ...],
        grounding_replies: tuple[c4.roll.GroundReply, ...],
        materiality_decisions: tuple[c4.c3.MaterialityDecision, ...],
        *,
        infer,
        max_entities: int = c4.MAX_ENTITIES,
        max_total_calls: int = MAX_COMPLETE_DAY_C5_CALLS,
        max_total_seconds: float = 3600,
        per_entity_seconds: float = 120,
        require_day_policy: bool = True,
        max_context_bytes: int = c4.roll.c1a.MAX_CONTEXT_BYTES,
        ) -> CompletePrivateDaily:
    """Run each C3-selected Issue/PR with the existing C5 runner, then compose.

    This is an explicit operator-controlled, PRIVATE preview, not a scheduler,
    persistence boundary or automatic Writer activation. No additional model
    judging or rewriting occurs beyond C5's existing Grounding.

    A complete worst-case call budget must be reserved before model inference.
    Time exhaustion between entities leaves remaining source-selected items
    visibly pending. Source, model and trust policies are separately controlled.
    """
    if (type(max_entities) is not int
            or not 1 <= max_entities <= MAX_COMPLETE_DAY_ENTITIES
            or type(max_total_calls) is not int
            or not 1 <= max_total_calls <= MAX_COMPLETE_DAY_C5_CALLS
            or type(require_day_policy) is not bool
            or not callable(infer)):
        raise IndividualDigestError("invalid complete-day execution bounds")
    try:
        remaining_total = rt._number(max_total_seconds, MAX_COMPLETE_DAY_SECONDS)
        per_entity = rt._number(per_entity_seconds, rt.MAX_RUN_SECONDS)
        args = dict(capture_sha256=capture_sha256,
                    partial_replies=partial_replies,
                    grounding_replies=grounding_replies,
                    materiality_decisions=materiality_decisions,
                    max_context_bytes=max_context_bytes)
        plan = c4.build_witnessed_narrative_contexts(**args)
        contexts = plan.report()["contexts"]
        if len(contexts) > max_entities:
            raise IndividualDigestError("selected day exceeds complete-request entity cap")
        if len(contexts) * (1 + c4.MAX_SENTENCES) > max_total_calls:
            raise IndividualDigestError("complete-request model call budget insufficient")
        identities = []
        for item in contexts:
            ctx = item["context"]
            e = ctx["entity"]
            identities.append((e["repository"], e["entity_type"], e["number"]))
            if require_day_policy:
                # A #358 produced, authenticated CAS source is required. This
                # presence check is not a standalone GitHub signature or
                # reconstruction of original PR-body edit history.
                sources = ctx["sources"]
                if type(sources) is not list:
                    raise IndividualDigestError("invalid period-bound source set")
                for row in sources:
                    event = row["original_event"]
                    observation = event.get("temporal_observation")
                    if (type(observation) is not dict
                            or observation.get("policy") != "github-day-snapshot-v1"
                            or observation.get("historical_instant_verified") is not False):
                        raise IndividualDigestError("missing date-scoped source policy")
        if len(set(identities)) != len(identities):
            raise IndividualDigestError("duplicate selected Issue/PR")

        started = _monotonic()
        runs = []
        stop_reason = None
        for target in identities:
            remaining = remaining_total - (_monotonic() - started)
            if remaining <= 0.01:
                stop_reason = "total_elapsed_budget_exhausted"
                break
            run = rt.run_witnessed_narrative_inference(
                **args, infer=infer, target_entity=target,
                max_entities=1, max_calls=1 + c4.MAX_SENTENCES,
                max_elapsed_seconds=min(per_entity, remaining))
            runs.append(run)
        digest = compose_private_individual_digest(**args,
                                                  individual_runs=tuple(runs))
        aggregate = digest.report()
        if (aggregate["selected_entity_count"] != len(identities)
                or aggregate["requested_entity_count"] != len(runs)
                or aggregate["publication_authorized"] is not False):
            raise IndividualDigestError("complete-day source accounting changed")
        responses_complete = all(
            r.report()["inference_responses_complete"] for r in runs)
        # Empty selected input is not an attestation that GitHub had no
        # progress: that claim needs independently complete source selection.
        completed = (bool(identities) and len(runs) == len(identities)
                     and responses_complete
                     and aggregate["pending_summary_entity_count"] == 0)
        result = {
            "record_version": VERSION,
            "stage": "github_daily_complete_private_execution_v1",
            "source_capture_sha256": capture_sha256,
            "source_context_set_sha256": plan.sha256,
            "digest_sha256": digest.sha256,
            "date": aggregate["date"],
            "selected_entity_count": len(identities),
            "requested_entity_count": len(runs),
            "unrequested_entity_count": len(identities) - len(runs),
            "pending_summary_entity_count": aggregate["pending_summary_entity_count"],
            "attempt_record_count": sum(len(r.attempts) for r in runs),
            "all_selected_entities_requested": len(runs) == len(identities),
            "all_inference_responses_complete": responses_complete,
            "complete_and_all_selected_have_supported_text": completed,
            "stopped_before_new_entity_reason": stop_reason,
            "source_period_policy": ("day_snapshot_required"
                                    if require_day_policy else
                                    "legacy_unverified_explicit_offline"),
            "historical_instant_verified": False,
            "model_execution_attested": False,
            "semantic_coverage_verified": False,
            "factuality_verified": False,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }
        raw = rt._json(result)
        if len(raw) > MAX_REPORT_BYTES:
            raise IndividualDigestError("complete-day report exceeds budget")
        return CompletePrivateDaily(raw, digest, tuple(runs))
    except IndividualDigestError:
        raise
    except (c4.DailyNarrativeError, rt.NarrativeRuntimeError,
            ArtifactLifecycleError, KeyError, TypeError, ValueError,
            AttributeError, IndexError, OverflowError, RecursionError):
        raise IndividualDigestError("trusted complete-day input unavailable") from None
