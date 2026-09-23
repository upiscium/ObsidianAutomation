from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ConsistencyConflictProposal:
    """A model-proposed conflict before deterministic evidence binding.

    The model selects deterministic excerpt identifiers rather than reproducing
    quote bytes. Candidate path authority remains outside the model.
    """

    proposal_excerpt_id: str
    candidate_excerpt_id: str
    # Historical internal callers may still populate this field. v7 model output
    # never supplies it and deterministic binding/verifier logic ignores it.
    incompatibility: str | None = None


@dataclass(frozen=True)
class BoundConsistencyConflictProposal:
    """A conflict proposal after deterministic excerpt-ID resolution."""

    proposal_quote: str
    candidate_quote: str
    # Retained only for source compatibility; v7 verifier input omits it.
    incompatibility: str | None = None


@dataclass(frozen=True)
class ConsistencyVerification:
    """The verifier's verdict for one deterministically anchored conflict."""

    verdict: str
    explanation: str


@dataclass(frozen=True)
class ConsistencyConflict:
    """A verifier-confirmed proposal/candidate incompatibility.

    The model-facing form deliberately has no candidate path authority. A path
    is attached only after deterministic binding to the candidate supplied in
    the evaluation prompt.
    """

    proposal_claim: str
    candidate_claim: str
    incompatibility: str
    candidate_path: str | None = None

    def bind_candidate_path(self, candidate_path: str) -> ConsistencyConflict:
        return replace(self, candidate_path=candidate_path)


# Keep descriptive aliases available to callers that refer to the evidence
# generically rather than by its consistency dimension.
EvaluatorConflict = ConsistencyConflict
ConsistencyConflictEvidence = ConsistencyConflict
