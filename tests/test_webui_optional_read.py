"""Synthetic optional read binding; no real provider, Writer, journal or Vault."""
from dataclasses import replace
import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from obsidian_automation import webui_b5_ports as b5
from obsidian_automation import webui_optional_read as optional
from obsidian_automation.webui_b1_adapter import (
    Attempt, B1FakeAdapter, B1ReadAdapter, LedgerState, PreparedBinding, VERSION,
)
from obsidian_automation.webui_b3_adapter import (
    B3FakeAdapter, Binding, SyntheticLedger, SyntheticWriter,
)
from obsidian_automation.webui_control_get import read_control_catalog


class Trap:
    def __getattribute__(self, name):
        raise AssertionError("opaque object hook accessed")


class DictTrap(dict):
    def __iter__(self):
        raise AssertionError("dict subclass iterated")

    def items(self):
        raise AssertionError("dict subclass copied")


class StringTrap(str):
    def __eq__(self, other):
        raise AssertionError("key subclass compared")

    __hash__ = str.__hash__


class MemoryLedger:
    """Already provisioned in-memory B1 state, not a production store."""
    def __init__(self):
        self.state = LedgerState("4" * 32, PreparedBinding("1" * 32, "2" * 32, "3" * 32))
        self.reads = 0
        self.transact = Mock(side_effect=AssertionError("transaction forbidden"))

    def read(self):
        self.reads += 1
        return self.state


def absent_ports():
    return {"b1": B1ReadAdapter(None, clock=lambda: 10),
            "b3": B3FakeAdapter(None, read_only=True, observed_at=10),
            "b5": b5.ReadPort(None, observed_at=10)}


def catalog(ports, at=20):
    return read_control_catalog(
        snapshot_job=None, daily_job=None, observed_at=at,
        optional_snapshots=optional.materialize_optional_read_ports(ports))


def test_exact_approved_types_accepted_and_map_copied_without_observe_lookup(monkeypatch):
    ports = absent_ports()
    for port in ports.values():
        monkeypatch.setattr(type(port), "observe", property(
            lambda _self: pytest.fail("constructor looked up observe")))
    validated = optional.validate_optional_read_ports(ports)
    assert validated == ports and validated is not ports
    ports.clear()
    assert set(validated) == {"b1", "b3", "b5"}
    assert optional.validate_optional_read_ports({}) == {}


@pytest.mark.parametrize("case", ["none", "bool", "list", "tuple", "opaque", "subdict",
                                 "subkey", "intkey", "b2", "b4", "other", "b1", "b3", "b5"])
def test_invalid_maps_and_objects_rejected_without_hooks(case):
    # Construct hostile objects here, not in pytest's parameter-ID machinery.
    bad = {"none": None, "bool": False, "list": [], "tuple": (), "opaque": Trap(),
           "subdict": DictTrap(b1=Trap()), "subkey": {StringTrap("b1"): Trap()},
           "intkey": {1: Trap()}}.get(case, {case: Trap()})
    with pytest.raises(TypeError, match="^invalid_workflow_control_read_ports$"):
        optional.validate_optional_read_ports(bad)


def test_rejects_subclasses_fake_manual_ports_and_wrong_slot_before_any_lookup(monkeypatch):
    ports = absent_ports()
    for key, port in ports.items():
        subclass = type("Unapproved", (type(port),), {
            "observe": property(lambda _self: pytest.fail("subclass observe looked up"))})
        with pytest.raises(TypeError):
            optional.validate_optional_read_ports({key: object.__new__(subclass)})
    for bad in (
        {"b1": B1FakeAdapter(None, provider=None, clock=lambda: 10)},
        {"b5": object.__new__(b5.TestOnlyMockPort)},
        {"b1": ports["b3"]}, {"b3": ports["b5"]}, {"b5": ports["b1"]},
        {"b1": ports["b1"], "b3": Trap()},
    ):
        monkeypatch.setattr(B1ReadAdapter, "observe", property(
            lambda _self: pytest.fail("partial map observed before full validation")))
        with pytest.raises(TypeError):
            optional.materialize_optional_read_ports(bad)


