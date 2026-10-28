"""Contract tests for the intentionally source-only B5 ports.

All inputs are synthetic in-memory bytes and explicitly injected test fixtures;
these tests do not assert a real ZIP parser, durable journal, or production gate.
"""
import hashlib
import ast
import importlib
import json
from pathlib import Path
import stat
import zipfile
from dataclasses import replace
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

import pytest


# Keep TestOnly* types behind an alias: pytest must not collect them as tests.
ports = importlib.import_module("obsidian_automation.webui_b5_ports")
ID = "1" * 32
REV = "2" * 32
GEN = "3" * 32
POLICY = "4" * 32


def inspection(data=b"synthetic archive", **kw):
    kw.setdefault("selection_resolved", True)
    kw.setdefault("selection_accepted", True)
    return ports.TestOnlyInspection(hashlib.sha256(data).hexdigest(), **kw)


def setup(data=b"synthetic archive", **fixture_kw):
    evidence = inspection(data, **fixture_kw)
    journal = ports.TestOnlyPrivateJournal(REV, GEN, POLICY)
    port = ports.TestOnlyMockPort(
        journal=journal, inspector=ports.TestOnlyInspector((evidence,)),
        limits=ports.TestOnlyStreamLimits(max_bytes=128, chunk_bytes=7),
        observed_at=10.0, authority_checked=True,
    )
    return port, journal, evidence, data


def stage_request(request_id=ID, revision=REV):
    return ports.StageArchiveRequest(request_id, revision)


def stage(port, data=b"synthetic archive", request_id=ID, revision=REV):
    return port.stage_archive(stage_request(request_id, revision), BytesIO(data))


def import_request(staged, request_id="5" * 32, *, revision):
    return ports.InventoryImportRequest(
        request_id, revision,
        staged["stagedArchive"]["archiveRef"], staged["stagedArchive"]["selectionRef"],
    )


def test_exact_request_parsing_and_rejects_ambiguous_json():
    value = {"schemaVersion": ports.SCHEMA_VERSION, "requestId": ID, "expectedRevision": REV}
    assert ports.StageArchiveRequest.parse(value) == stage_request()
    assert ports.StageArchiveRequest.parse(json.dumps(value)).request_id == ID
    bad = [
        json.dumps(value)[:-1] + ',"requestId":"' + "6" * 32 + '"}',
        json.dumps({**value, "extra": True}),
        json.dumps({**value, "schemaVersion": "webui-workflow-control/v2"}),
        json.dumps({**value, "requestId": "not-an-id"}),
        '{"schemaVersion":"webui-workflow-control/v1","requestId":"' + ID +
        '","expectedRevision":"' + REV + '","x":NaN}',
    ]
    for raw in bad:
        with pytest.raises(ports.RequestError):
            ports.StageArchiveRequest.parse(raw)
    with pytest.raises(ports.RequestError, match="unsupported_action"):
        ports.InventoryImportRequest.parse({
            "schemaVersion": ports.SCHEMA_VERSION, "requestId": ID,
            "expectedRevision": REV, "action": "run", "input": {},
        })


def test_read_only_observation_is_manual_and_disabled_without_mutation():
    read = ports.ReadPort(observed_at=1)
    before = read.observe()
    assert before["configuration"]["reason"] == "pending_integration"
    assert before["schedule"] == {
        "state": "not_applicable", "intervalSeconds": None,
        "nextDueAt": None, "reason": "manual_only",
    }
    assert before["availableActions"] == []
    assert read.lookup(ID)["error"]["code"] == "admission_unknown"
    assert read.lookup_staging(ID)["error"]["code"] == "admission_unknown"
    assert read.observe() == before
    journal = ports.TestOnlyPrivateJournal(REV, GEN, POLICY)
    mock = ports.TestOnlyMockPort(
        journal=journal, inspector=ports.TestOnlyInspector(()),
        limits=ports.TestOnlyStreamLimits(max_bytes=2, chunk_bytes=1),
        observed_at=1, authority_checked=True,
    )
    assert mock.observe()["availableActions"] == []
    assert mock.journal is journal


