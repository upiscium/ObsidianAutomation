from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import unquote, urlsplit

import pytest

from obsidian_automation import core_live_reconciliation as live
from obsidian_automation import managed_promotion_deployment as managed
from obsidian_automation import promotion_deployment as deployment
from obsidian_automation.core_promotion_transport import HTTPResponse, load_checkpoint
from obsidian_automation.promotion_bootstrap import bootstrap_production_checkpoint

REL = "98-System/00-command/rename_entity.md"
DESIRED = b"<%* await tp.user.rename_entity(tp); %>\n"
SECRET = "TEST-PRIVATE-PASSWORD-AND-BODY-CANARY"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


class DAV:
    def __init__(self, files):
        self.files = dict(files)
        self.etags = {path: '"v1"' for path in self.files}
        self.requests = []
        self.before_put = None
        self.fail_put = None

    def __call__(self, **kwargs):
        path = unquote(urlsplit(kwargs["target_url"]).path).removeprefix("/dav/Vault/")
        method = kwargs["method"]
        headers = kwargs["headers"]
        self.requests.append((method, path, dict(headers)))
        if method == "GET":
            if path not in self.files:
                return HTTPResponse(404, b"", None)
            return HTTPResponse(200, self.files[path], self.etags[path])
        assert method == "PUT", "Reconciliation must never DELETE or MOVE"
        if self.before_put:
            self.before_put(path)
        if self.fail_put == path:
            raise OSError(SECRET)
        if headers.get("If-None-Match") == "*":
            if path in self.files:
                return HTTPResponse(412, b"", None)
        elif headers.get("If-Match") != self.etags.get(path):
            return HTTPResponse(412, b"", None)
        else:
            assert path in self.files
        self.files[path] = kwargs["body"]
        self.etags[path] = '"v2"'
        return HTTPResponse(204, b"", '"v2"')

    @property
    def puts(self):
        return [row for row in self.requests if row[0] == "PUT"]


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "core"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    path = repo / REL
    path.parent.mkdir(parents=True)
    path.write_bytes(DESIRED)
    (repo / "README.md").write_text("repository-owned\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Sync public projection from ObsidianVault")
    head = git(repo, "rev-parse", "HEAD")
    config = tmp_path / "policy.toml"
    config.write_text('version=1\nstrict_missing=true\ninclude=["98-System/**", ".obsidian/appearance.json"]\nexclude=[]\nrepository_owned=["README.md"]\n')
    dav = DAV({REL: DESIRED})
    common = dict(core_repository=repo, config_path=config,
                  base_url="https://example.invalid/dav/Vault", username="test", password=SECRET,
                  transport=dav)
    return common, head, dav, tmp_path


def audit(setup):
    common, head, _dav, _tmp = setup
    return live.audit_live(**common, core_commit=head)


def repair(setup, plan, **overrides):
    common, head, _dav, tmp = setup
    data = live._json(plan)
    kwargs = dict(**common, plan_data=data, expected_sha256=hashlib.sha256(data).hexdigest(),
                  current_core_commit=head, receipt_directory=tmp / "receipts")
    kwargs.update(overrides)
    return live.repair_live(**kwargs)


def test_read_only_convergence_audits_only_tracked_allowlist(setup):
    _common, _head, dav, _tmp = setup
    dav.files["11-Knowledge/private.md"] = SECRET.encode()
    dav.files["98-System/local-extra.md"] = b"legitimate unpublished Live work"
    plan = audit(setup)
    assert plan["observations"] == []
    assert plan["audited_files"] == 1
    assert plan["remote_only_paths"] == "not_enumerated_or_deleted"
    assert dav.requests == [("GET", REL, {"Accept": "application/octet-stream"})]
    assert SECRET not in live._json(plan).decode()


def test_missing_repair_is_conditional_create_and_same_plan_idempotent(setup):
    _common, _head, dav, _tmp = setup
    del dav.files[REL]
    plan = audit(setup)
    assert len(plan["observations"]) == 1
    assert plan["observations"][0]["observed_sha256"] is None
    assert not dav.puts
    receipt, path = repair(setup, plan)
    assert receipt["status"] == "completed"
    assert receipt["checkpoint_advanced"] is False
    assert path.stat().st_mode & 0o777 == 0o600
    assert dav.files[REL] == DESIRED
    assert dav.puts[0][2]["If-None-Match"] == "*"
    again, _ = repair(setup, plan)
    assert again["outcomes"] == [{"path": REL, "result": "already_desired"}]
    assert len(dav.puts) == 1
    assert audit(setup)["observations"] == []


