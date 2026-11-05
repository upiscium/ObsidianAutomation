"""R2/B1: isolated effect-time binding, fake Nextcloud only.

This test never touches real credentials or a service. Production still uses
FormatterJob(writer=None). Verifier callbacks stand in for separately governed
durable approval authorities, NOT a production grant.
"""
from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.vault_formatter import create_plan
from obsidian_automation.vault_format_writer import FormatWriteError, _binding
from obsidian_automation.vault_format_canary_binding import _generation
from obsidian_automation.vault_format_recurring_admission import RecurringPolicy, _policy_digest
from obsidian_automation.vault_format_recurring_binding import RecurringEffectWriter


ROOT = "https://dav.invalid/remote.php/dav/files/user/"
BASE = ROOT + "ObsidianVault"
BODY = b"# Test\nBefore\n  \nAfter\n"


class Shared:
    def __init__(self):
        self._lock = threading.RLock()
        self.details = {
            "webdav_url": ROOT, "vault_folder": "ObsidianVault",
            "nextcloud_user": "user", "nextcloud_password": "SYNTHETIC-ONLY",
        }
    def load_nextcloud(self):
        with self._lock:
            return dict(self.details)


class DAV:
    def __init__(self, contents):
        self.files = dict(contents)
        self.etags = {path: '"v0"' for path in contents}
        self.calls = []
        self.on_call = None
    def __call__(self, *, method, target_url, username, password, headers, body,
                 timeout, response_limit):
        assert username == "user" and password == "SYNTHETIC-ONLY"
        assert target_url.startswith(BASE + "/98-System/")
        path = target_url.split("/ObsidianVault/", 1)[1]
        self.calls.append((method, path))
        if self.on_call:
            self.on_call(method, path)
        if method == "GET":
            return HTTPResponse(200, self.files[path], self.etags[path])
        assert method == "PUT" and headers["If-Match"] == self.etags[path]
        self.files[path] = body
        self.etags[path] = '"v1"'
        return HTTPResponse(204, b"", '"v1"')


def fixture(tmp_path, count=2):
    vault = tmp_path / "vault"
    root = vault / "98-System"
    root.mkdir(parents=True)
    for i in range(count):
        (root / f"note{i}.md").write_bytes(BODY)
    plan = create_plan(vault)
    shared = Shared()
    generation = _generation(shared.details)
    policy = RecurringPolicy(
        source_generation=generation,
        account_binding_sha256=_binding(BASE, "user"),
        provider_proof_sha256="a" * 64,
        max_targets=3, max_selected_bytes=65536,
    )
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    files = {row["path"]: (vault / row["path"]).read_bytes()
             for row in plan["results"] if row["status"]=="would_change"}
    dav = DAV(files)
    return plan, policy, shared, receipts, dav, vault


def binder(data, *, run_id="d" * 32, policy_verifier=None,
           run_verifier=None, provider_verifier=None, generation=None,
           transport=None):
    _, policy, shared, receipts, dav, _ = data
    psha = _policy_digest(policy)
    def policy_check(policy_sha, source_generation, binding, proof):
        return (policy_sha == psha and source_generation == policy.source_generation
                and binding == policy.account_binding_sha256
                and proof == policy.provider_proof_sha256)
    return RecurringEffectWriter(
        shared,
        source_generation=generation or (lambda: _generation(shared.load_nextcloud())),
        policy=policy, run_id=run_id, receipt_root=receipts,
        policy_verifier=policy_check if policy_verifier is None else policy_verifier,
        run_verifier=(lambda *_: True) if run_verifier is None else run_verifier,
        provider_verifier=(lambda *_: True) if provider_verifier is None else provider_verifier,
        transport=dav if transport is None else transport,
    )


def test_full_plan_effect_and_replay_same_run_no_second_get_or_put(tmp_path):
    data = fixture(tmp_path)
    plan, _, _, receipts, dav, vault = data
    effect = binder(data)
    result = effect(plan)
    assert result["status"] == "complete"
    assert result["vault_modified"] is True
    assert len([x for x in dav.calls if x[0] == "PUT"]) == 2
    before_retry = list(dav.calls)
    with pytest.raises(FormatWriteError, match="operation_replay_blocked"):
        effect(plan)
    assert dav.calls == before_retry
    assert (vault / "98-System/note0.md").read_bytes() == BODY


