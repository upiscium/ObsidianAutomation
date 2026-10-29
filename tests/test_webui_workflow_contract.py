"""Pure contract examples/source checks, NOT runtime adapter acceptance.

No application imports, jobs, journal initialization, sockets or subprocesses.
The validator below intentionally checks only the frozen fixture invariants;
it is not a shared production schema implementation.
"""
from __future__ import annotations

import ast
import copy
import json
import math
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/webui-workflow-control-contract-v1.md"
VERSION = "webui-workflow-control/v1"
IDS = ("b1", "b2", "b3", "b4", "b5")
ACTIONS = {
    "b1": {"generate", "set_schedule"},
    "b2": {"run_once", "set_schedule"},
    "b3": {"sync_project", "set_schedule"},
    "b4": {"run_once", "set_schedule"},
    "b5": {"stage_archive", "import_inventory"},
}
HEX = re.compile(r"[a-f0-9]{32}\Z")
STAGING_REQUEST_KEYS = {"schemaVersion", "requestId", "expectedRevision"}
STAGING_ADMISSION_KEYS = {"schemaVersion", "requestId", "decision", "stagedArchive", "error"}
STAGED_ARCHIVE_KEYS = {"schemaVersion", "requestId", "archiveRef", "selectionRef", "inspection"}
JSON_COMMAND_INPUTS = {
    "generate": {"contextRef", "recipeRef"},
    "sync_project": {"bindingRef"},
    "import_inventory": {"archiveRef", "selectionRef"},
    "set_schedule": {"enabled", "intervalSeconds"},
}
# Staging uses the section 6 fixed Error vocabulary, not arbitrary upload text.
ERROR_RETRIES = {
    "invalid_request": "never", "unsupported_version": "never",
    "unsupported_action": "never", "origin_rejected": "after_refresh",
    "csrf_rejected": "after_refresh", "authority_rejected": "after_refresh",
    "request_not_found": "lookup", "request_conflict": "after_refresh",
    "stale_revision": "after_refresh", "busy": "after_refresh",
    "reconciliation_required": "after_refresh", "configuration_missing": "after_refresh",
    "configuration_invalid": "after_refresh", "input_binding_invalid": "after_refresh",
    "pending_integration": "after_refresh", "not_attached": "after_refresh",
    "observation_unavailable": "after_refresh", "read_only": "after_refresh",
    "mock_only": "after_refresh", "scheduler_decision_required": "after_refresh",
    "admission_unknown": "lookup",
}


def validate_request_metadata(value):
    assert value["schemaVersion"] == VERSION
    assert type(value["requestId"]) is str and HEX.fullmatch(value["requestId"])
    assert type(value["expectedRevision"]) is str and HEX.fullmatch(value["expectedRevision"])


def validate_staging_request(value):
    assert set(value) == STAGING_REQUEST_KEYS
    validate_request_metadata(value)


def validate_json_command(value):
    assert set(value) == STAGING_REQUEST_KEYS | {"action", "input"}
    validate_request_metadata(value)
    assert value["action"] in JSON_COMMAND_INPUTS
    assert set(value["input"]) == JSON_COMMAND_INPUTS[value["action"]]


def validate_staging_admission(value):
    assert set(value) == STAGING_ADMISSION_KEYS
    assert value["schemaVersion"] == VERSION
    assert type(value["requestId"]) is str and HEX.fullmatch(value["requestId"])
    assert value["decision"] in {"accepted", "replayed", "rejected", "indeterminate"}
    archive, error = value["stagedArchive"], value["error"]
    if value["decision"] in {"accepted", "replayed"}:
        assert type(archive) is dict and set(archive) == STAGED_ARCHIVE_KEYS
        assert archive["schemaVersion"] == VERSION
        assert archive["requestId"] == value["requestId"]
        for key in ("archiveRef", "selectionRef"):
            assert type(archive[key]) is str and HEX.fullmatch(archive[key])
        assert archive["inspection"] in {"inspected", "schema_review_required"}
        assert error is None
    else:
        assert archive is None
        assert type(error) is dict and set(error) == {"code", "retry"}
        assert error["code"] in ERROR_RETRIES
        assert error["retry"] == ERROR_RETRIES[error["code"]]
        if value["decision"] == "indeterminate":
            assert error == dict(code="admission_unknown", retry="lookup")
        else:
            assert error["code"] != "admission_unknown"


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_effect(value):
    assert set(value) == {"kind", "state", "changed"}
    assert value["kind"] in {
        "none", "review_artifact", "snapshot_ref", "project_vault",
        "daily_preview", "daily_vault", "private_inventory",
    }
    assert value["state"] in {"none", "verified", "unknown"}
    assert value["changed"] is None or type(value["changed"]) is bool
    if value["state"] != "verified":
        assert value["changed"] is None
    if value["state"] == "none":
        assert value["kind"] == "none"