def test_modified_is_detected_without_overwrite_then_etag_guarded(setup):
    _common, _head, dav, _tmp = setup
    dav.files[REL] = SECRET.encode()
    plan = audit(setup)
    assert len(plan["observations"]) == 1
    assert not dav.puts
    assert SECRET not in live._json(plan).decode()
    receipt, _ = repair(setup, plan)
    assert receipt["status"] == "completed"
    assert dav.puts[0][2]["If-Match"] == '"v1"'
    assert dav.files[REL] == DESIRED


@pytest.mark.parametrize("change", ["body", "etag", "missing_appeared"])
def test_stale_approval_is_rejected_before_any_put(setup, change):
    _common, _head, dav, _tmp = setup
    dav.files[REL] = b"old observed body"
    if change == "missing_appeared":
        del dav.files[REL]
    plan = audit(setup)
    if change == "etag":
        dav.etags[REL] = '"newer"'
    else:
        dav.files[REL] = b"new human edit"
        dav.etags[REL] = '"newer"'
    with pytest.raises(live.ReconciliationError):
        repair(setup, plan)
    assert not dav.puts


@pytest.mark.parametrize("etag", [None, 'W/"weak"', '"bad\r\nInjected: yes"'])
def test_missing_or_unsafe_etag_never_allows_update(setup, etag):
    _common, _head, dav, _tmp = setup
    dav.files[REL] = b"modified"
    dav.etags[REL] = etag
    plan = audit(setup)
    with pytest.raises(live.ReconciliationError, match="strong_etag"):
        repair(setup, plan)
    assert not dav.puts


def test_race_after_preflight_is_cas_conflict_and_receipted(setup):
    _common, _head, dav, tmp = setup
    dav.files[REL] = b"modified"
    plan = audit(setup)
    def race(path):
        dav.files[path] = b"human won"
        dav.etags[path] = '"race"'
    dav.before_put = race
    with pytest.raises(live.ReconciliationError, match="cas_conflict"):
        repair(setup, plan)
    assert dav.files[REL] == b"human won"
    receipts = [json.loads(p.read_bytes()) for p in (tmp / "receipts").glob("*.json")]
    assert any(r["status"] == "failed_or_partial" and r["outcomes"][-1]["result"] == "cas_conflict" for r in receipts)


def test_all_path_preflight_prevents_partial_mutation_on_known_conflict(setup):
    common, _head, dav, _tmp = setup
    second = "98-System/zz-second.md"
    (common["core_repository"] / second).write_bytes(b"second")
    git(common["core_repository"], "add", "-A")
    git(common["core_repository"], "commit", "-m", "second")
    head = git(common["core_repository"], "rev-parse", "HEAD")
    setup = (common, head, dav, _tmp)
    dav.files[REL] = b"first old"
    dav.files[second], dav.etags[second] = b"second old", '"v1"'
    plan = audit(setup)
    dav.files[second] = b"new human edit"
    with pytest.raises(live.ReconciliationError, match="live_changed"):
        repair(setup, plan)
    assert not dav.puts


def test_partial_network_failure_is_not_rolled_back_and_retry_resumes(setup):
    common, _head, dav, tmp = setup
    second = "98-System/zz-second.md"
    (common["core_repository"] / second).write_bytes(b"second")
    git(common["core_repository"], "add", "-A")
    git(common["core_repository"], "commit", "-m", "second")
    head = git(common["core_repository"], "rev-parse", "HEAD")
    setup = (common, head, dav, tmp)
    dav.files.clear()
    plan = audit(setup)
    dav.fail_put = second
    with pytest.raises(live.ReconciliationError, match="webdav_request_failed") as error:
        repair(setup, plan)
    assert SECRET not in str(error.value)
    assert dav.files[REL] == DESIRED
    receipts = [json.loads(p.read_bytes()) for p in (tmp / "receipts").glob("*.json")]
    assert any(r["status"] == "failed_or_partial" and r["outcomes"][-1]["result"] == "ambiguous_requires_recheck" for r in receipts)
    assert SECRET not in "".join(p.read_text() for p in (tmp / "receipts").glob("*.json"))
    dav.fail_put = None
    receipt, _ = repair(setup, plan)
    assert receipt["status"] == "completed"
    assert receipt["outcomes"][0]["result"] == "already_desired"
    assert dav.files[second] == b"second"