def test_materializes_once_in_fixed_order_and_omits_unattached_ports(monkeypatch):
    ports = absent_ports()
    calls = []
    for key, port in ports.items():
        original = port.observe

        def observe(key=key, original=original):
            calls.append(key)
            return original()

        monkeypatch.setattr(port, "observe", observe)
    snapshots = optional.materialize_optional_read_ports(dict(reversed(list(ports.items()))))
    assert calls == ["b1", "b3", "b5"]
    assert all(type(value) is dict for value in snapshots.values())
    calls.clear()
    assert set(optional.materialize_optional_read_ports({"b5": ports["b5"]})) == {"b5"}
    assert calls == ["b5"]
    assert optional.materialize_optional_read_ports({}) == {}
    assert [w["integration"] for w in catalog({})["workflows"]] == [
        "pending", "not_attached", "pending", "not_attached", "pending"]


def test_absent_sources_are_not_bootstrapped_or_promoted():
    result = catalog(absent_ports())["workflows"]
    assert [w["integration"] for w in result] == [
        "not_attached", "not_attached", "not_attached", "not_attached", "pending"]
    assert all(w["availableActions"] == [] and w["lastVerifiedOutcome"] is None for w in result)
    assert result[4]["schedule"] == {
        "state": "not_applicable", "intervalSeconds": None,
        "nextDueAt": None, "reason": "manual_only"}


def test_attached_synthetic_sources_stay_read_only_or_visibly_mock_without_effects(monkeypatch):
    ledger = MemoryLedger()
    binding = Binding("5" * 32, "6" * 32, "PRIVATE_PROJECT", "PRIVATE_VAULT_PATH")
    writer = SyntheticWriter(binding)
    synthetic = SyntheticLedger.fresh_fixture(binding, writer)
    journal = b5.TestOnlyPrivateJournal("7" * 32, "8" * 32, "9" * 32)
    ports = {"b1": B1ReadAdapter(ledger, clock=lambda: 10),
             "b3": B3FakeAdapter(synthetic, manual_simulation=True, observed_at=10),
             "b5": b5.ReadPort(journal, observed_at=10)}
    forbidden = Mock(side_effect=AssertionError("effect/lookup forbidden"))
    for port in ports.values():
        for name in ("lookup", "generate", "sync_project", "set_schedule", "stage_archive",
                     "import_inventory", "submit", "tick", "transact", "lookup_staging",
                     "detail", "inventory_detail"):
            monkeypatch.setattr(port, name, forbidden, raising=False)
    monkeypatch.setattr(writer, "_apply", forbidden)
    for _ in range(2):
        result = catalog(ports)["workflows"]
        assert [result[i]["adapterMode"] for i in (0, 2, 4)] == ["read_only", "mock", "mock"]
        assert [result[i]["integration"] for i in (0, 2, 4)] == ["pending"] * 3
        assert "mock" in result[2]["label"] and "mock" in result[4]["label"]
        assert all(w["availableActions"] == [] and w["lastVerifiedOutcome"] is None for w in result)
        assert result[4]["schedule"]["reason"] == "manual_only"
        assert "PRIVATE_" not in json.dumps(result)
    forbidden.assert_not_called()
    ledger.transact.assert_not_called()
    assert ledger.reads == 2 and writer.calls == 0
    assert synthetic._records == {} and ports["b5"]._journal is journal


@pytest.mark.parametrize("case", ["none", "bool", "list", "string", "opaque", "subdict"])
def test_wrong_return_type_becomes_unavailable_and_other_slots_survive(monkeypatch, case):
    bad = {"none": None, "bool": False, "list": [], "string": "PRIVATE_TOKEN",
           "opaque": Trap(), "subdict": DictTrap()}[case]
    ports = absent_ports()
    monkeypatch.setattr(ports["b1"], "observe", lambda: bad)
    result = catalog(ports)["workflows"]
    assert result[0]["integration"] == "unavailable"
    assert result[0]["configuration"]["reason"] == "observation_unavailable"
    assert result[0]["execution"]["state"] == "unknown"
    assert result[2]["integration"] == "not_attached"
    assert result[4]["integration"] == "pending"
    assert "PRIVATE_TOKEN" not in json.dumps(result)


