"""Offline source-only composition tests; no adapter/runtime method is invoked."""

from __future__ import annotations

import ast
from collections.abc import Mapping
import copy
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from obsidian_automation import webui_control_read as read


ROOT = Path(__file__).resolve().parents[1]
IDS = ("b1", "b2", "b3", "b4", "b5")
LABELS = ("AI-derived notes", "Vault Snapshot to private Gitea", "Project data into Vault",
          "GitHub progress into Daily Note", "ChatGPT Export intake")
ACTIONS = (("generate", "set_schedule"), ("run_once", "set_schedule"),
           ("sync_project", "set_schedule"), ("run_once", "set_schedule"),
           ("stage_archive", "import_inventory"))
KINDS = ("review_artifact", "snapshot_ref", "project_vault", "daily_vault", "private_inventory")
WORKFLOW_KEYS = {"schemaVersion", "id", "label", "adapterMode", "revision", "observedAt",
                 "integration", "configuration", "schedule", "execution", "lastAttempt",
                 "lastVerifiedOutcome", "lastAttemptAt", "availableActions", "actionReasons"}
AT = 100.0


def candidate(index=0, mode="read_only"):
    """Explicitly materialized synthetic DTO, not source/attachment discovery."""
    value = {
        "schemaVersion": read.SCHEMA_VERSION, "id": IDS[index], "label": LABELS[index],
        "adapterMode": mode, "revision": "a" * 32, "observedAt": AT,
        "integration": "pending", "configuration": {"state": "configured", "reason": None},
        "schedule": {"state": "unknown", "intervalSeconds": None, "nextDueAt": None,
                     "reason": "scheduler_decision_required" if index in (0, 2) else "read_only"},
        "execution": {"state": "idle", "requestId": None, "startedAt": None},
        "lastAttempt": None, "lastVerifiedOutcome": None, "lastAttemptAt": None,
        "availableActions": [], "actionReasons": {a: "mock_only" if mode == "mock" else "read_only"
                                                   for a in ACTIONS[index]},
    }
    if index == 4:
        value["schedule"].update(state="not_applicable", reason="manual_only")
        value["actionReasons"]["stage_archive"] = "pending_integration"
    return value


def attempted(index=0, *, state="completed", outcome="unknown", mode="mock"):
    value = candidate(index, mode)
    effect_state = "verified" if outcome == "success" else "unknown"
    value["lastAttemptAt"] = 20
    value["lastAttempt"] = {
        "requestId": "b" * 32, "state": state,
        "outcome": outcome if state == "completed" else None,
        "completedAt": 40 if state == "completed" else None,
        "effect": {"kind": KINDS[index], "state": effect_state, "changed": None},
    }
    value["execution"] = {
        "state": ("idle" if outcome == "success" else "unknown") if state == "completed" else state,
        "requestId": "b" * 32 if state != "completed" else None,
        "startedAt": 30 if state == "running" else None,
    }
    return value


def with_verified(value, *, at=10, status="success"):
    value["lastVerifiedOutcome"] = {
        "status": status, "completedAt": at,
        "effect": {"kind": KINDS[IDS.index(value["id"])], "state": "verified", "changed": False},
    }
    return value