def test_stage_is_receipt_only_replays_conflicts_and_keeps_original_snapshot():
    port, original, _, data = setup()
    accepted = stage(port, data)
    assert accepted["decision"] == "accepted"
    dto = accepted["stagedArchive"]
    assert set(dto) == {"schemaVersion", "requestId", "archiveRef", "selectionRef", "inspection"}
    assert "workflowId" not in accepted and "runId" not in dto and "effect" not in dto
    rotated = replace(port.journal, revision="6" * 32, generation="7" * 32, policy_ref="8" * 32)
    replay_port = ports.TestOnlyMockPort(
        journal=rotated, inspector=ports.TestOnlyInspector(()),
        limits=ports.TestOnlyStreamLimits(max_bytes=128, chunk_bytes=7),
        observed_at=20, authority_checked=True,
    )
    replay = stage(replay_port, data, revision="9" * 32)
    assert replay["decision"] == "replayed" and replay["stagedArchive"] == dto
    conflict = stage(replay_port, b"different", revision="9" * 32)
    assert conflict["decision"] == "rejected"
    assert conflict["error"]["code"] == "request_conflict"
    assert original.stages == () and len(port.journal.stages) == 1
    stale = stage(port, data, request_id="a" * 32, revision="b" * 32)
    assert stale["error"]["code"] == "stale_revision"


def test_import_receipt_idempotency_conflicts_and_lookup_only_unknown():
    port, _, _, data = setup()
    staged = stage(port, data)
    request = import_request(staged, revision=port.observe()["revision"])
    receipt = port.import_inventory(request)
    assert receipt["decision"] == "accepted"
    assert receipt["receipt"]["action"] == "import_inventory"
    assert port.import_inventory(request) == {**receipt, "decision": "replayed"}
    changed = replace(request, selection_ref="c" * 32)
    assert port.import_inventory(changed)["error"]["code"] == "request_conflict"
    unknown = replace(port.journal, verified=False)
    observer = ports.ReadPort(unknown, observed_at=11)
    assert observer.lookup(request.request_id)["decision"] == "indeterminate"
    assert observer.lookup("d" * 32)["error"]["code"] == "admission_unknown"
    assert observer.observe()["configuration"]["reason"] == "observation_unavailable"


def test_authority_guard_precedes_replay_and_out_of_order_selection_rejected():
    port, _, _, data = setup()
    first = stage(port, data)
    # A different staged identity is a synthetic immutable inspection fixture.
    other = b"second synthetic archive"
    inspector = ports.TestOnlyInspector((inspection(data), inspection(other)))
    port = ports.TestOnlyMockPort(
        journal=port.journal, inspector=inspector,
        limits=ports.TestOnlyStreamLimits(max_bytes=128, chunk_bytes=8),
        observed_at=10, authority_checked=True,
    )
    second = stage(port, other, request_id="e" * 32, revision=port.observe()["revision"])
    req = import_request(second, request_id="f" * 32, revision=port.observe()["revision"])
    assert port.import_inventory(req)["error"]["code"] == "input_binding_invalid"
    unauth = ports.TestOnlyMockPort(
        journal=port.journal, inspector=inspector,
        limits=ports.TestOnlyStreamLimits(max_bytes=128, chunk_bytes=8),
        observed_at=1, authority_checked=False,
    )
    assert unauth.import_inventory(req)["error"]["code"] == "authority_rejected"
    assert first["decision"] == "accepted"


@pytest.mark.parametrize("status", ["inventory_review_required", "normalization_rejected", "untrusted"])
def test_inventory_is_not_review_vault_or_generation_and_rejection_is_not_success(status):
    port, _, _, data = setup(inventory_status=status, quarantined_slots=1)
    staged = stage(port, data)
    receipt = port.import_inventory(import_request(staged, revision=port.observe()["revision"]))
    assert receipt["decision"] == "accepted"
    body = receipt["receipt"]
    assert body["outcome"] == ("failure" if status == "normalization_rejected" else "unknown" if status == "untrusted" else "success")
    detail = ports.ReadPort(port.journal, observed_at=12).inventory_detail(body["requestId"])
    assert detail["eligible_for_generation"] is False
    assert detail["generation_jobs_queued"] == 0
    assert detail["review_artifact_created"] is False and detail["vault_changed"] is False


def test_bounds_reject_oversize_and_non_bytesio_without_journal_change():
    port, _, _, data = setup()
    before = port.journal
    assert stage(port, data * 20)["decision"] == "rejected"
    assert port.journal is before
    assert port.stage_archive(stage_request("a" * 32), object())["decision"] == "rejected"
    assert port.journal is before


