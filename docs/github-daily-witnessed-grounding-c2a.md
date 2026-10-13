# C2a: source-bound singleton Grounding (private, nonpublishing)

This child of #351 adds no model invocation, Writer, Timer, CAS publication,
public rendering or host configuration. It is the v3 successor for singleton
claim checking, not a transparent change to the old Evidence-only v2 APIs.
Existing #331/#333 Grounding/rollup implementations are neither copied nor
modified. Rollup and complete-day materiality/omission review remain later work.

## Two entry points and their source ownership

`build_witnessed_grounding_contexts(partial_context, partial_response_bytes,
capture_sha256=...)` first re-normalizes the C1b reply. C1b independently
verifies the complete original CAS partition and consumes only its owned
scalar/ID snapshot (#351). C2a separately reconstructs C1a contexts from the
same Capture digest and byte budget, selects the exact normalized Context SHA,
and matches the entire source chain and Issue/PR identity. It resolves cited
IDs against this fresh trusted Context, never the caller's event container.
The only selection read from the caller before admission is the bounded integer
Context byte budget; caller key/events are not used after C1b normalization.

Each candidate gets one `WitnessedGroundContext` containing immutable canonical
UTF-8 JSON bytes (sorted keys, compact separators, trailing newline). It includes
Capture, Receipt, Evidence, C0 Closure, partial Context, raw partial reply and
candidate SHA, target JST date, recorded capture time/mode, exact Issue/PR key,
private candidate summary and the cited original Events with their route proofs.
The bytes are at most 256 KiB. No mutable source dict or caller-owned tuple is
returned by this API. Its self-hash is an identifier, NOT producer authority.

`normalize_witnessed_grounding_verdict(ground_context, reply,
partial_context=..., partial_response_bytes=..., capture_sha256=...)` rebuilds
those singleton contexts and requires byte-for-byte equality with exactly one.
It consumes only the matching freshly reconstructed serialization thereafter.
An internally consistent forged Context, old v2 serialization, another partial
reply, wrong source hash, replaced entity, or altered cited Event is rejected.
A missing source remains a typed `DailyWitnessedGroundingError`, never an empty
successful assessment. An empty candidate list yields no singleton contexts;
it does not certify an inactive day, and C1b uncited IDs remain subject to review.

The trusted scheduler must independently admit the Capture digest. Readable
historical captures retain their original day and explicit reconfirmation mode;
they are not automatically eligible for today's Daily. Root or compromised
same-UID Python/Collector execution is outside the existing local CAS trust
model. These functions are not a sandbox for arbitrary Python code execution.

## Narrow model result and no authority upgrade

The model may return only `verdict` (`supported` or `unsupported`) and a nonempty,
one-line printable `reason`, at most 2,048 characters / 8 KiB. The entire reply
is limited to 8 KiB. Duplicate JSON keys, additional fields, wrong types,
control/invisible characters, surrogate encoding failures and over-limit input
are rejected. No model-selected entity, candidate ID, URL, merge/deploy status
or permission is accepted as a structured field.

Normalized `WitnessedGroundAssessment` binds all source hashes and original
Evidence IDs plus Grounding Context SHA, raw reply SHA, verdict and reason SHA.
It does NOT retain the reason prose, original Events or candidate summary.
A reason mentioning a fabricated URL or merge therefore cannot become trusted
editorial data through this result. Each report returns an independent JSON
object decoded from owned bytes.

Every Context/result keeps `model_verdict_is_advisory=true`,
`requires_human_quality_review=true`, `publication_authorized=false`.
A supported model verdict is NOT independent proof of actual GitHub progress,
publication consent, an approved deployment, or semantic coverage of uncited
source events. Result objects must be independently revalidated by future
rollup; their Python class, hash or Boolean is not a transferable capability.

## Per-kind route proof is unchanged

Discussion comments use their fetched comment and `/issues/N` classification.
PR review comments use their fetched `/pulls/comments/ID` linkage; PR commits
use exact SHA membership from `/pulls/N/commits`. For the latter two, a linked
`/pulls/N` reference is NOT another fetched PR resource. `verified_api_path`
and `linked_entity_api_path` remain distinct. Structural lifecycle Events with
no per-event route witness retain `verified_route: null`; C2a does not elevate
those to fresh API attestations.

## Verification and remaining gates

Offline fixtures use the existing synthetic GitHub/real local POSIX-ACL CAS
path. They cover full digest-chain identity, singleton selection, all four
route kinds and original-only Events, correct-hash forged Contexts, changed
reply envelope, CAS loss, caller tuple indexing and mutation after verification,
rebuild failure, malformed/oversized model output and nonpublication flags.
A separate operator/regression run does not count as an independent reviewer.

C2a still requires exact-head CI and independent Correctness/Security review.
Parent #351 sign-off is separate. Rollup/coverage, actual production UID/GID/ACL
and scheduler checkpoint acceptance, #337 HTTP409, #290 omissions, #283 Japanese
quality, Summary/Projection/CAS migration and explicit Writer/Timer rollout
remain outstanding. No main merge or deployment is authorized by this Draft.