@pytest.mark.parametrize("field,value", [("current_core_commit", "f" * 40), ("username", "other"),
                                        ("base_url", "https://other.invalid/dav/Vault")])
def test_wrong_head_or_remote_binding_refused_before_io(setup, field, value):
    _common, _head, dav, _tmp = setup
    del dav.files[REL]
    plan = audit(setup)
    dav.requests.clear()
    with pytest.raises(live.ReconciliationError):
        repair(setup, plan, **{field: value})
    assert dav.requests == []


def test_changed_policy_refused_before_io(setup):
    common, _head, dav, _tmp = setup
    del dav.files[REL]
    plan = audit(setup)
    common["config_path"].write_text(common["config_path"].read_text() + "# changed\n")
    dav.requests.clear()
    with pytest.raises(live.ReconciliationError, match="policy_changed"):
        repair(setup, plan)
    assert not dav.requests


@pytest.mark.parametrize("mutation", ["traversal", "duplicate", "desired_hash", "unmanaged", "extra_key"])
def test_malformed_or_unbound_plan_cannot_widen_authority(setup, mutation):
    _common, _head, dav, _tmp = setup
    del dav.files[REL]
    plan = copy.deepcopy(audit(setup))
    if mutation == "traversal":
        plan["observations"][0]["path"] = "../secret"
    elif mutation == "duplicate":
        plan["observations"] *= 2
    elif mutation == "desired_hash":
        plan["observations"][0]["desired_sha256"] = "f" * 64
    elif mutation == "unmanaged":
        plan["observations"][0]["path"] = "11-Knowledge/private.md"
    else:
        plan["observations"][0]["body"] = "unapproved bytes"
    with pytest.raises(live.ReconciliationError):
        repair(setup, plan)
    assert not dav.puts


def test_digest_and_duplicate_json_keys_rejected(setup):
    plan = audit(setup)
    data = live._json(plan)
    with pytest.raises(live.ReconciliationError, match="digest"):
        live.parse_plan(data, "0" * 64)
    duplicate = data.replace(b'"record_version":1', b'"record_version":1,"record_version":1')
    with pytest.raises(live.ReconciliationError):
        live.parse_plan(duplicate, hashlib.sha256(duplicate).hexdigest())


def test_appearance_ignores_unmanaged_state_and_repair_preserves_it(setup):
    common, _head, dav, tmp = setup
    path = common["core_repository"] / live.APPEARANCE_PATH
    path.parent.mkdir()
    core = {"theme": "obsidian", "cssTheme": "", "enabledCssSnippets": ["obsidian-core"]}
    path.write_text(json.dumps(core))
    git(common["core_repository"], "add", "-A")
    git(common["core_repository"], "commit", "-m", "appearance")
    head = git(common["core_repository"], "rev-parse", "HEAD")
    setup = common, head, dav, tmp
    remote = {**core, "private_setting": SECRET, "enabledCssSnippets": ["personal", "obsidian-core"]}
    dav.files[live.APPEARANCE_PATH] = json.dumps(remote, indent=4).encode()
    dav.etags[live.APPEARANCE_PATH] = '"a1"'
    assert audit(setup)["observations"] == []
    remote["theme"] = "moonstone"
    dav.files[live.APPEARANCE_PATH] = json.dumps(remote).encode()
    plan = audit(setup)
    assert len(plan["observations"]) == 1
    assert SECRET not in live._json(plan).decode()
    repair(setup, plan)
    merged = json.loads(dav.files[live.APPEARANCE_PATH])
    assert merged["theme"] == "obsidian"
    assert merged["private_setting"] == SECRET
    assert "personal" in merged["enabledCssSnippets"]
    assert audit(setup)["observations"] == []