def test_concurrent_duplicate_stage_and_import_ids_bind_at_most_once():
    port, _, _, data = setup()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: stage(port, data), range(16)))
    assert sum(r["decision"] == "accepted" for r in results) == 1
    assert len(port.journal.stages) == 1
    req = import_request(results[0], revision=port.observe()["revision"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: port.import_inventory(req), range(16)))
    assert sum(r["decision"] == "accepted" for r in receipts) == 1
    assert len(port.journal.imports) == 1


def test_zip_adversarial_bytes_are_not_claimed_as_parsed_or_inspected():
    # The module intentionally consumes only digest fixtures; adversarial ZIP
    # structures have no injected digest evidence and therefore cannot stage.
    candidates = [
        b"../escape", b"/absolute", b"duplicate-member", b"symlink",
        b"encrypted", b"ambiguous", b"malformed-json", b"high-ratio",
    ]
    port, journal, _, _ = setup()
    for i, data in enumerate(candidates):
        result = stage(port, data, request_id=f"{i + 10:032x}")
        assert result["decision"] == "rejected"
        assert result["error"]["code"] == "input_binding_invalid"
    assert port.journal is journal


def mock_from(journal, fixtures=(), *, authority=True, max_bytes=4096):
    return ports.TestOnlyMockPort(
        journal=journal, inspector=ports.TestOnlyInspector(tuple(fixtures)),
        limits=ports.TestOnlyStreamLimits(max_bytes=max_bytes, chunk_bytes=17),
        observed_at=20, authority_checked=authority,
    )


@pytest.mark.parametrize("field,bad", [
    ("requestId", True), ("requestId", "A" * 32), ("requestId", "a" * 31),
    ("requestId", "a" * 32 + "\n"), ("expectedRevision", None),
    ("expectedRevision", 1), ("schemaVersion", 1),
])
def test_strict_metadata_types(field, bad):
    value = {"schemaVersion": ports.SCHEMA_VERSION, "requestId": ID, "expectedRevision": REV}
    value[field] = bad
    with pytest.raises(ports.RequestError):
        ports.StageArchiveRequest.parse(value)


@pytest.mark.parametrize("field", ["action", "input", "path", "url", "bytes", "argv", "secret"])
def test_streaming_metadata_never_accepts_json_command_or_upload_fields(field):
    with pytest.raises(ports.RequestError, match="invalid_request"):
        ports.StageArchiveRequest.parse({"schemaVersion": ports.SCHEMA_VERSION,
                                        "requestId": ID, "expectedRevision": REV, field: "sentinel"})


def test_import_parser_exact_shape_duplicate_nested_keys_and_nonfinite():
    command = {"schemaVersion": ports.SCHEMA_VERSION, "requestId": ID,
               "expectedRevision": REV, "action": "import_inventory",
               "input": {"archiveRef": GEN, "selectionRef": POLICY}}
    assert ports.InventoryImportRequest.parse(json.dumps(command)) == ports.InventoryImportRequest(ID, REV, GEN, POLICY)
    for raw in [
        json.dumps(command).replace('"selectionRef":', '"archiveRef":'),
        json.dumps({**command, "input": {**command["input"], "path": "/not-read"}}),
        json.dumps({**command, "input": {"archiveRef": GEN}}),
        json.dumps({**command, "input": {"archiveRef": GEN, "selectionRef": True}}),
        json.dumps(command).replace('"import_inventory"', '"stage_archive"'),
        json.dumps(command).replace('"import_inventory"', '"set_schedule"'),
        json.dumps(command).replace('"import_inventory"', '"run_once"'),
        json.dumps(command).replace('"archiveRef": "' + GEN + '"', '"archiveRef": Infinity'),
        "[]", "null", "{", "{" * 1500,
    ]:
        with pytest.raises(ports.RequestError):
            ports.InventoryImportRequest.parse(raw)


def test_exact_envelopes_and_contract_fixture_validation():
    # Reuse only pure, source-owned fixture validators; no runtime integration.
    from test_webui_workflow_contract import validate_workflow, validate_staging_admission

    port, _, _, data = setup()
    validate_workflow(port.observe())
    staged = stage(port, data)
    validate_staging_admission(staged)
    assert set(staged) == {"schemaVersion", "requestId", "decision", "stagedArchive", "error"}
    validate_staging_admission(port.lookup_staging(ID))
    validate_staging_admission(port.lookup_staging("a" * 32))
    admitted = port.import_inventory(import_request(staged, revision=port.observe()["revision"]))
    assert set(admitted) == {"schemaVersion", "workflowId", "requestId", "decision", "receipt", "error"}
    assert set(admitted["receipt"]) == {
        "schemaVersion", "workflowId", "requestId", "runId", "action", "state", "outcome",
        "createdAt", "startedAt", "completedAt", "effect", "error",
    }
    validate_workflow(port.observe())
    assert admitted["receipt"]["effect"] == {"kind": "private_inventory", "state": "verified", "changed": True}
    assert port.observe()["lastVerifiedOutcome"]["effect"]["kind"] == "private_inventory"


