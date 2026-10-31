# #486 W4B: explicitly attached optional read sources

Source-only integration for parent #486 / epic #298 and API #480, following
merged W3 #493 and W4A #495. This is not a production binding or rollout.

## Explicit attachment and scope

`make_server(workflow_control_read_ports=...)` accepts only a built-in `dict`
with optional `b1`, `b3`, `b5` string keys and these exact concrete types:

| Slot | Accepted type | Current source truth |
| --- | --- | --- |
| B1 | `webui_b1_adapter.B1ReadAdapter` | Noninitializing read of an explicitly attached bounded ledger; no provider dependency |
| B3 | `webui_b3_adapter.B3FakeAdapter` | Test-only synthetic in-memory ledger observation, always mock when retained |
| B5 | `webui_b5_ports.ReadPort` | No journal, or an explicitly injected `TestOnlyPrivateJournal` mock snapshot; not a production reader |

Subclasses, opaque/duck-typed objects, dict/key subclasses, wrong-slot types,
`B1FakeAdapter`, and `TestOnlyMockPort` are rejected before any supplied method
lookup. Validation copies the map without observing sources and occurs before
constructing the listening server. An empty map is an explicit attachment map.
B2/B4 ownership cannot be supplied or overridden through it.

The existing `workflow_control_snapshots` pre-materialized DTO input remains
backward-compatible. Supplying both non-`None` inputs, including empty maps,
raises `conflicting_workflow_control_sources` before binding. Both default to
`None`; no CLI, auto-discovery, credential loading, or configured production
attachment is added.

## Per-request observation and provenance

Only valid `GET /api/v1/workflow-control` and its five exact per-B detail GETs
materialize attached ports. Each such request calls only `observe()`, once per
attached source in B1/B3/B5 order, then passes the materialized map to existing
`read_control_catalog(optional_snapshots=...)`. Detail GETs use the same full
catalog composition, not a separate source/detail/lookup API. Repeated GETs read
again; no cross-request snapshot cache is introduced.

Exceptions and non-built-in-dict results become an invalid sentinel for just
that slot; the unchanged composer produces neutral `unavailable` with fixed
reasons, unknown execution and no private error text. Malformed DTOs are subject
to the existing exact v1 validation. Omitted ports stay `pending`; attached
adapters can report their own absent/unavailable source, not an invented empty
healthy store. B5 remains `not_applicable` / `manual_only`, even on failures.

The outer observation clock is sampled by the existing catalog reader **after**
optional observations and existing B2/B4 status reads. Valid source `observedAt`
values are preserved, not relabelled as the request time. B1 uses its injected
clock; B3/B5 use their explicitly supplied snapshot times. A refresh need not
change a fixed source timestamp. Future/invalid timestamps fail closed; there is
no new maximum-age policy, freshness guarantee, or authenticated provenance.

Static assets/pages, other GET routes, POSTs, and request-receipt lookups never
invoke these ports. HEAD retains the existing response/header handling but does
not observe optional ports; with port injection it uses neutral optional slots
for header construction. Legacy B2/B4 status reads on versioned HEAD are unchanged.

## No effect authority; known conservative limits

There are no new action routes or available actions, schedule decisions, upload
transport, provider/Writer/Vault calls, journal initialization/repair, credential
access, or effect verification. B3 remains a fake adapter even when constructed
with `read_only=True` or synthetic manual simulation enabled. B5's read port
cannot become a real private-journal reader by attachment. Real B3 and B5
production readers do not exist yet.

The conservative compositor is deliberately unchanged: it rejects connected
observations and actionable DTOs, rejects read-only verified-effect history,
and suppresses mock verified history. In particular, a valid B1 read adapter may
return verified Review-artifact history from its attached ledger; the current
composer makes that entire slot unavailable rather than promoting untrusted
verification. This known display limitation is not bypassed here. Mock verified
history cannot become connected health, real Vault success, or effect authority.
Neutral unavailable/absent fallbacks can drop the mock label because they retain
no source claims; a retained B3/B5 mock snapshot is visibly labelled mock.

## Coordinator verification handoff

Added synthetic coverage in `tests/test_webui_optional_read.py` and narrow HTTP
coverage in `tests/test_webui_control_get.py`: exact injection, pre-bind rejection,
lazy/fixed-order observation, source refresh/time ordering, per-source failures,
redaction, composer limits, mock labels, B5 manual-only, no side-effect calls,
static/HEAD/Host/Origin/POST isolation and legacy read compatibility. Existing
pre-materialized snapshot tests are retained.

Suggested coordinator checks (not executed in this source-only session):

```text
python -m pytest tests/test_webui_optional_read.py tests/test_webui_control_get.py
python -m pytest tests/test_webui_control_read.py tests/test_webui_legacy_read.py tests/test_webui_b1_adapter.py tests/test_webui_b3_adapter.py tests/test_webui_b5_ports.py tests/test_webui_workflow_contract.py
```

Also run the repository's existing Snapshot/Daily/Formatter/dashboard regression
suite under the coordinator's approved source-only test workflow. HTTP tests use
only coordinator-owned loopback fixtures when executed. No tests or commands
were run by the implementation session. No Fresh-LXC E2E, live credentials,
network provider, real Vault effect, or deployment acceptance is claimed.
