# #472 D1/B1/B2 — Owner→Broker accepted-socket read-only observation responder

**Status:** LOCAL-ONLY / SOURCE-ONLY / UNWIRED. Based on PR #562 merge main 09def9b580fcb394b28a90835832e4fb5f1055a3. No Commit/Push/PR/Ready/Merge, service/UID deployment, Provider access, Vault Writer, live CAS, credential installation, rotation or activation.

## Exact narrow scope

New module `vault_format_broker_readonly_session.py` handles exactly **one already-accepted Linux AF_UNIX SOCK_STREAM connection** and then closes that descriptor. It **does not bind a socket, accept new connections, provision a directory, launch an OS service or run a loop.** No existing Owner listener/Worker/Formatter/Writer/scheduler runtime module imports it.

The handler pins its nonroot Broker-process PID/effective UID/GID and a separate expected nonroot Owner UID plus Owner primary GID. It validates the accepted connected peer using kernel `SO_PEERCRED` **before reading any request bytes or accessing Broker Journal state**. A same-UID, wrong-UID, wrong-GID or unsupported peer receives no Broker data. `SO_PEERCRED` proves connected peer identity, **not origin of a subsequently forwarded FD, actual trusted executable identity or an approved Owner Run/Claim**. A real service must independently provision Broker-owned parent directory, socket mode/GID, executable, distinct OS users, credential ACL/egress and lifecycle. Neither process nor socket is created here.

After peer authentication, the server accepts only one bounded ASCII JSON v1 request line terminated by the peer's write-half-close/actual EOF before responding. The one-connection deadline and 1024-byte request/response bounds match the existing read-only A0 Owner client. Extra bytes/newlines, duplicate keys, invalid booleans/numbers, unsupported versions, missing IDs and unexpected fields fail closed. Invalid request, identity mismatch or unavailable durable Broker state results only in static protocol failure codes (`broker_denied` or `broker_unavailable`) without echoing request content or filesystem paths; no auto-retry. If the peer closes or socket write fails, the Broker never claims a response was successfully delivered.

The **only two allowed methods** are:

- `credential.observe`: exact account-binding SHA-256. Returns the existing A0 schema with persisted **non-secret credential-generation digest, epoch and ledger revision metadata**, **not** real credentials/currentness proof. Unknown account and invalid scope fail closed; no issuance/rotation/secret.
- `operation.finality`: exact 32-lowerhex Run ID and operation ID plus 64-lowerhex account-binding and immutable sealed attempt SHA-256. Added `BrokerPrivateJournal.observe_finality_by_key` reads the **actual persisted exact full-attempt digest** from an opened-inode-pinned transaction. Unknown/mismatched operation identity is denied, not converted into no-effect. `pre_dispatch`, `dispatch_possible`, `unconfirmed` return `pending` with **no receipt or journal sequence**; only the existing locally terminal `verified_no_effect` returns a positive persisted sequence/digest and exact echoed identity **after independent recomputation of the canonical Broker-local never-dispatched receipt transcript and verification of its monotonic gate watermark and the absence of the exact attempt's resource lock in the same SQL read transaction**. A digest-shaped arbitrary receipt, a forged terminal status with a stale gate sequence, or a matching transcript while the resource lock remains held is rejected rather than reported as terminal. Any unexpected `verified_remote_effect` / malformed terminal record fails closed. The transcript key is the Broker-private DB identity, **not** a Provider-origin cryptographic attestation; a hostile process with the same Broker UID or authority to rewrite its state is still outside this slice's security proof. This responder does **not** mint real Provider finality or call Broker mutation methods.

Both response types are plain **observations**, not bearer capabilities. They are never fed directly into D1/B0 Owner journal settlement, Writer authority or resource-conflict release. A valid `SO_PEERCRED` report plus a Broker-local receipt is NOT enough to establish a Provider never wrote, especially if another process could use credentials or send a delayed request.

## Test boundary

`tests/test_472_broker_readonly_session_d1b1b2.py` uses disposable 0700 Broker-private SQLite fixtures and `AF_UNIX SOCK_STREAM` pairs, and verifies:

- A0-compatible response schema and strictly limited fields, persistent pending vs. never-dispatched-only terminal reads, stable terminal receipts across observation repeats, replay/mismatch/unknown denial, reboot ambiguity remaining pending.
- Actual kernel `SO_PEERCRED` returns the same local UID and the handler rejects that peer **before reading**; wrong synthetic GID similarly rejected. Positive cross-UID sessions **mock the connected SO_PEERCRED result**; there is no real distinct-UID end-to-end acceptance or filesystem socket binding proof.
- Forbidden dispatch/PUT/rotate/reconcile/settle operations, duplicate-key/overflow/extra-byte/extra-line/non-ASCII/unsupported JSON, missing write-half-close timeout and no raw-account echo; unknown and corrupted state produce fail-closed fixed reasons.
- No modification or import into existing runtime paths, no network Provider, no live Vault.

Tests are a source-only simulation. Real kernel cross-UID acceptance, actual Broker credential protection, authenticated Owner Plan/Claim, stable effect lock, controlled Broker Provider request, operation-specific durable Provider finality, account-wide recovery, power-loss and live Vault canaries **remain future separately approved gates**.

## Follow-on dependency

D1/B1/B3 or later must independently implement a preprovisioned trusted Broker Unix listener with enforced parent/socket ownership, no FD forwarding, bounded connection dispatch and revalidation under real distinct principals; separately establish a Broker-controlled Provider CAS/PUT and independent operation-specific durable receipt/finality. The Owner must hold its stable cross-process effect flock and independently check the exact signed Owner-private Claim/immutable plan before any effect. Neither this source merge nor a typed `verified_no_effect` may be interpreted as authorization for those behaviors.