def test_same_run_changed_plan_denied_before_extra_http(tmp_path):
    data = fixture(tmp_path)
    plan, policy, shared, receipts, dav, vault = data
    effect = binder(data)
    effect(plan)
    prior_calls = list(dav.calls)
    (vault / "98-System/note0.md").write_bytes(b"# Human edit\n \ncontent\n")
    changed = create_plan(vault)
    with pytest.raises(FormatWriteError, match="operation_replay_blocked"):
        effect(changed)
    assert dav.calls == prior_calls


@pytest.mark.parametrize("missing", ["policy", "run", "provider"])
def test_missing_authority_no_provider_call(tmp_path, missing):
    data = fixture(tmp_path)
    plan, _, _, receipts, dav, vault = data
    effect = binder(data)
    if missing == "policy": effect._policy_verifier = None
    if missing == "run": effect._run_verifier = None
    if missing == "provider": effect._provider_verifier = None
    with pytest.raises(FormatWriteError, match="recurring_effect_authority_missing"):
        effect(plan)
    assert dav.calls == [] and list(receipts.iterdir()) == []


@pytest.mark.parametrize("failure", ["policy", "run", "provider"])
def test_rejected_evidence_never_sends_http(tmp_path, failure):
    data = fixture(tmp_path)
    plan, _, _, receipts, dav, vault = data
    effect = binder(
        data,
        policy_verifier=(lambda *_: False) if failure == "policy" else None,
        run_verifier=(lambda *_: False) if failure == "run" else None,
        provider_verifier=(lambda *_: False) if failure == "provider" else None,
    )
    with pytest.raises(FormatWriteError):
        effect(plan)
    assert dav.calls == [] and list(receipts.iterdir()) == []


def test_stale_credential_generation_before_effect_no_http(tmp_path):
    data = fixture(tmp_path)
    plan, _, shared, receipts, dav, vault = data
    shared.details["nextcloud_password"] = "ROTATED"
    effect = binder(data)
    with pytest.raises(FormatWriteError, match="recurring_source_generation_changed"):
        effect(plan)
    assert dav.calls == []


def test_generation_mutated_by_run_verifier_no_http(tmp_path):
    data=fixture(tmp_path)
    plan, _, shared, receipts, dav, vault=data
    original=shared.details["nextcloud_password"]
    def rotate(*_):
        shared.details["nextcloud_password"]="ROTATED"
        return True
    effect=binder(data, run_verifier=rotate)
    with pytest.raises(FormatWriteError, match="recurring_source_generation_changed"):
        effect(plan)
    assert not dav.calls
    assert list(receipts.iterdir())==[]


def test_mutable_caller_plan_during_policy_check_does_not_change_effect(tmp_path):
    data=fixture(tmp_path)
    plan, policy, shared, receipts, dav, vault=data
    digest=_policy_digest(policy)
    def mutate(*_):
        plan["results"].clear()
        plan["counts"]["would_change"]=0
        return True
    effect=binder(data, policy_verifier=mutate)
    outcome=effect(plan)
    assert outcome["status"]=="complete"
    assert len([x for x in dav.calls if x[0]=="PUT"])==2


def test_writer_requires_approved_run_id_proof_without_normal_runtime(tmp_path):
    data=fixture(tmp_path)
    plan, _, shared, receipts, dav, vault=data
    seen=[]
    effect=binder(data, run_verifier=lambda *args: seen.append(args) or True)
    result=effect(plan)
    assert result["status"]=="complete"
    assert len(seen) >= 4  # admission plus GET/PUT/readback fences
    assert len(set(seen)) == 1
    rid, plan_sha, policy_sha, op=seen[0]
    assert rid=="d"*32 and plan_sha==plan["plan_sha256"]
    assert len(op)==32 and len(policy_sha)==64


