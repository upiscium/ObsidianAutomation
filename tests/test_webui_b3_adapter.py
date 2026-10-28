"""Bounded offline B3 mock acceptance: no legacy workers or external dependencies."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import unittest

from obsidian_automation.webui_b3_adapter import (
    B3FakeAdapter, Binding, InvalidRequest, ProjectSyncRequest,
    SyntheticInterruption, SyntheticLedger, SyntheticWriter, VERSION,
)


class B3Fixture(unittest.TestCase):
    def setUp(self):
        self.binding = Binding("b" * 32, "c" * 32)
        self.writer = SyntheticWriter(self.binding)
        self.ledger = SyntheticLedger.fresh_fixture(self.binding, self.writer)
        self.adapter = B3FakeAdapter(self.ledger, manual_simulation=True)

    def request(self, request_id="a" * 32, *, revision=None, binding_ref=None):
        return ProjectSyncRequest(request_id, revision or self.adapter.observe()["revision"],
                                  binding_ref or self.binding.bindingRef)

    def payload(self):
        return {"schemaVersion": VERSION, "requestId": "a" * 32,
                "expectedRevision": self.adapter.observe()["revision"],
                "action": "sync_project", "input": {"bindingRef": self.binding.bindingRef}}

    def test_observation_is_mock_typed_and_not_authority(self):
        before = self.adapter.observe()
        self.assertEqual(before["revision"], self.adapter.observe()["revision"])
        self.assertEqual(set(before), {
            "schemaVersion", "id", "label", "adapterMode", "revision", "observedAt",
            "integration", "configuration", "schedule", "execution", "lastAttempt",
            "lastVerifiedOutcome", "lastAttemptAt", "availableActions", "actionReasons",
        })
        self.assertEqual(before["schemaVersion"], VERSION)
        self.assertEqual(before["adapterMode"], "mock")
        self.assertIn("mock", before["label"])
        self.assertNotEqual(before["integration"], "connected")
        self.assertEqual(before["availableActions"], [])
        self.assertEqual(before["actionReasons"], {
            "sync_project": "mock_only", "set_schedule": "scheduler_decision_required"})
        self.assertEqual(before["schedule"], {"state": "unknown", "intervalSeconds": None,
                                             "nextDueAt": None,
                                             "reason": "scheduler_decision_required"})
        closed = B3FakeAdapter(self.ledger)
        self.assertEqual(closed.sync_project(self.request()).error.code, "mock_only")
        readonly = B3FakeAdapter(self.ledger, manual_simulation=True, read_only=True)
        self.assertEqual(readonly.sync_project(self.request()).error.code, "read_only")
        self.assertEqual(self.writer.calls, 0)

    def test_exact_request_and_duplicate_json_keys(self):
        payload = self.payload()
        self.assertEqual(ProjectSyncRequest.from_mapping(payload), self.request())
        self.assertEqual(ProjectSyncRequest.from_json(json.dumps(payload)), self.request())
        for key, value in (("schemaVersion", "webui-workflow-control/v9"),
                           ("requestId", True), ("requestId", "A" * 32),
                           ("expectedRevision", "../private"), ("action", "execute"),
                           ("input", {"bindingRef": "/some/path"}),
                           ("input", {"bindingRef": "b" * 32, "path": "forbidden"}),
                           ("input", {"bindingRef": "b" * 32, "argv": []})):
            with self.subTest(key=key, value=value), self.assertRaises(InvalidRequest):
                ProjectSyncRequest.from_mapping({**payload, key: value})
        for value in ({**payload, "authority": True}, [], None):
            with self.assertRaises(InvalidRequest):
                ProjectSyncRequest.from_mapping(value)
        encoded = json.dumps(payload)
        for raw in (encoded[:-1] + ',"requestId":"' + "d" * 32 + '"}',
                    encoded.replace('"bindingRef":', '"bindingRef":"' + "d" * 32 + '","bindingRef":'),
                    encoded.replace('"bindingRef": "' + "b" * 32 + '"', '"bindingRef": NaN'),
                    '{', encoded.replace('"bindingRef": "' + "b" * 32 + '"', '"bindingRef": Infinity')):
            with self.subTest(raw=raw), self.assertRaises(InvalidRequest):
                ProjectSyncRequest.from_json(raw)
        self.assertEqual(self.writer.calls, 0)

    def test_duplicate_replays_completed_receipt_before_stale_check(self):
        request = self.request()
        first = self.adapter.sync_project(request)
        replay = self.adapter.sync_project(request)
        self.assertEqual(first.decision, "accepted")
        self.assertEqual(replay.decision, "replayed")
        self.assertEqual(first.receipt, replay.receipt)
        self.assertNotEqual(request.expectedRevision, self.adapter.observe()["revision"])
        self.assertEqual(self.writer.calls, 1)
        self.assertEqual(first.receipt.effect.state, "verified")
        self.assertTrue(first.receipt.effect.changed)
        self.assertEqual(first.receipt.outcome, "success")
        self.assertEqual(set(first.to_dict()), {
            "schemaVersion", "workflowId", "requestId", "decision", "receipt", "error"})
        self.assertEqual(set(first.to_dict()["receipt"]), {
            "schemaVersion", "workflowId", "requestId", "runId", "action", "state", "outcome",
            "createdAt", "startedAt", "completedAt", "effect", "error"})

    def test_concurrent_same_id_and_multiple_adapter_instances(self):
        request = self.request()
        adapters = [B3FakeAdapter(self.ledger, manual_simulation=True) for _ in range(16)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda a: a.sync_project(request), adapters))
        self.assertEqual(sum(r.decision == "accepted" for r in results), 1)
        self.assertTrue(all(r.decision in {"accepted", "replayed"} for r in results))
        self.assertEqual(len({r.receipt.runId for r in results}), 1)
        self.assertEqual(self.writer.calls, 1)

    def test_conflict_and_stale_and_bad_binding_do_not_apply(self):
        request = self.request()
        self.adapter.sync_project(request)
        conflict = self.adapter.sync_project(replace(request, bindingRef="d" * 32))
        self.assertEqual(conflict.error.code, "request_conflict")
        stale = self.adapter.sync_project(replace(request, requestId="e" * 32))
        self.assertEqual(stale.error.code, "stale_revision")
        invalid = self.adapter.sync_project(self.request("f" * 32, binding_ref="d" * 32))
        self.assertEqual(invalid.error.code, "input_binding_invalid")
        self.assertIsNone(stale.receipt)
        self.assertEqual(self.writer.calls, 1)

    def test_adapter_restart_uses_injected_port_and_rotation_replays_old_scope(self):
        request = self.request()
        first = self.adapter.sync_project(request)
        restarted = B3FakeAdapter(self.ledger, manual_simulation=True)
        self.assertEqual(restarted.lookup(request.requestId).receipt, first.receipt)
        new_binding = Binding("d" * 32, "e" * 32, "new-project", "new-destination")
        new_writer = SyntheticWriter(new_binding)
        self.ledger.rotate_binding(new_binding, new_writer)
        self.assertIsNone(restarted.observe()["lastVerifiedOutcome"])
        replay = restarted.sync_project(request)
        self.assertEqual(replay.receipt, first.receipt)
        self.assertEqual(replay.decision, "replayed")
        self.assertEqual(new_writer.calls, 0)
        conflict = restarted.sync_project(replace(request, bindingRef=new_binding.bindingRef))
        self.assertEqual(conflict.error.code, "request_conflict")
        self.assertEqual(new_writer.calls, 0)
        with self.assertRaises(ValueError):
            self.ledger.rotate_binding(replace(new_binding, generation="f" * 32), new_writer)

    def test_unknown_or_lost_or_corrupt_ledger_never_initializes_or_reexecutes(self):
        request = self.request()
        first = self.adapter.sync_project(request)
        self.ledger.lose_history()
        for ledger in (None, self.ledger, SyntheticLedger()):
            adapter = B3FakeAdapter(ledger, manual_simulation=True)
            result = adapter.sync_project(request)
            self.assertEqual(result.decision, "indeterminate")
            self.assertEqual(result.error.code, "admission_unknown")
            self.assertEqual(adapter.lookup(request.requestId).decision, "indeterminate")
            self.assertEqual(adapter.observe()["execution"]["state"], "unknown")
            self.assertIsNone(adapter.observe()["lastVerifiedOutcome"])
        self.assertEqual(first.receipt.outcome, "success")
        self.assertEqual(self.writer.calls, 1)
        corrupt = SyntheticLedger.fresh_fixture(self.binding, self.writer)
        corrupt._records["a" * 32] = object()  # controlled corrupt-port fixture
        adapter = B3FakeAdapter(corrupt, manual_simulation=True)
        self.assertEqual(adapter.sync_project(request).decision, "indeterminate")
        self.assertEqual(adapter.observe()["integration"], "unavailable")
        self.assertEqual(adapter.observe()["actionReasons"]["set_schedule"], "observation_unavailable")

    def test_interruption_lookup_has_no_recovery_effect_and_blocks_new_intent(self):
        self.writer.mode = "interrupt"
        request = self.request()
        with self.assertRaises(SyntheticInterruption):
            self.adapter.sync_project(request)
        restarted = B3FakeAdapter(self.ledger, manual_simulation=True)
        receipt = restarted.lookup(request.requestId).receipt
        self.assertEqual(receipt.state, "running")
        self.assertIsNone(receipt.outcome)
        self.assertEqual(receipt.effect.state, "unknown")
        before = dict(self.ledger._records)
        restarted.observe()
        restarted.lookup(request.requestId)
        restarted.detail()
        self.assertEqual(self.ledger._records, before)
        self.assertEqual(restarted.sync_project(self.request("e" * 32)).error.code, "busy")
        self.ledger.mark_interrupted(request.requestId, at=1793145601)
        self.assertEqual(restarted.lookup(request.requestId).receipt.outcome, "unknown")
        self.assertEqual(restarted.observe()["execution"]["state"], "unknown")
        self.assertEqual(restarted.sync_project(self.request("f" * 32)).error.code,
                         "reconciliation_required")
        self.assertEqual(restarted.sync_project(request).decision, "replayed")
        self.assertEqual(self.writer.calls, 1)

    def test_corrupt_stored_receipts_fail_closed_instead_of_replaying_unknown_enums(self):
        request = self.request()
        self.adapter.sync_project(request)
        original = self.ledger._records[request.requestId]
        for field, value in (("state", "invented"), ("state", []), ("outcome", "invented"),
                             ("completedAt", float("nan")), ("createdAt", True),
                             ("schemaVersion", "webui-workflow-control/v9"),
                             ("action", "promote"), ("runId", "private-path")):
            with self.subTest(field=field):
                self.ledger._records[request.requestId] = replace(
                    original, receipt=replace(original.receipt, **{field: value}))
                self.assertEqual(self.adapter.lookup(request.requestId).decision, "indeterminate")
                self.assertEqual(self.adapter.sync_project(request).decision, "indeterminate")
                self.assertEqual(self.adapter.observe()["integration"], "unavailable")
                self.assertIsNone(self.adapter.observe()["lastAttempt"])
        self.ledger._records[request.requestId] = replace(original, evidence=())
        self.assertEqual(self.adapter.lookup(request.requestId).decision, "indeterminate")
        for field in ("subeffect", "outcome"):
            bad_evidence = (replace(original.evidence[0], **{field: []}), original.evidence[1])
            self.ledger._records[request.requestId] = replace(original, evidence=bad_evidence)
            self.assertEqual(self.adapter.lookup(request.requestId).decision, "indeterminate")
            self.assertEqual(self.adapter.sync_project(request).decision, "indeterminate")
            self.assertEqual(self.adapter.observe()["integration"], "unavailable")
        self.assertEqual(self.writer.calls, 1)

    def test_mock_has_no_runtime_port_or_effectful_constructor_dependency(self):
        class ForbiddenRuntimePort:
            def __getattr__(self, name):
                raise AssertionError("runtime port accessed")

        with self.assertRaises(ValueError):
            SyntheticLedger.fresh_fixture(self.binding, ForbiddenRuntimePort())
        before = self.writer.calls
        new_adapter = B3FakeAdapter(self.ledger, manual_simulation=True)
        new_adapter.observe()
        new_adapter.detail()
        new_adapter.lookup("a" * 32)
        self.assertEqual(self.writer.calls, before)
        self.assertEqual(self.ledger._records, {})

    def test_queue_partial_stale_unknown_and_bad_readback_are_not_effect(self):
        for mode in ("queue_only", "partial", "stale_evidence", "unknown_status", "bad_readback"):
            with self.subTest(mode=mode):
                writer = SyntheticWriter(self.binding, mode=mode)
                ledger = SyntheticLedger.fresh_fixture(self.binding, writer)
                ledger.set_observations(watcher="collected", mirror="current")
                adapter = B3FakeAdapter(ledger, manual_simulation=True)
                request = ProjectSyncRequest("a" * 32, adapter.observe()["revision"], "b" * 32)
                result = adapter.sync_project(request)
                self.assertEqual(result.receipt.outcome, "unknown")
                self.assertEqual(result.receipt.effect.state, "unknown")
                self.assertEqual(result.receipt.error.code, "effect_unconfirmed")
                self.assertIsNone(adapter.observe()["lastVerifiedOutcome"])
                self.assertEqual(adapter.detail()["watcher"], "collected")
                self.assertEqual(adapter.detail()["mirror"], "current")
                self.assertNotIn("private untrusted", json.dumps(result.to_dict()))
                self.assertNotIn(self.binding.destination, json.dumps(adapter.observe()))
                blocked = adapter.sync_project(replace(request, requestId="e" * 32,
                                                       expectedRevision=adapter.observe()["revision"]))
                self.assertEqual(blocked.error.code, "reconciliation_required")

    def test_cas_conflict_preserves_immutable_binding_owner_edit_and_managed_bytes(self):
        self.writer.mode = "cas_conflict"
        before = self.writer.documents
        result = self.adapter.sync_project(self.request())
        after = self.writer.documents
        self.assertEqual(result.receipt.outcome, "unknown")
        self.assertEqual(self.adapter.detail()["cas"], "conflict")
        self.assertEqual(after["status"].binding, before["status"].binding)
        self.assertEqual(after["status"].owner_text, "owner edited text")
        self.assertEqual(after["status"].managed_text, before["status"].managed_text)
        self.assertGreater(after["status"].etag, before["status"].etag)
        self.assertEqual(after["overview"], before["overview"])
        with self.assertRaises(FrozenInstanceError):
            self.binding.project = "browser replacement"

    def test_verified_noop_not_counted_as_fresh_write_and_older_success_retained(self):
        first = self.adapter.sync_project(self.request())
        second = self.adapter.sync_project(self.request("e" * 32))
        self.assertTrue(first.receipt.effect.changed)
        self.assertFalse(second.receipt.effect.changed)
        self.assertEqual(second.receipt.outcome, "success")
        self.writer.mode = "queue_only"
        third = self.adapter.sync_project(self.request("f" * 32))
        observation = self.adapter.observe()
        self.assertEqual(third.receipt.outcome, "unknown")
        self.assertEqual(observation["lastAttempt"]["outcome"], "unknown")
        self.assertEqual(observation["lastVerifiedOutcome"]["status"], "success")
        self.assertFalse(observation["lastVerifiedOutcome"]["effect"]["changed"])
        self.assertTrue(all(d.owner_text == "owner text" for d in self.writer.documents.values()))

    def test_absent_writer_and_missing_configuration_fail_before_admission(self):
        for binding, code in ((self.binding, "not_attached"), (None, "configuration_missing")):
            ledger = SyntheticLedger.fresh_fixture(binding)
            adapter = B3FakeAdapter(ledger, manual_simulation=True)
            request = ProjectSyncRequest("a" * 32, adapter.observe()["revision"], "b" * 32)
            self.assertEqual(adapter.sync_project(request).error.code, code)
            self.assertEqual(adapter.lookup(request.requestId).error.code, "request_not_found")
            self.assertIsNone(adapter.observe()["lastAttempt"])
        self.assertEqual(self.writer.calls, 0)

    def test_schedule_setter_requires_decision_and_never_creates_receipt(self):
        payload = {**self.payload(), "action": "set_schedule",
                   "input": {"enabled": True, "intervalSeconds": 123}}
        before = self.adapter.observe()
        result = self.adapter.set_schedule(payload)
        self.assertEqual(set(result), {"schemaVersion", "workflowId", "requestId", "decision",
                                       "schedule", "revision", "error"})
        self.assertEqual(result["error"]["code"], "scheduler_decision_required")
        self.assertIsNone(result["schedule"])
        self.assertIsNone(result["revision"])
        self.assertEqual(self.adapter.observe(), before)
        for inputs in ({"enabled": 1, "intervalSeconds": 1},
                       {"enabled": True, "intervalSeconds": True},
                       {"enabled": True, "intervalSeconds": 1.5},
                       {"enabled": True, "intervalSeconds": 1, "engine": "systemd"}):
            with self.assertRaises(InvalidRequest):
                self.adapter.set_schedule({**payload, "input": inputs})

    def test_returned_dtos_cannot_mutate_server_receipts_or_observations(self):
        self.writer.mode = "queue_only"
        result = self.adapter.sync_project(self.request())
        dto = result.to_dict()
        dto["receipt"]["effect"]["state"] = "verified"
        dto["receipt"]["error"]["code"] = "success"
        with self.assertRaises(FrozenInstanceError):
            result.receipt.error.code = "success"
        observation = self.adapter.observe()
        observation["lastAttempt"]["effect"]["state"] = "verified"
        self.assertEqual(self.adapter.lookup(result.requestId).receipt.effect.state, "unknown")
        self.assertEqual(self.adapter.lookup(result.requestId).receipt.error.code, "effect_unconfirmed")

    def test_evidence_requires_both_subeffects_exact_bytes_scope_run_and_request(self):
        request = self.request()
        result = self.adapter.sync_project(request)
        receipt = result.receipt
        evidence = self.ledger._records[request.requestId].evidence
        self.assertTrue(self.adapter._verified(evidence, receipt, self.binding))
        bad = [(), (object(), object()), (evidence[0], evidence[0])]
        for field, value in (("runId", "f" * 32), ("requestId", "f" * 32),
                             ("binding", replace(self.binding, generation="f" * 32)),
                             ("observed", b"not exact bytes"), ("outcome", "queued"),
                             ("subeffect", []), ("outcome", [])):
            bad.append((replace(evidence[0], **{field: value}), evidence[1]))
        for values in bad:
            with self.subTest(values=values):
                self.assertFalse(self.adapter._verified(values, receipt, self.binding))

    def test_unavailable_observation_is_neutral_and_reads_do_not_call_writer(self):
        self.ledger.lose_history()
        before = self.writer.calls
        value = self.adapter.observe()
        self.assertEqual(value["configuration"]["state"], "not_observed")
        self.assertEqual(value["execution"]["state"], "unknown")
        self.assertEqual(value["schedule"]["reason"], "observation_unavailable")
        self.assertEqual(value["availableActions"], [])
        self.assertIsNone(value["lastAttempt"])
        self.adapter.lookup("a" * 32)
        self.assertEqual(self.writer.calls, before)
        self.assertFalse(self.ledger._known)
        for invalid_time in (True, float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                B3FakeAdapter(self.ledger, observed_at=invalid_time)


if __name__ == "__main__":
    unittest.main()