def test_stream_reader_shadow_is_never_called_and_closed_stream_rejects():
    port, journal, _, data = setup()
    stream = BytesIO(data)

    def forbidden(_):
        pytest.fail("arbitrary reader invoked")

    stream.read = forbidden
    assert port.stage_archive(stage_request(), stream)["decision"] == "accepted"
    assert journal.stages == ()
    closed = BytesIO(data)
    closed.close()
    before = port.journal
    assert port.stage_archive(stage_request("a" * 32, before.revision), closed)["error"]["code"] == "invalid_request"
    assert port.journal is before


def test_missing_corrupt_and_uncertain_journal_lookup_only_never_consumes_bytes():
    j = ports.TestOnlyPrivateJournal(REV, GEN, POLICY)
    for snapshot in [replace(j, verified=False),
                     replace(j, uncertain_staging=(ID,)), replace(j, uncertain_imports=(ID,))]:
        mock = mock_from(snapshot)
        before = mock.journal
        stream = BytesIO(b"not consumed")
        result = mock.stage_archive(stage_request(), stream)
        assert result["decision"] in {"indeterminate", "rejected"}
        assert result["error"]["code"] in {"admission_unknown", "reconciliation_required"}
        assert stream.tell() == 0 and mock.journal is before
        for lookup in (mock.lookup, mock.lookup_staging):
            if not snapshot.verified:
                assert lookup(ID)["decision"] == "indeterminate"
        new_stage = mock.stage_archive(stage_request("a" * 32), BytesIO())
        assert new_stage["error"]["code"] in {"admission_unknown", "reconciliation_required"}
        new_import = mock.import_inventory(ports.InventoryImportRequest("b" * 32, REV, GEN, POLICY))
        assert new_import["error"]["code"] in {"admission_unknown", "reconciliation_required"}
        assert mock.journal is before
    absent = ports.ReadPort(j, observed_at=1)
    for lookup in (absent.lookup, absent.lookup_staging):
        assert lookup(ID)["error"] == {"code": "request_not_found", "retry": "lookup"}


def test_authority_precedes_existing_receipt_replay_and_stream_reads():
    mock, _, _, data = setup()
    staged = stage(mock, data)
    request = import_request(staged, revision=mock.observe()["revision"])
    mock.import_inventory(request)
    denied = mock_from(mock.journal, authority=False)
    stream = BytesIO(data)
    assert denied.stage_archive(stage_request(), stream)["error"]["code"] == "authority_rejected"
    assert stream.tell() == 0
    assert denied.import_inventory(request)["error"]["code"] == "authority_rejected"
    assert denied.journal is mock.journal


def test_import_rotation_replay_original_and_never_rebind_old_handles():
    mock, _, _, data = setup()
    staged = stage(mock, data)
    request = import_request(staged, revision=mock.observe()["revision"])
    admitted = mock.import_inventory(request)
    rotated = replace(mock.journal, revision="a" * 32, generation="b" * 32, policy_ref="c" * 32)
    restored = mock_from(rotated)
    assert restored.import_inventory(request) == {**admitted, "decision": "replayed"}
    new = replace(request, request_id="d" * 32, expected_revision=rotated.revision)
    assert restored.import_inventory(new)["error"]["code"] == "input_binding_invalid"
    assert restored.observe()["lastVerifiedOutcome"] is None
    assert restored.journal is rotated


def test_schema_review_needs_explicit_selection_acceptance_and_remains_visible():
    for accepted in (False, True):
        mock, _, _, data = setup(inspection="schema_review_required", selection_accepted=accepted)
        staged = stage(mock, data)
        assert staged["stagedArchive"]["inspection"] == "schema_review_required"
        result = mock.import_inventory(import_request(staged, revision=mock.observe()["revision"]))
        if not accepted:
            assert result["error"]["code"] == "input_binding_invalid"
            assert mock.journal.imports == ()
        else:
            assert result["receipt"]["outcome"] == "success"
            detail = ports.ReadPort(mock.journal, observed_at=20).inventory_detail(result["requestId"])
            assert detail["status"] == "inventory_review_required"
    assert ports.TestOnlyInspection(hashlib.sha256(b"test").hexdigest()).selection_accepted is False