def test_concurrent_invocation_is_rejected_without_second_effect(tmp_path):
    data = fixture(tmp_path, count=1)
    plan, _, _, receipts, dav, _ = data
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocking_dav(**kwargs):
        if kwargs["method"] == "GET" and not entered.is_set():
            entered.set()
            assert release.wait(4)
        return dav(**kwargs)

    effect = binder(data, transport=blocking_dav)

    def first():
        try:
            assert effect(plan)["status"] == "complete"
        except BaseException as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(FormatWriteError, match="^recurring_effect_busy$"):
            effect(plan)
        assert not dav.calls
    finally:
        release.set()
        thread.join(5)
    assert not errors
    assert len([row for row in dav.calls if row[0] == "PUT"]) == 1


def test_generation_mutated_at_transport_boundary_blocks_next_effect(tmp_path):
    data = fixture(tmp_path, count=1)
    plan, _, shared, receipts, dav, _ = data
    calls = []

    def rotate_after_first_get(**kwargs):
        calls.append(kwargs["method"])
        result = dav(**kwargs)
        if kwargs["method"] == "GET":
            shared.details["nextcloud_password"] = "ROTATED"
        return result

    effect = binder(data, transport=rotate_after_first_get)
    # Initial GET passed before the generation changed, so the Writer
    # already recorded an 'attempting' marker. Its PUT transport guard then
    # refuses the request; the Writer conservatively records an unknown
    # effect because a request that reached attempting is never auto-retried.
    result = effect(plan)
    assert result["status"] == "indeterminate"
    assert result["vault_modified"] is None
    assert "PUT" not in calls


def test_durable_run_verifier_failure_does_not_touch_receipts(tmp_path):
    data = fixture(tmp_path, count=1)
    plan, _, _, receipts, dav, _ = data

    def denied(*_):
        raise RuntimeError("PRIVATE_NOTE_NOT_FOR_LOGGING")

    effect = binder(data, run_verifier=denied)
    with pytest.raises(FormatWriteError, match="^recurring_run_unverified$") as err:
        effect(plan)
    assert "PRIVATE_NOTE" not in str(err.value)
    assert not list(receipts.iterdir())
    assert not dav.calls


@pytest.mark.parametrize("revoke", ["run", "provider", "policy"])
def test_authority_revoked_after_get_prevents_put(tmp_path, revoke):
    data = fixture(tmp_path, count=1)
    plan, _, _, receipts, dav, _ = data
    approved = [True]
    def verifier(*_):
        return approved[0]
    def rotate_after_get(**kwargs):
        response = dav(**kwargs)
        if kwargs["method"] == "GET":
            approved[0] = False
        return response
    effect = binder(
        data, transport=rotate_after_get,
        policy_verifier=verifier if revoke == "policy" else None,
        run_verifier=verifier if revoke == "run" else None,
        provider_verifier=verifier if revoke == "provider" else None,
    )
    result = effect(plan)
    # Initial preflight GET succeeded but authorization was revoked before PUT.
    # Guarded Writer's attempting marker remains indeterminate (never replay).
    assert result["status"] == "indeterminate"
    assert result["vault_modified"] is None
    assert len([x for x in dav.calls if x[0] == "PUT"]) == 0


def test_falsy_callable_transport_is_not_replaced_with_live_http(tmp_path):
    data=fixture(tmp_path,count=1)
    plan, _, _, _, dav, _=data

    class FalsyTransport:
        def __bool__(self):
            return False
        def __call__(self, **kwargs):
            return dav(**kwargs)

    result=binder(data,transport=FalsyTransport())(plan)
    assert result["status"]=="complete"
    assert len([x for x in dav.calls if x[0]=="PUT"])==1


def test_noncallable_transport_is_refused_before_webdav(tmp_path):
    data=fixture(tmp_path,count=1)
    plan, policy, shared, receipts, dav, _=data
    writer=binder(data,transport=object())
    with pytest.raises(FormatWriteError,match="^recurring_transport_invalid$"):
        writer(plan)
    assert not dav.calls and not list(receipts.iterdir())