class ComposerTests(unittest.TestCase):
    def slot(self, value, index=0):
        return read.compose_read_catalog({IDS[index]: value}, AT)["workflows"][index]

    def assert_neutral(self, value, index=0, integration="unavailable"):
        self.assertEqual(value["integration"], integration)
        self.assertEqual(value["label"], LABELS[index])
        self.assertEqual(value["adapterMode"], "read_only")
        self.assertEqual(value["configuration"]["state"], "not_observed")
        self.assertEqual(value["execution"], dict(state="unknown", requestId=None, startedAt=None))
        for field in ("lastAttempt", "lastAttemptAt", "lastVerifiedOutcome"):
            self.assertIsNone(value[field])
        self.assertEqual(value["availableActions"], [])
        self.assertEqual(set(value["actionReasons"]), set(ACTIONS[index]))
        if index == 4:
            self.assertEqual(value["schedule"], dict(state="not_applicable", intervalSeconds=None,
                                                     nextDueAt=None, reason="manual_only"))
        else:
            self.assertEqual(value["schedule"]["state"], "unknown")

    def assert_rejected(self, value, index=0):
        self.assert_neutral(self.slot(value, index), index)

    def test_empty_catalog_exact_shape_and_neutral_absence(self):
        result = read.compose_read_catalog({}, AT)
        self.assertEqual(set(result), {"schemaVersion", "authority", "observedAt", "workflows"})
        self.assertEqual(result["schemaVersion"], "webui-workflow-control/v1")
        self.assertEqual(result["authority"], "observation_only")
        self.assertEqual(result["observedAt"], AT)
        self.assertEqual([w["id"] for w in result["workflows"]], list(IDS))
        for index, value in enumerate(result["workflows"]):
            with self.subTest(id=IDS[index]):
                self.assertEqual(set(value), WORKFLOW_KEYS)
                self.assertEqual(set(value["configuration"]), {"state", "reason"})
                self.assertEqual(set(value["schedule"]), {"state", "intervalSeconds", "nextDueAt", "reason"})
                self.assertEqual(set(value["execution"]), {"state", "requestId", "startedAt"})
                self.assertEqual(value["revision"], "0" * 32)
                self.assert_neutral(value, index, "not_attached" if index in (1, 3) else "pending")

    def test_reordered_and_missing_ids_are_independent_and_canonical(self):
        observations = {IDS[i]: candidate(i) for i in reversed(range(5))}
        result = read.compose_read_catalog(observations, AT)
        self.assertEqual([w["id"] for w in result["workflows"]], list(IDS))
        self.assertTrue(all(w["revision"] == "a" * 32 for w in result["workflows"]))
        for index in range(5):
            missing = dict(observations)
            del missing[IDS[index]]
            actual = read.compose_read_catalog(missing, AT)["workflows"]
            for other in range(5):
                if other != index:
                    self.assertEqual(actual[other], result["workflows"][other])
            self.assert_neutral(actual[index], index, "not_attached" if index in (1, 3) else "pending")

    def test_extra_unknown_outer_id_invalidates_entire_envelope(self):
        for key in ("b6", "B1", "__proto__", "../../private", 1):
            with self.subTest(key=key):
                values = {"b1": candidate(), key: object()}
                result = read.compose_read_catalog(values, AT)
                for index, slot in enumerate(result["workflows"]):
                    self.assert_neutral(slot, index)

    def test_id_mismatch_and_bad_slot_do_not_invalidate_other_ids(self):
        observations = {IDS[i]: candidate(i) for i in range(5)}
        observations["b1"]["id"] = "b3"
        observations["b5"] = None
        result = read.compose_read_catalog(observations, AT)["workflows"]
        self.assert_neutral(result[0])
        self.assert_neutral(result[4], 4)
        for index in (1, 2, 3):
            self.assertEqual(result[index]["revision"], "a" * 32)

    def test_json_and_duplicate_pair_sequences_are_not_inputs(self):
        raw = '{"b1":null,"b1":' + json.dumps(candidate()) + '}'
        for value in (raw, [("b1", None), ("b1", candidate())], [candidate()], None, True):
            with self.subTest(type=type(value).__name__):
                result = read.compose_read_catalog(value, AT)
                for index, slot in enumerate(result["workflows"]):
                    self.assert_neutral(slot, index)
        # Deliberately expose the limitation: ordinary json.loads has ALREADY
        # lost the duplicate. A caller's strict decoder must reject it first.
        collapsed = json.loads(raw)
        self.assertEqual(read.compose_read_catalog(collapsed, AT),
                         read.compose_read_catalog({"b1": candidate()}, AT))
        nested = json.dumps(candidate()).replace('"state": "configured"',
                                                 '"state":"missing","state":"configured"')
        self.assert_rejected(nested)

    def test_all_missing_and_extra_dto_keys_fail_closed(self):
        value = with_verified(attempted())
        paths = ((), ("configuration",), ("schedule",), ("execution",), ("actionReasons",),
                 ("lastAttempt",), ("lastAttempt", "effect"),
                 ("lastVerifiedOutcome",), ("lastVerifiedOutcome", "effect"))
        self.assertEqual(self.slot(value)["lastAttempt"], value["lastAttempt"])
        for path in paths:
            target = value
            for component in path:
                target = target[component]
            for key in (*target, "private_extra"):
                with self.subTest(path=path, key=key):
                    changed = copy.deepcopy(value)
                    destination = changed
                    for component in path:
                        destination = destination[component]
                    if key == "private_extra":
                        destination[key] = "/private/note/token"
                    else:
                        del destination[key]
                    self.assert_rejected(changed)

    def test_invalid_versions_modes_integration_revisions_and_labels(self):
        cases = {
            "schemaVersion": (None, True, 1, "webui-workflow-control/v9"),
            "adapterMode": (None, True, "connected", "observer"),
            "integration": (None, True, "connected", "healthy"),
            "revision": (True, 32, "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n"),
            "label": (None, True, "", "<script>/private/note?token=secret</script>"),
        }
        for field, values in cases.items():
            for bad in values:
                with self.subTest(field=field, bad=bad):
                    value = candidate()
                    value[field] = bad
                    self.assert_rejected(value)

    def test_supplied_time_is_strict_finite_nonnegative_and_fixed_error(self):
        for bad in (True, False, None, "100", -1, float("inf"), float("-inf"), float("nan"), 10**400):
            with self.subTest(type=type(bad).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid_observed_at$"):
                    read.compose_read_catalog({}, bad)
        for at in (0, 0.5, AT):
            self.assertEqual(read.compose_read_catalog({}, at)["observedAt"], at)

    def test_every_timestamp_rejects_bool_nonfinite_negative_and_wrong_type(self):
        value = with_verified(attempted(state="running"))
        for path in (("observedAt",), ("lastAttemptAt",), ("execution", "startedAt"),
                     ("lastVerifiedOutcome", "completedAt")):
            for bad in (True, False, -1, "10", float("nan"), float("inf"), 10**400):
                with self.subTest(path=path, bad=type(bad).__name__):
                    changed = copy.deepcopy(value)
                    target = changed if len(path) == 1 else changed[path[0]]
                    target[path[-1]] = bad
                    self.assert_rejected(changed)
        for bad in (True, -1, "40", None, float("nan"), float("inf"), 10**400):
            changed = attempted()
            changed["lastAttempt"]["completedAt"] = bad
            self.assert_rejected(changed)
        for index in (1, 3):
            for bad in (True, -1, "40", float("nan"), float("inf"), 10**400):
                changed = candidate(index)
                changed["schedule"].update(state="enabled", intervalSeconds=86400, nextDueAt=bad, reason=None)
                self.assert_rejected(changed, index)

    def test_chronology_and_active_request_correlation(self):
        cases = [
            (("observedAt",), 101), (("lastAttemptAt",), 101),
            (("execution", "startedAt"), 19), (("execution", "startedAt"), 101),
            (("execution", "requestId"), "c" * 32),
            (("lastVerifiedOutcome", "completedAt"), 21),
        ]
        for path, bad in cases:
            changed = with_verified(attempted(state="running"))
            target = changed if len(path) == 1 else changed[path[0]]
            target[path[-1]] = bad
            self.assert_rejected(changed)
        for at in (19, 101):
            changed = attempted()
            changed["lastAttempt"]["completedAt"] = at
            self.assert_rejected(changed)
        changed = with_verified(attempted(outcome="success"), at=40)
        self.assert_rejected(changed)  # same completion time but different effect
        changed["lastVerifiedOutcome"]["effect"] = copy.deepcopy(changed["lastAttempt"]["effect"])
        self.assertIsNone(self.slot(changed)["lastAttempt"])

    def test_unknown_enums_and_reason_types(self):
        for path in (("configuration", "state"), ("configuration", "reason"),
                     ("schedule", "state"), ("schedule", "reason"),
                     ("execution", "state"), ("lastAttempt", "state"),
                     ("lastAttempt", "outcome"), ("lastAttempt", "effect", "kind"),
                     ("lastAttempt", "effect", "state"),
                     ("lastVerifiedOutcome", "status"), ("actionReasons", "generate")):
            for bad in (True, 1, [], {}, "private error /token", "__proto__"):
                with self.subTest(path=path, type=type(bad).__name__):
                    changed = with_verified(attempted())
                    target = changed
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = bad
                    self.assert_rejected(changed)

    def test_effect_kinds_and_changed_are_typed_and_product_specific(self):
        for index in range(5):
            for bad in ("none", KINDS[(index + 1) % 5], "connected", "private text"):
                value = attempted(index)
                value["lastAttempt"]["effect"]["kind"] = bad
                self.assert_rejected(value, index)
            for bad in (True, False, 0, 1, "false", []):
                value = attempted(index)
                value["lastAttempt"]["effect"]["changed"] = bad
                self.assert_rejected(value, index)
            for bad in (0, 1, "true", []):
                value = attempted(index, outcome="success")
                value["lastAttempt"]["effect"]["changed"] = bad
                self.assert_rejected(value, index)

    def test_active_completed_and_history_invariants(self):
        mutations = [
            ("execution", "state", "idle"), ("execution", "requestId", None),
            ("lastAttempt", "completedAt", 40), ("lastAttempt", "outcome", "success"),
            ("lastAttempt", "state", "completed"), ("lastAttempt", "requestId", "B" * 32),
        ]
        for section, field, bad in mutations:
            value = attempted(state="running")
            value[section][field] = bad
            self.assert_rejected(value)
        value = attempted(state="queued")
        value["execution"]["startedAt"] = 30
        self.assert_rejected(value)
        value = attempted(outcome="success")
        value["lastAttempt"]["effect"]["state"] = "unknown"
        self.assert_rejected(value)
        value = attempted()
        value["lastAttempt"]["effect"] = dict(kind="none", state="none", changed=None)
        self.assert_rejected(value)  # unknown outcome cannot establish no effect
        value = candidate()
        value["lastAttemptAt"] = 20
        self.assert_rejected(value)
        value = with_verified(candidate())
        self.assert_rejected(value)

    def test_configuration_state_reason_pairings(self):
        for state, reason in (("configured", None), ("missing", "configuration_missing"),
                              ("invalid", "configuration_invalid"), ("not_observed", "read_only")):
            value = candidate()
            value["configuration"] = dict(state=state, reason=reason)
            self.assertEqual(self.slot(value)["configuration"], value["configuration"])
            value["configuration"]["reason"] = "busy"
            self.assert_rejected(value)

    def test_b1_b3_schedule_is_never_enabled_or_decided(self):
        for index in (0, 2):
            for state in ("enabled", "disabled", "not_applicable"):
                value = candidate(index)
                value["schedule"].update(state=state, intervalSeconds=60)
                self.assert_rejected(value, index)
            value = candidate(index)
            value["schedule"]["reason"] = None
            self.assert_rejected(value, index)

    def test_b5_is_manual_only_on_every_fallback_and_accepted_path(self):
        for value in (None, {}, candidate(4), candidate(4, "mock"), attempted(4, outcome="success")):
            result = self.slot(value, 4)
            self.assertEqual(result["schedule"], dict(state="not_applicable", intervalSeconds=None,
                                                      nextDueAt=None, reason="manual_only"))
        for field, bad in (("state", "enabled"), ("intervalSeconds", 60),
                           ("nextDueAt", 100), ("reason", "due_unavailable")):
            value = candidate(4)
            value["schedule"][field] = bad
            self.assert_rejected(value, 4)

    def test_safe_b2_b4_schedule_projection_does_not_infer_connected(self):
        for index, lower in ((1, 60), (3, 3600)):
            for state, due, reason in (("enabled", 200, None), ("disabled", None, None),
                                       ("enabled", None, "due_unavailable")):
                value = candidate(index)
                value["schedule"].update(state=state, intervalSeconds=lower, nextDueAt=due, reason=reason)
                result = self.slot(value, index)
                self.assertEqual(result["schedule"], value["schedule"])
                self.assertEqual(result["integration"], "pending")
                self.assertEqual(result["availableActions"], [])
            for bad in (True, False, lower - 1, 86401, 3600.0, "3600"):
                value = candidate(index)
                value["schedule"].update(state="enabled", intervalSeconds=bad, nextDueAt=200, reason=None)
                self.assert_rejected(value, index)
            value = candidate(index)
            value["schedule"].update(state="enabled", intervalSeconds=lower, nextDueAt=200, reason=None)
            value["configuration"] = dict(state="missing", reason="configuration_missing")
            self.assert_rejected(value, index)

    def test_no_actions_connected_state_or_private_text_for_b1_b3_b5(self):
        for index in (0, 2, 4):
            for mode in ("read_only", "mock"):
                for bad in (list(ACTIONS[index]), ["__proto__"], ["generate", "generate"], (), True):
                    value = attempted(index, outcome="success", mode=mode)
                    value["availableActions"] = bad
                    self.assert_rejected(value, index)
                for field in ("integration", "adapterMode"):
                    value = candidate(index, mode)
                    value[field] = "connected"
                    self.assert_rejected(value, index)
                for field in ("note", "path", "payload", "argv", "error"):
                    value = attempted(index, outcome="success", mode=mode)
                    value[field] = "secret /private/note token=credential"
                    result = self.slot(value, index)
                    self.assert_neutral(result, index)
                    self.assertNotIn("secret", json.dumps(result))

    def test_read_only_verification_rejected_mock_verification_suppressed(self):
        for index in range(5):
            value = with_verified(attempted(index, outcome="success"), at=40)
            value["lastVerifiedOutcome"]["effect"] = copy.deepcopy(value["lastAttempt"]["effect"])
            result = self.slot(value, index)
            self.assertEqual(result["adapterMode"], "mock")
            self.assertEqual(result["label"], LABELS[index] + " (mock)")
            self.assertEqual(result["integration"], "pending")
            self.assertEqual(result["execution"]["state"], "idle")
            self.assertIsNone(result["lastAttempt"])
            self.assertIsNone(result["lastAttemptAt"])
            self.assertIsNone(result["lastVerifiedOutcome"])
            self.assertNotIn('"verified"', json.dumps(result))
            value["adapterMode"] = "read_only"
            self.assert_rejected(value, index)

    def test_safe_active_unknown_and_none_failure_are_preserved_not_invented(self):
        for index in range(5):
            for state in ("queued", "running", "completed"):
                value = attempted(index, state=state, mode="read_only")
                result = self.slot(value, index)
                for field in ("execution", "lastAttempt", "lastAttemptAt"):
                    self.assertEqual(result[field], value[field])
            value = attempted(index, outcome="failure", mode="read_only")
            value["lastAttempt"]["effect"] = dict(kind="none", state="none", changed=None)
            value["execution"]["state"] = "idle"
            self.assertEqual(self.slot(value, index)["lastAttempt"], value["lastAttempt"])
            # Historical mock success is removed without changing newer unknown.
            value = with_verified(attempted(index))
            result = self.slot(value, index)
            self.assertIsNone(result["lastVerifiedOutcome"])
            self.assertEqual(result["lastAttempt"], value["lastAttempt"])

    def test_unavailable_not_attached_snapshots_are_neutralized(self):
        for index in range(5):
            for integration in ("unavailable", "not_attached"):
                value = attempted(index, outcome="success")
                value["integration"] = integration
                self.assert_neutral(self.slot(value, index), index, integration)

    def test_output_is_stable_deeply_detached_and_does_not_mutate_inputs(self):
        observations = {IDS[i]: with_verified(attempted(i)) for i in range(5)}
        before = copy.deepcopy(observations)
        first = read.compose_read_catalog(observations, AT)
        second = read.compose_read_catalog(observations, AT)
        self.assertEqual(observations, before)
        self.assertEqual(first, second)
        slot = first["workflows"][0]
        slot["configuration"]["state"] = "invalid"
        slot["schedule"]["reason"] = "busy"
        slot["execution"]["state"] = "idle"
        slot["lastAttempt"]["effect"]["kind"] = "none"
        slot["actionReasons"]["generate"] = "busy"
        slot["availableActions"].append("generate")
        self.assertEqual(observations, before)
        self.assertEqual(read.compose_read_catalog(observations, AT), second)
        observations["b1"]["lastAttempt"]["effect"]["kind"] = "none"
        self.assertEqual(second["workflows"][0]["lastAttempt"]["effect"]["kind"], "review_artifact")
        empty = read.compose_read_catalog({}, AT)
        empty["workflows"][0]["execution"]["state"] = "idle"
        self.assertEqual(empty["workflows"][1]["execution"]["state"], "unknown")

    def test_candidate_observation_time_preserved_no_clock_or_freshness_inference(self):
        value = candidate()
        value["observedAt"] = 0
        result = read.compose_read_catalog({"b1": value}, AT)
        self.assertEqual(result["observedAt"], AT)
        self.assertEqual(result["workflows"][0]["observedAt"], 0)
        self.assertEqual(result["workflows"][0]["integration"], "pending")

    def test_fixed_fixture_labels_and_explicit_mock_marker(self):
        for index in range(5):
            value = candidate(index, "mock")
            value["label"] += " (mock)"
            self.assertEqual(self.slot(value, index)["label"], LABELS[index] + " (mock)")
            value["adapterMode"] = "read_only"
            self.assert_rejected(value, index)  # never erase an explicit mock label
        value = candidate(4, "mock")
        value["label"] += " (TEST-ONLY mock)"
        self.assertEqual(self.slot(value, 4)["label"], LABELS[4] + " (mock)")

    def test_scalar_and_nested_container_subclasses_are_not_plain_dtos(self):
        def bomb(*args, **kwargs):
            raise AssertionError("subclass hook was invoked")

        class StringHook(str):
            __eq__ = __hash__ = bomb

        class IntHook(int):
            __ge__ = __le__ = __float__ = bomb

        class ListHook(list):
            __bool__ = __iter__ = bomb

        class DictHook(dict):
            __iter__ = keys = values = bomb

        for field, bad in (("id", StringHook("b1")), ("observedAt", IntHook(100)),
                            ("availableActions", ListHook()),
                            ("actionReasons", DictHook(candidate()["actionReasons"])),
                            ("configuration", DictHook(candidate()["configuration"]))):
            value = candidate()
            value[field] = bad
            self.assert_rejected(value)
        with self.assertRaisesRegex(ValueError, "^invalid_observed_at$"):
            read.compose_read_catalog({}, IntHook(100))

    def test_common_contract_fixture_is_not_blanket_runtime_acceptance(self):
        text = (ROOT / "docs/webui-workflow-control-contract-v1.md").read_text(encoding="utf-8")
        fixture = json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))
        actual = read.compose_read_catalog({w["id"]: w for w in fixture["workflows"]}, fixture["observedAt"])
        self.assertEqual([w["id"] for w in actual["workflows"]], list(IDS))
        for index, value in enumerate(actual["workflows"]):
            self.assertEqual(set(value), WORKFLOW_KEYS)
            self.assertNotEqual(value["integration"], "connected")
            self.assertEqual(value["availableActions"], [])
            self.assertIsNone(value["lastVerifiedOutcome"])
            if index in (1, 2, 3):
                self.assert_neutral(value, index)  # connected mocks/unavailable
            else:
                self.assertEqual(value["adapterMode"], "mock")

    def test_lazy_mappings_subclasses_callbacks_and_effect_hooks_are_not_called(self):
        def bomb(*args, **kwargs):
            raise AssertionError("effect/hook was invoked")

        class Lazy(Mapping):
            __getitem__ = __iter__ = __len__ = bomb

        class DictHook(dict):
            __iter__ = __getitem__ = items = keys = bomb

        class ScalarHook:
            __eq__ = __bool__ = __iter__ = __deepcopy__ = bomb

        for outer in (Lazy(), DictHook(b1=candidate()), bomb):
            result = read.compose_read_catalog(outer, AT)
            for index, value in enumerate(result["workflows"]):
                self.assert_neutral(value, index)
        for value in (Lazy(), DictHook(candidate()), bomb):
            self.assert_rejected(value)
        for path in (("configuration",), ("schedule", "reason"), ("execution", "state"),
                     ("lastAttempt", "effect", "state"), ("lastAttempt", "effect", "changed")):
            value = attempted()
            target = value
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = ScalarHook()
            self.assert_rejected(value)
        with patch("builtins.open", bomb), patch("socket.socket", bomb), \
                patch("subprocess.Popen", bomb), patch("os.stat", bomb), \
                patch("os.listdir", bomb), patch("time.time", bomb):
            result = read.compose_read_catalog({IDS[i]: candidate(i) for i in range(5)}, AT)
        self.assertTrue(all(w["revision"] == "a" * 32 for w in result["workflows"]))

    def test_source_has_no_runtime_dependency_or_route(self):
        source = (ROOT / "src/obsidian_automation/webui_control_read.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
            self.assertNotIsInstance(node, (ast.ClassDef, ast.AsyncFunctionDef))
        self.assertEqual(imports, {"__future__", "math", "re"})
        for forbidden in ("/api/", "open(", "observe(", "lookup(", "inventory_ready(", "uuid4("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