def validate_workflow(value):
    assert set(value) == {
        "schemaVersion", "id", "label", "adapterMode", "revision", "observedAt",
        "integration", "configuration", "schedule", "execution", "lastAttempt",
        "lastVerifiedOutcome", "lastAttemptAt", "availableActions", "actionReasons",
    }
    assert value["schemaVersion"] == VERSION
    identifier = value["id"]
    assert identifier in IDS
    assert type(value["label"]) is str and 1 <= len(value["label"]) <= 160
    assert HEX.fullmatch(value["revision"])
    assert timestamp(value["observedAt"])
    assert value["integration"] in {"connected", "pending", "not_attached", "unavailable"}
    assert value["adapterMode"] in {"mock", "read_only", "connected"}
    configuration = value["configuration"]
    assert set(configuration) == {"state", "reason"}
    assert configuration["state"] in {"configured", "missing", "invalid", "not_observed"}
    schedule = value["schedule"]
    assert set(schedule) == {"state", "intervalSeconds", "nextDueAt", "reason"}
    assert schedule["state"] in {"enabled", "disabled", "unknown", "not_applicable"}
    interval = schedule["intervalSeconds"]
    assert interval is None or type(interval) is int and interval > 0
    if schedule["state"] == "enabled":
        assert configuration["state"] == "configured" and interval is not None
        assert timestamp(schedule["nextDueAt"]) or (
            schedule["nextDueAt"] is None and schedule["reason"] == "due_unavailable")
    else:
        assert schedule["nextDueAt"] is None
    if identifier == "b5":
        assert schedule == dict(state="not_applicable", intervalSeconds=None,
                                nextDueAt=None, reason="manual_only")
    execution = value["execution"]
    assert set(execution) == {"state", "requestId", "startedAt"}
    assert execution["state"] in {"idle", "queued", "running", "unknown"}
    if execution["state"] in {"queued", "running"}:
        assert HEX.fullmatch(execution["requestId"])
    if execution["state"] == "queued":
        assert execution["startedAt"] is None
    assert execution["startedAt"] is None or timestamp(execution["startedAt"])
    actions = value["availableActions"]
    assert type(actions) is list and len(set(actions)) == len(actions)
    reasons = value["actionReasons"]
    assert set(actions).isdisjoint(reasons)
    assert set(actions) | set(reasons) == ACTIONS[identifier]
    assert all(type(reason) is str and reason for reason in reasons.values())
    if (value["adapterMode"] != "connected" or value["integration"] != "connected"
            or configuration["state"] != "configured"):
        assert not actions
    assert value["lastAttemptAt"] is None or timestamp(value["lastAttemptAt"])
    last = value["lastAttempt"]
    if last is not None:
        assert set(last) == {"requestId", "state", "outcome", "completedAt", "effect"}
        assert HEX.fullmatch(last["requestId"])
        assert last["state"] in {"queued", "running", "completed"}
        validate_effect(last["effect"])
        if last["state"] == "completed":
            assert last["outcome"] in {"success", "failure", "unknown"}
            assert timestamp(last["completedAt"])
        else:
            assert last["outcome"] is None and last["completedAt"] is None
    verified = value["lastVerifiedOutcome"]
    if verified is not None:
        assert set(verified) == {"status", "completedAt", "effect"}
        assert verified["status"] in {"success", "failure"}
        assert timestamp(verified["completedAt"])
        validate_effect(verified["effect"])
        assert verified["effect"]["state"] in {"verified", "none"}


class ContractExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCUMENT.read_text(encoding="utf-8")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate fixture key")
                result[key] = value
            return result

        def reject_constant(_):
            raise ValueError("nonfinite fixture number")

        cls.examples = [json.loads(block, object_pairs_hook=unique,
                                   parse_constant=reject_constant)
                        for block in re.findall(r"```json\n(.*?)\n```", cls.text, re.S)]
        cls.catalog = cls.examples[0]

    def test_all_json_blocks_parse_strictly(self):
        self.assertEqual(len(self.examples), 15)

    def test_five_product_fixture(self):
        self.assertEqual(set(self.catalog), {"schemaVersion", "authority", "observedAt", "workflows"})
        self.assertEqual(self.catalog["schemaVersion"], VERSION)
        self.assertEqual(self.catalog["authority"], "observation_only")
        self.assertEqual([w["id"] for w in self.catalog["workflows"]], list(IDS))
        for workflow in self.catalog["workflows"]:
            validate_workflow(workflow)

    def test_unavailable_never_advertises_actions(self):
        workflow = self.catalog["workflows"][2]
        self.assertEqual(workflow["integration"], "unavailable")
        self.assertEqual(workflow["execution"]["state"], "unknown")
        self.assertFalse(workflow["availableActions"])

    def test_b5_manual_only_even_when_unavailable(self):
        workflow = copy.deepcopy(self.catalog["workflows"][4])
        workflow["integration"] = "unavailable"
        validate_workflow(workflow)
        workflow["schedule"]["state"] = "enabled"
        with self.assertRaises(AssertionError):
            validate_workflow(workflow)

    def test_invalid_example_is_rejected(self):
        with self.assertRaises(AssertionError):
            validate_workflow(self.examples[1])

    def test_mock_cannot_enable_a_real_action(self):
        workflow = copy.deepcopy(self.catalog["workflows"][1])
        workflow["availableActions"] = ["run_once"]
        del workflow["actionReasons"]["run_once"]
        with self.assertRaises(AssertionError):
            validate_workflow(workflow)

    def test_current_running_not_relabelled_historical_success(self):
        workflow = self.catalog["workflows"][3]
        self.assertEqual(workflow["execution"]["state"], "running")
        self.assertIsNone(workflow["lastAttempt"]["outcome"])
        self.assertLess(workflow["lastVerifiedOutcome"]["completedAt"], workflow["lastAttemptAt"])
        self.assertEqual(workflow["lastVerifiedOutcome"]["effect"]["kind"], "daily_preview")
        self.assertIsNone(workflow["execution"]["startedAt"])

    def test_unknown_is_not_verified_outcome(self):
        workflow = copy.deepcopy(self.catalog["workflows"][1])
        workflow["lastVerifiedOutcome"]["status"] = "unknown"
        with self.assertRaises(AssertionError):
            validate_workflow(workflow)

    def test_not_attached_and_missing_configuration_are_nonactionable(self):
        for state in ("not_attached", "connected"):
            workflow = copy.deepcopy(self.catalog["workflows"][1])
            workflow.update(integration=state, adapterMode="connected")
            workflow["configuration"] = dict(state="missing", reason="configuration_missing")
            workflow["actionReasons"] = {action: "configuration_missing" for action in ACTIONS["b2"]}
            validate_workflow(workflow)

    def test_connected_b1_b3_b5_scenarios_need_no_engine_choice(self):
        for index, action, kind in ((0, "generate", "review_artifact"),
                                    (2, "sync_project", "project_vault"),
                                    (4, "import_inventory", "private_inventory")):
            workflow = copy.deepcopy(self.catalog["workflows"][index])
            workflow.update(adapterMode="connected", integration="connected")
            workflow["configuration"] = dict(state="configured", reason=None)
            workflow["execution"] = dict(state="idle", requestId=None, startedAt=None)
            workflow["availableActions"] = [action]
            workflow["actionReasons"] = {
                item: "pending_integration" if item == "stage_archive" else "scheduler_decision_required"
                for item in ACTIONS[workflow["id"]] - {action}
            }
            if index != 4:
                workflow["schedule"] = dict(state="unknown", intervalSeconds=None,
                                            nextDueAt=None, reason="scheduler_decision_required")
            workflow["lastVerifiedOutcome"] = dict(
                status="success", completedAt=1793059000,
                effect=dict(kind=kind, state="verified", changed=None))
            validate_workflow(workflow)

    def test_boolean_interval_and_unknown_enum_rejected(self):
        for field, value in (("intervalSeconds", True), ("state", "__proto__")):
            workflow = copy.deepcopy(self.catalog["workflows"][1])
            workflow["schedule"][field] = value
            with self.assertRaises(AssertionError):
                validate_workflow(workflow)

    def test_typed_b5_admission_and_completed_receipt_correlate(self):
        request, admission, receipt = self.examples[2:5]
        validate_json_command(request)
        self.assertEqual(set(request), {"schemaVersion", "requestId", "expectedRevision", "action", "input"})
        self.assertEqual(request["action"], "import_inventory")
        self.assertEqual(set(request["input"]), {"archiveRef", "selectionRef"})
        self.assertEqual(admission["decision"], "accepted")
        queued = admission["receipt"]
        self.assertEqual(queued["state"], "queued")
        self.assertIsNone(queued["outcome"])
        self.assertEqual(queued["effect"]["state"], "unknown")
        self.assertEqual(request["requestId"], queued["requestId"])
        for key in ("schemaVersion", "workflowId", "requestId", "runId", "action", "createdAt"):
            self.assertEqual(queued[key], receipt[key])
        self.assertEqual(receipt["outcome"], "success")
        validate_effect(receipt["effect"])
        self.assertEqual(receipt["effect"]["kind"], "private_inventory")
        self.assertLessEqual(receipt["createdAt"], receipt["startedAt"])
        self.assertLessEqual(receipt["startedAt"], receipt["completedAt"])

    def test_rejected_admission_is_not_execution(self):
        rejected = self.examples[5]
        self.assertEqual(rejected["decision"], "rejected")
        self.assertIsNone(rejected["receipt"])
        self.assertEqual(rejected["error"], dict(code="stale_revision", retry="after_refresh"))

    def test_existing_payload_examples_keep_legacy_key_sets(self):
        snapshot, daily, run, replay = self.examples[6:10]
        self.assertEqual(set(snapshot), {"enabled", "interval_seconds", "source_name", "destination_name"})
        self.assertEqual(set(daily), {"enabled", "interval_seconds"})
        self.assertEqual(set(run), {"request_id"})
        self.assertTrue(HEX.fullmatch(run["request_id"]))
        self.assertEqual(set(replay), {"version", "outcome", "run_id"})
        self.assertIs(type(replay["version"]), int)

    def test_staging_request_is_not_a_json_command(self):
        request = self.examples[10]
        validate_staging_request(request)
        with self.assertRaises(AssertionError):
            validate_json_command(request)
        # The original contradiction: adding the common action/input pair must
        # make staging invalid, not silently create a second accepted shape.
        command = dict(request, action="stage_archive", input={})
        for validator in (validate_staging_request, validate_json_command):
            with self.assertRaises(AssertionError):
                validator(command)
        for key, value in (("stream", "bytes"), ("path", "/private/archive.zip"),
                           ("url", "https://invalid.example/archive.zip")):
            with self.assertRaises(AssertionError):
                validate_staging_request(dict(request, **{key: value}))
        for key, value in (("schemaVersion", "webui-workflow-control/v9"),
                           ("requestId", "new-id"), ("expectedRevision", True)):
            malformed = dict(request, **{key: value})
            with self.assertRaises(AssertionError):
                validate_staging_request(malformed)

    def test_staging_results_have_four_typed_decisions_and_exact_replay(self):
        request = self.examples[10]
        accepted, replayed, rejected, unknown = self.examples[11:15]
        for result in (accepted, replayed, rejected, unknown):
            validate_staging_admission(result)
            self.assertEqual(result["requestId"], request["requestId"])
        self.assertEqual([result["decision"] for result in self.examples[11:15]],
                         ["accepted", "replayed", "rejected", "indeterminate"])
        self.assertEqual(accepted["stagedArchive"], replayed["stagedArchive"])
        self.assertEqual(rejected["error"], dict(code="request_conflict", retry="after_refresh"))
        self.assertEqual(unknown["error"], dict(code="admission_unknown", retry="lookup"))
        # The archive/selection become inputs to a distinct import intent, never
        # an execution receipt or evidence of private_inventory/Vault/LLM effect.
        import_request = self.examples[2]
        self.assertNotEqual(request["requestId"], import_request["requestId"])
        for key in ("archiveRef", "selectionRef"):
            self.assertEqual(accepted["stagedArchive"][key], import_request["input"][key])

    def test_staging_response_cannot_be_generic_execution_admission(self):
        with self.assertRaises(AssertionError):
            validate_staging_admission(self.examples[3])
        accepted = self.examples[11]
        for key, value in (("receipt", self.examples[4]), ("workflowId", "b5"),
                           ("runId", "f" * 32), ("action", "stage_archive"),
                           ("effect", dict(kind="private_inventory", state="verified", changed=True))):
            with self.assertRaises(AssertionError):
                validate_staging_admission(dict(accepted, **{key: value}))
        for key in STAGING_ADMISSION_KEYS:
            malformed = copy.deepcopy(accepted)
            del malformed[key]
            with self.assertRaises(AssertionError):
                validate_staging_admission(malformed)
        for key in STAGED_ARCHIVE_KEYS:
            malformed = copy.deepcopy(accepted)
            del malformed["stagedArchive"][key]
            with self.assertRaises(AssertionError):
                validate_staging_admission(malformed)
        for key, value in (("archiveRef", "/private/archive.zip"),
                           ("requestId", "0" * 32), ("inspection", "bytes_received"),
                           ("effect", "verified")):
            malformed = copy.deepcopy(accepted)
            malformed["stagedArchive"][key] = value
            with self.assertRaises(AssertionError):
                validate_staging_admission(malformed)

    def test_staging_uncertainty_is_lookup_only_without_success_handles(self):
        unknown = self.examples[14]
        for key, value in (("retry", "after_refresh"), ("code", "execution_failed"),
                           ("message", "private exception")):
            malformed = copy.deepcopy(unknown)
            malformed["error"][key] = value
            with self.assertRaises(AssertionError):
                validate_staging_admission(malformed)
        for decision in ("rejected", "indeterminate"):
            malformed = copy.deepcopy(unknown)
            malformed["decision"] = decision
            malformed["stagedArchive"] = self.examples[11]["stagedArchive"]
            with self.assertRaises(AssertionError):
                validate_staging_admission(malformed)
        for archive, error in ((None, None), (self.examples[11]["stagedArchive"], unknown["error"])):
            with self.assertRaises(AssertionError):
                validate_staging_admission(dict(self.examples[11], stagedArchive=archive, error=error))
        with self.assertRaises(AssertionError):
            validate_staging_admission(dict(unknown, decision="rejected"))

    def test_staging_design_signatures_and_normative_exact_fields(self):
        blocks = re.findall(r"```python\n(.*?)\n```", self.text, re.S)
        self.assertEqual(len(blocks), 1)
        tree = ast.parse(blocks[0])
        ports = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        staging = {node.name: node for node in ports["PrivateArchiveStagingPort"].body
                   if isinstance(node, ast.FunctionDef)}
        self.assertEqual(set(staging), {"stage_archive", "lookup_staging"})
        stage, lookup = staging["stage_archive"], staging["lookup_staging"]
        self.assertEqual([arg.arg for arg in stage.args.args], ["self", "request", "stream"])
        self.assertEqual([ast.unparse(arg.annotation) for arg in stage.args.args[1:]],
                         ["StageArchiveRequest", "BoundedBinaryStream"])
        self.assertEqual([arg.arg for arg in lookup.args.args], ["self", "request_id"])
        self.assertEqual(ast.unparse(lookup.args.args[1].annotation), "RequestId")
        for method in staging.values():
            self.assertEqual(ast.unparse(method.returns), "StagingAdmission")
        import_method = next(node for node in ports["InventoryImportPort"].body
                             if isinstance(node, ast.FunctionDef))
        self.assertEqual(ast.unparse(import_method.returns), "Admission")
        command_table = self.text.split("| Type / route |", 1)[1].split("Schedule policy bounds", 1)[0]
        self.assertEqual(set(re.findall(r"\| `([a-z_]+)` \|", command_table)),
                         set(JSON_COMMAND_INPUTS))
        for name, keys in (("StageArchiveRequest", STAGING_REQUEST_KEYS),
                           ("StagingAdmission", STAGING_ADMISSION_KEYS),
                           ("StagedArchive", STAGED_ARCHIVE_KEYS)):
            declarations = re.findall(rf"`{name} = \{{([^}}]+)\}}`", self.text)
            self.assertEqual(len(declarations), 1)
            self.assertEqual(set(declarations[0].split(",")), keys)
        self.assertIn("All new **JSON command** requests", self.text)
        self.assertIn("streaming-port exception", self.text)
        self.assertIn("Staging MUST NOT return this generic Admission", self.text)

    def test_staging_is_pending_manual_only_without_fabricated_live_route(self):
        workflow = self.catalog["workflows"][4]
        validate_workflow(workflow)
        self.assertEqual(ACTIONS["b5"], {"stage_archive", "import_inventory"})
        self.assertNotIn("stage_archive", workflow["availableActions"])
        self.assertEqual(workflow["actionReasons"]["stage_archive"], "pending_integration")
        self.assertEqual(workflow["schedule"], dict(state="not_applicable", intervalSeconds=None,
                                                  nextDueAt=None, reason="manual_only"))
        # The route proposal table must not quietly choose an upload transport.
        routes = self.text.split("Proposed future routes", 1)[1].split("B2/B4 commands", 1)[0]
        b5_posts = re.findall(r"\| POST `([^`]*b5[^`]*)`", routes)
        self.assertEqual(b5_posts, ["/api/v1/workflow-control/b5/imports"])
        source = (ROOT / "src/obsidian_automation/admin_dashboard.py").read_text(encoding="utf-8")
        strings = {node.value for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.Constant) and type(node.value) is str}
        # A read-only v1 Catalog GET may now be registered; the upload and
        # import/staging POST transports still have no live endpoint.
        self.assertIn("/api/v1/workflow-control", strings)
        self.assertFalse(any("/api/" in value
                             and (re.search(r"/(?:b5|upload|archives?)(?:/|$)", value)
                                  or ("workflow-control" in value
                                      and value != "/api/v1/workflow-control"))
                             for value in strings))
        for requirement in ("not selected or accepted by this correction", "lookup-only",
                            "Lookup/GET never repairs, publishes, initializes",
                            "No live upload port", "verified, durable completed private archive",
                            "Same ID with changed", "unavailable/corrupt private",
                            "Malformed/extra/duplicate metadata"):
            self.assertIn(requirement, self.text)

    def test_legacy_routes_still_present_in_source_ast(self):
        source = ROOT / "src/obsidian_automation/admin_dashboard.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        strings = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and type(node.value) is str}
        for product in ("snapshot", "daily"):
            for suffix in ("", "/settings", "/runs", "/runs/", "/connections"):
                self.assertIn(f"/api/v1/{product}{suffix}", strings)
        self.assertIn("/api/v1/workflows", strings)
        # Registration is GET/HEAD only. The existing write allowlist must
        # never grant the new observation endpoint command authority.
        self.assertIn("/api/v1/workflow-control", strings)
        post = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        post_strings = {node.value for node in ast.walk(post)
                        if isinstance(node, ast.Constant) and type(node.value) is str}
        self.assertFalse(any("workflow-control" in value for value in post_strings))

    def test_legacy_interval_bounds_match_inspected_ast(self):
        for filename, lower in (("snapshot_job.py", 60), ("github_daily_job.py", 3600)):
            tree = ast.parse((ROOT / "src/obsidian_automation" / filename).read_text(encoding="utf-8"))
            bounds = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and len(node.ops) == 2:
                    if isinstance(node.left, ast.Constant) and isinstance(node.comparators[-1], ast.Constant):
                        bounds.append((node.left.value, node.comparators[-1].value))
            self.assertIn((lower, 86400), bounds)


if __name__ == "__main__":
    unittest.main()