def test_external_run_verifier_can_wait_for_setup_store_reader(tmp_path):
    """Verifier must never run while SetupStore's lock is held."""
    data=fixture(tmp_path,count=1)
    plan, _, shared, _, dav, _=data
    thread_errors=[]

    def run_verifier(*args):
        seen=[]
        def read_store():
            try:
                seen.append(shared.load_nextcloud())
            except Exception as error:
                thread_errors.append(type(error).__name__)
        t=threading.Thread(target=read_store,daemon=True)
        t.start()
        t.join(2)
        assert not t.is_alive(), "run verifier deadlocked holding store mutex"
        assert seen and not thread_errors
        return True

    result=binder(data,run_verifier=run_verifier)(plan)
    assert result["status"]=="complete"
    assert len([x for x in dav.calls if x[0]=="PUT"])==1


def test_failing_shared_connection_load_is_sanitized(tmp_path):
    data=fixture(tmp_path,count=1)
    plan, _, shared, receipts, dav, _=data
    def leak():
        raise RuntimeError("SYNTHETIC_PRIVATE_APP_PASSWORD")
    shared.load_nextcloud=leak
    # Use a pinned generation callback which does not itself touch the shared
    # store so that the initial credential loading path is reached.
    policy=data[1]
    writer=binder(data,generation=lambda: policy.source_generation)
    with pytest.raises(FormatWriteError,match="^recurring_connection_missing$") as err:
        writer(plan)
    assert "PRIVATE" not in str(err.value)
    assert not dav.calls and not list(receipts.iterdir())


def test_revocation_race_during_put_cannot_be_stopped_without_lease(tmp_path):
    """Document the boundary: checks do not atomically bind remote transmission.

    This is synthetic and intentionally demonstrates that a later revocation
    cannot cancel a PUT already past the last local approval check.
    """
    data=fixture(tmp_path,count=1)
    plan, _, shared, receipts, dav, _=data
    approved=[True]

    def late_revoke(**kwargs):
        if kwargs["method"]=="PUT":
            approved[0]=False  # AFTER the pre-request authority check
        return dav(**kwargs)

    result=binder(data,transport=late_revoke,
                  run_verifier=lambda *_: approved[0])(plan)
    assert any(x[0]=="PUT" for x in dav.calls)
    assert result["status"]=="indeterminate"
    # Completion/readback is refused after revocation; the prior PUT may
    # already have reached the server and cannot be declared rolled back.
    assert result["vault_modified"] is None


def test_credential_exception_from_generation_is_redacted(tmp_path):
    data=fixture(tmp_path,count=1)
    plan, _, shared, receipts, dav, _=data
    def generation():
        raise RuntimeError("SYNTHETIC_PRIVATE_APP_PASSWORD")
    writer=binder(data,generation=generation)
    with pytest.raises(FormatWriteError,match="^recurring_source_unavailable$") as err:
        writer(plan)
    assert "PRIVATE" not in str(err.value)
    assert not dav.calls


def test_generation_callback_can_wait_for_store_reader(tmp_path):
    """No injected generation callback may be invoked while owning the store lock."""
    data=fixture(tmp_path,count=1)
    plan, policy, shared, _, dav, _=data
    failures=[]

    def generation():
        values=[]
        def read():
            try:
                values.append(_generation(shared.load_nextcloud()))
            except Exception as exc:
                failures.append(type(exc).__name__)
        t=threading.Thread(target=read,daemon=True)
        t.start()
        t.join(2)
        assert not t.is_alive(), "generation callback deadlocked waiting for store reader"
        assert values and not failures
        return values[0]

    result=binder(data,generation=generation)(plan)
    assert result["status"]=="complete"
    assert len([x for x in dav.calls if x[0]=="PUT"])==1


def test_transport_callback_can_wait_for_setup_store_reader(tmp_path):
    """A transport callback must not be invoked under SetupStore._lock."""
    data=fixture(tmp_path,count=1)
    plan, _, shared, _, dav, _=data
    failures=[]

    def transport(**kwargs):
        seen=[]
        def read_store():
            try:
                seen.append(shared.load_nextcloud())
            except Exception as exc:
                failures.append(type(exc).__name__)
        t=threading.Thread(target=read_store,daemon=True)
        t.start()
        t.join(2)
        assert not t.is_alive(), "transport callback deadlocked waiting on SetupStore"
        assert seen and not failures
        return dav(**kwargs)

    result=binder(data,transport=transport)(plan)
    assert result["status"]=="complete"
    assert len([x for x in dav.calls if x[0]=="PUT"])==1