def test_ambiguous_selection_never_issues_staging_handles():
    data = b"synthetic ambiguous selection"
    evidence = inspection(data, selection_resolved=False, selection_accepted=False)
    j = ports.TestOnlyPrivateJournal(REV, GEN, POLICY)
    mock = mock_from(j, (evidence,))
    result = stage(mock, data)
    assert result["decision"] == "rejected"
    assert result["stagedArchive"] is None
    assert result["error"]["code"] == "input_binding_invalid"
    assert mock.journal is j


def test_unknown_result_preserves_older_verified_outcome_and_blocks_new_ids():
    first, second = b"synthetic first", b"synthetic second"
    mock = mock_from(ports.TestOnlyPrivateJournal(REV, GEN, POLICY),
                     (inspection(first), inspection(second, inventory_status="secret-sentinel")))
    a = stage(mock, first)
    ra = import_request(a, revision=mock.observe()["revision"])
    good = mock.import_inventory(ra)
    b = stage(mock, second, request_id="a" * 32, revision=mock.observe()["revision"])
    rb = import_request(b, request_id="b" * 32, revision=mock.observe()["revision"])
    unknown = mock.import_inventory(rb)
    assert unknown["decision"] == "accepted" and unknown["receipt"]["outcome"] == "unknown"
    assert unknown["receipt"]["effect"] == {"kind": "private_inventory", "state": "unknown", "changed": None}
    before = mock.journal
    read = ports.ReadPort(before, observed_at=30)
    assert read.lookup(rb.request_id)["receipt"] == unknown["receipt"]
    assert mock.import_inventory(rb) == {**unknown, "decision": "replayed"}
    assert mock.import_inventory(replace(rb, request_id="c" * 32, expected_revision=before.revision))["error"]["code"] == "reconciliation_required"
    obs = mock.observe()
    assert obs["lastAttempt"]["outcome"] == "unknown"
    assert obs["execution"]["state"] == "unknown"
    assert obs["lastVerifiedOutcome"]["effect"] == good["receipt"]["effect"]
    assert "secret-sentinel" not in json.dumps([obs, unknown, read.inventory_detail(rb.request_id)])
    assert mock.journal is before


def test_import_content_noop_and_normalization_rejection_do_not_claim_new_writes():
    mock, _, _, data = setup()
    staged = stage(mock, data)
    req = import_request(staged, revision=mock.observe()["revision"])
    mock.import_inventory(req)
    noop = mock.import_inventory(replace(req, request_id="a" * 32, expected_revision=mock.observe()["revision"]))
    assert noop["receipt"]["effect"]["changed"] is False
    rejected, _, _, data = setup(inventory_status="normalization_rejected")
    staged = stage(rejected, data)
    failure = rejected.import_inventory(import_request(staged, revision=rejected.observe()["revision"]))
    assert failure["receipt"]["outcome"] == "failure"
    assert failure["receipt"]["effect"] == {"kind": "none", "state": "none", "changed": None}
    assert failure["receipt"]["error"]["code"] == "input_rejected"


@pytest.mark.parametrize("timestamp", [True, -1, float("nan"), float("inf"), 10 ** 1000])
def test_invalid_observation_timestamps_fail_closed(timestamp):
    with pytest.raises(ValueError):
        ports.ReadPort(observed_at=timestamp)


def test_immutable_snapshots_and_returned_dtos_cannot_mutate_binding():
    from dataclasses import FrozenInstanceError

    mock, _, _, data = setup()
    staged = stage(mock, data)
    saved = mock.lookup_staging(ID)
    staged["stagedArchive"]["archiveRef"] = "a" * 32
    assert mock.lookup_staging(ID) == saved
    with pytest.raises(FrozenInstanceError):
        mock.journal.revision = "a" * 32
    with pytest.raises(ValueError):
        replace(mock.journal, stages=list(mock.journal.stages))
    with pytest.raises(ValueError):
        replace(mock.journal, stages=mock.journal.stages * 2)
    with pytest.raises(ValueError):
        replace(mock.journal, uncertain_staging=(ID,))


