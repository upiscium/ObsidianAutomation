from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ConsistencyConflictProposal:
    """A model-proposed conflict before independent verification.

    Quotes are exact excerpts supplied by the model and are never trusted as
    provenance until deterministic code checks them against the immutable
    proposal and candidate contents.  This model-facing type deliberately has
    no candidate path.
    """

    proposal_quote: str
    candidate_quote: str
    incompatibility: str


@dataclass(frozen=True)
class ConsistencyVerification:
    """The verifier's verdict for one anchored conflict proposal."""

    verdict: str
    explanation: str


@dataclass(frozen=True)
class ConsistencyConflict:
    """A verifier-confirmed proposal/candidate incompatibility.

    The model-facing form deliberately has no candidate path authority.  A
    path is attached only after deterministic binding to the candidate that
    was supplied in the evaluation prompt.
    """

    proposal_claim: str
    candidate_claim: str
    incompatibility: str
    candidate_path: str | None = None

    def bind_candidate_path(self, candidate_path: str) -> ConsistencyConflict:
        return replace(self, candidate_path=candidate_path)


# Keep a descriptive alias available to callers that refer to the evidence
# generically rather than by its consistency dimension.
EvaluatorConflict = ConsistencyConflict
ConsistencyConflictEvidence = ConsistencyConflict