@pytest.mark.parametrize("slot", ["b1", "b3", "b5"])
def test_exception_isolated_and_redacted_including_b5_manual_schedule(monkeypatch, slot):
    ports = absent_ports()

    def fail():
        raise RuntimeError("PRIVATE_TOKEN /private/path https://user:password@invalid")

    monkeypatch.setattr(ports[slot], "observe", fail)
    result = catalog(ports)["workflows"]
    assert result[int(slot[1]) - 1]["integration"] == "unavailable"
    assert sum(w["integration"] == "unavailable" for w in result) == 1
    assert result[4]["schedule"]["reason"] == "manual_only"
    assert "PRIVATE_TOKEN" not in json.dumps(result) and "password" not in json.dumps(result)


@pytest.mark.parametrize("damage", ["extra", "connected", "actions", "version", "future", "nested"])
def test_existing_composer_validates_dicts_without_private_data_or_authority(monkeypatch, damage):
    ports = absent_ports()
    value = ports["b5"].observe()
    if damage == "extra":
        value["secret"] = "PRIVATE_TOKEN"
    elif damage == "connected":
        value["integration"] = "connected"
    elif damage == "actions":
        value["availableActions"] = ["import_inventory"]
    elif damage == "version":
        value["schemaVersion"] = "PRIVATE_TOKEN"
    elif damage == "future":
        value["observedAt"] = 21
    else:
        value["configuration"] = DictTrap(state="configured", reason=None)
    original = dict(value)
    monkeypatch.setattr(ports["b5"], "observe", lambda: value)
    result = catalog(ports)["workflows"]
    assert result[4]["integration"] == "unavailable"
    assert result[0]["integration"] == result[2]["integration"] == "not_attached"
    assert "PRIVATE_TOKEN" not in json.dumps(result)
    assert value == original


def test_read_only_verified_b1_history_remains_fail_closed_not_promoted():
    ledger = MemoryLedger()
    receipt = {"schemaVersion": VERSION, "workflowId": "b1", "requestId": "a" * 32,
               "runId": "b" * 32, "action": "generate", "state": "completed",
               "outcome": "success", "createdAt": 1, "startedAt": 2, "completedAt": 3,
               "effect": {"kind": "review_artifact", "state": "verified", "changed": None},
               "error": None}
    ledger.state = replace(ledger.state, attempts=(Attempt(
        "1" * 32, "2" * 32, "3" * 32, "4" * 32, receipt),))
    port = B1ReadAdapter(ledger, clock=lambda: 10)
    raw = port.observe()
    assert raw["integration"] == "pending"
    assert raw["lastVerifiedOutcome"]["effect"]["state"] == "verified"
    result = catalog({"b1": port})["workflows"][0]
    assert result["integration"] == "unavailable"
    assert result["lastAttempt"] is result["lastVerifiedOutcome"] is None
    assert result["availableActions"] == []
    ledger.transact.assert_not_called()


def test_mock_verified_history_is_suppressed_not_vault_success(monkeypatch):
    binding = Binding("5" * 32, "6" * 32)
    port = B3FakeAdapter(SyntheticLedger.fresh_fixture(binding), observed_at=10)
    value = port.observe()
    effect = {"kind": "project_vault", "state": "verified", "changed": True}
    value.update(lastAttempt={"requestId": "a" * 32, "state": "completed", "outcome": "success",
                              "completedAt": 3, "effect": effect}, lastAttemptAt=1,
                 lastVerifiedOutcome={"status": "success", "completedAt": 3, "effect": effect})
    monkeypatch.setattr(port, "observe", lambda: value)
    result = catalog({"b3": port})["workflows"][2]
    assert result["adapterMode"] == "mock" and "mock" in result["label"]
    assert result["integration"] == "pending" and result["availableActions"] == []
    assert result["lastAttempt"] is result["lastAttemptAt"] is result["lastVerifiedOutcome"] is None


def test_module_dependencies_and_calls_are_read_only_source_only():
    tree = ast.parse(Path(optional.__file__).read_text(encoding="utf-8"))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports == {"__future__", "webui_b1_adapter", "webui_b3_adapter", "webui_b5_ports"}
    assert not any(isinstance(n, ast.Import) for n in ast.walk(tree))
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert {n.attr for n in calls if isinstance(n, ast.Attribute)} == {"observe"}
    assert {n.id for n in calls if isinstance(n, ast.Name)} <= {
        "type", "all", "dict", "TypeError", "validate_optional_read_ports"}