@pytest.mark.parametrize("status", [401, 403, 500, 502])
def test_remote_failure_never_looks_like_missing_file(setup, status):
    common, head, dav, _tmp = setup
    kwargs = {**common, "transport": lambda **kw: HTTPResponse(status, SECRET.encode(), None)}
    with pytest.raises(live.ReconciliationError) as error:
        live.audit_live(**kwargs, core_commit=head)
    assert SECRET not in str(error.value)
    assert not dav.puts


def test_size_and_count_bounds(setup, monkeypatch):
    common, head, dav, _tmp = setup
    monkeypatch.setattr(live, "MAX_FILES", 0)
    with pytest.raises(live.ReconciliationError, match="count_limit"):
        audit(setup)
    assert not dav.requests
    monkeypatch.setattr(live, "MAX_FILES", 4096)
    monkeypatch.setattr(live, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(live.ReconciliationError, match="total_size"):
        audit(setup)


def test_immutable_plan_storage_and_symlink_refusal(setup):
    _common, _head, _dav, tmp = setup
    plan = audit(setup)
    digest, path = live.store_artifact(tmp / "plans", plan, "live-drift-plan")
    assert path.stat().st_mode & 0o777 == 0o600
    assert live.store_artifact(tmp / "plans", plan, "live-drift-plan") == (digest, path)
    path.unlink()
    victim = tmp / "victim"
    victim.write_bytes(b"keep")
    path.symlink_to(victim)
    with pytest.raises(live.ReconciliationError):
        live.store_artifact(tmp / "plans", plan, "live-drift-plan")
    assert victim.read_bytes() == b"keep"


def test_production_unchanged_core_detects_live_drift_without_checkpoint_advance(setup):
    common, head, dav, tmp = setup
    remote = tmp / "core.git"
    subprocess.run(["git", "clone", "--bare", str(common["core_repository"]), str(remote)], check=True, capture_output=True)
    state = tmp / "state"
    git_runner = lambda args, cwd: deployment._run_git(args, cwd)
    _base, _fetched, checkpoint = bootstrap_production_checkpoint(
        state_root=state, config_path=common["config_path"], core_commit=head,
        repository_url=str(remote), git_runner=git_runner,
    )
    password = tmp / "password"
    password.write_text(SECRET + "\n")
    kwargs = dict(state_root=state, config_path=common["config_path"],
                  base_url=common["base_url"], username=common["username"], password_file=password,
                  repository_url=str(remote), git_runner=git_runner, http_transport=dav)
    before = checkpoint.read_bytes()
    result = managed.run_promotion_cycle(**kwargs)
    assert result.result == "up_to_date"
    assert dav.requests and not dav.puts
    del dav.files[REL]
    result = managed.run_promotion_cycle(**kwargs)
    assert result.result == "live_drift"
    assert result.plan_path.is_file()
    assert not dav.puts
    assert checkpoint.read_bytes() == before
    data = result.plan_path.read_bytes()
    receipt, _ = live.repair_live(
        plan_data=data, expected_sha256=result.plan_sha256, core_repository=state / "ObsidianCore",
        current_core_commit=head, config_path=common["config_path"], receipt_directory=state / "drift/receipts",
        base_url=common["base_url"], username=common["username"], password=SECRET, transport=dav,
    )
    assert receipt["status"] == "completed"
    assert checkpoint.read_bytes() == before
    assert managed.run_promotion_cycle(**kwargs).result == "up_to_date"
    assert load_checkpoint(checkpoint).last_observed_core_commit == head


def test_production_cli_drift_is_nonzero_not_false_success(monkeypatch, tmp_path, capsys):
    result = deployment.PromotionCycleResult("live_drift", "a" * 40, "a" * 40, "b" * 64,
                                               tmp_path / "plan.json", None, None)
    monkeypatch.setattr(managed, "run_promotion_cycle", lambda **kw: result)
    assert managed.main([]) == 3
    value = json.loads(capsys.readouterr().out)
    assert value["result"] == "live_drift"
    assert value["audit_scope"] == "core_tracked_managed_paths"
