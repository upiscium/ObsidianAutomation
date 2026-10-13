# C1b candidate admission: owned source snapshot (#351)

This note amends the C1b implementation contract. Public APIs, serialized
candidate fields, and candidate hash inputs remain unchanged. All outputs
remain private and nonpublishing.

## Verification is not ownership

The previous normalizer verified a caller-provided Context against Collector
CAS and then indexed that same caller-provided event sequence. A tuple
subclass could preserve normal equality/iteration while overriding indexing;
a later mutation of the caller's event dictionary or entity key had the same
problem. A hash returned by a verifier does not make that object safe to reuse.

The revised admission retains only exact integer batch-selection parameters,
invokes the existing C1a verifier, and separately rebuilds the complete Context
partition from the trusted Capture digest. The selected rebuilt Context must
have the same SHA returned by verification. It then copies the original
Evidence IDs and identity/hash strings into a frozen private scalar snapshot.
Both the schema and normalizer use that snapshot exclusively. They do not
index the caller's events or read its entity key after verification.

The second reconstruction costs another complete source read. It deliberately
avoids changing the C1a public verifier or accepting a Context SHA as a
credential. Missing source, changed batch count, or a different rebuilt SHA is
a typed failure, including when the model returns zero candidates.

## Regression evidence

The dedicated snapshot tests use the existing synthetic GitHub/real local CAS
fixture. They cover tuple indexing overrides, event/key mutation during reply
decoding, sequence-length mutation after verification, a failed/changed second
reconstruction, wrong-typed selection fields, and an independently calculated
candidate digest containing the complete Capture/Receipt/Evidence/Closure/
Context chain. A separate offline object-handling harness is not a substitute
for those CAS tests or independent code review.

This closes an in-process library-boundary defect; it does not claim defense
against arbitrary Python code that can replace trusted modules, root or
Collector-UID compromise, or an untrusted scheduler checkpoint. There is no
new model request, network call, CAS write, Writer/Timer integration, main
merge, or production permission. C2 Grounding and operational acceptance remain
separate gates.