def test_inconsistent_snapshot_cannot_promote_unknown_to_verified_or_forge_noop():
    unknown, _, _, data = setup(inventory_status="secret-unknown-status")
    staged = stage(unknown, data)
    unknown.import_inventory(import_request(staged, revision=unknown.observe()["revision"]))
    record = unknown.journal.imports[0]
    with pytest.raises(ValueError, match="invalid_test_import_evidence"):
        replace(record, status="inventory_only", changed=True)
    assert unknown.observe()["lastAttempt"]["outcome"] == "unknown"
    assert unknown.observe()["lastVerifiedOutcome"] is None
    success, _, _, data = setup()
    staged = stage(success, data)
    success.import_inventory(import_request(staged, revision=success.observe()["revision"]))
    record = success.journal.imports[0]
    with pytest.raises(ValueError, match="invalid_test_journal"):
        replace(success.journal, imports=(replace(record, changed=False),))


# All values below are deliberately tiny TEST-ONLY policy, not proposed B5
# upload/ZIP/spool limits. The helper lives only in tests and never extracts ZIPs.
TEST_ONLY_ENTRIES = 3
TEST_ONLY_EXPANDED = 256
TEST_ONLY_RATIO = 8
TEST_ONLY_JSON_BYTES = 128


def synthetic_zip(entries, *, compressed=False):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def inspect_test_only_zip(data):
    """Synthetic accept/reject oracle, not a shipping parser or deployment policy."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= TEST_ONLY_ENTRIES:
                return None
            names = [info.filename for info in entries]
            if len(names) != len(set(names)) or names != ["conversations.json"]:
                return None  # Fixed TEST-ONLY unambiguous selector.
            if sum(info.file_size for info in entries) > TEST_ONLY_EXPANDED:
                return None
            for info in entries:
                if (info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16)
                        or info.file_size > max(info.compress_size, 1) * TEST_ONLY_RATIO
                        or info.file_size > TEST_ONLY_JSON_BYTES):
                    return None
                with archive.open(info) as member:
                    raw = member.read(TEST_ONLY_JSON_BYTES + 1)
                if len(raw) > TEST_ONLY_JSON_BYTES or type(json.loads(raw)) is not list:
                    return None
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    return inspection(data)


def test_synthetic_bytesio_zip_inspection_fixtures_and_adversarial_rejection():
    good = synthetic_zip([("conversations.json", b"[]")])
    link = zipfile.ZipInfo("conversations.json")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    candidates = [
        synthetic_zip([("../escape.json", b"[]")]),
        synthetic_zip([("/absolute.json", b"[]")]),
        synthetic_zip([("folder/conversations.json", b"[]"), ("other/conversations.json", b"[]")]),
        synthetic_zip([(link, b"target")]),
        synthetic_zip([("conversations.json", b"not-json")]),
        synthetic_zip([("conversations.json", b"[]" + b" " * 200)]),
        synthetic_zip([("conversations.json", b" " * 250)], compressed=True),
        synthetic_zip([(str(i), b"[]") for i in range(4)]),
        good[:-12], b"not a ZIP",
    ]
    with pytest.warns(UserWarning, match="Duplicate name"):
        candidates.append(synthetic_zip([("conversations.json", b"[]"), ("conversations.json", b"[]")]))
    encrypted = bytearray(good)
    # Synthetic encryption flags in local and central headers, no encryption I/O.
    encrypted[6] |= 1
    central = encrypted.index(b"PK\x01\x02")
    encrypted[central + 8] |= 1
    candidates.append(bytes(encrypted))
    assert inspect_test_only_zip(good) is not None
    j = ports.TestOnlyPrivateJournal(REV, GEN, POLICY)
    for i, candidate in enumerate(candidates):
        assert inspect_test_only_zip(candidate) is None
        mock = mock_from(j)
        result = stage(mock, candidate, request_id=f"{i + 10:032x}")
        assert result["error"]["code"] == "input_binding_invalid"
        assert result["stagedArchive"] is None and mock.journal is j
    mock = mock_from(j, (inspect_test_only_zip(good),))
    assert stage(mock, good)["decision"] == "accepted"


def test_source_has_no_runtime_intake_transport_filesystem_or_router_dependencies():
    tree = ast.parse(Path(ports.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "hashlib", "io", "json", "math", "re", "threading", "uuid"}
    forbidden_calls = {"open", "publish_archive", "inventory_ready", "exec", "eval", "compile", "__import__"}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in forbidden_calls for node in ast.walk(tree))
