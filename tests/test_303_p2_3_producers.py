"""#303 Phase 2 / P2-3: the B4 Daily writer follows the mapping record.

Receipts keep the logical Daily path; only the WebDAV URL uses the Daily
role's physical folder, and the publisher re-checks the pins before the PUT.
"""
from __future__ import annotations

import inspect

import pytest

from obsidian_automation import snapshot_app
from obsidian_automation import vault_bindings as vb
from obsidian_automation.github_daily_deterministic_writer import publish_verified_daily
from obsidian_automation.github_daily_progress import DailyProgressError, daily_target_path
from obsidian_automation.vault_layout import DEFAULT_ROLE_FOLDERS, physical_path
from test_github_daily_deterministic_writer import (  # noqa: F401 (fixture)
    EXPECTED_URL, JST_YESTERDAY, SyntheticDailyDAV, previous_jst_preview,
)
from test_303_p2_2_consumers import _root, folders, write_record

MAPPED_URL = EXPECTED_URL.replace("/Vault/00-DailyNote/", "/Vault/Journal/Daily/")


class MappedDAV(SyntheticDailyDAV):
    def __init__(self, url):
        super().__init__()
        self.url = url

    def __call__(self, *, target_url, **kw):
        assert target_url == self.url, target_url
        return super().__call__(target_url=EXPECTED_URL, **kw)


def test_physical_path_maps_only_the_role_folder():
    layout = {"daily": "Journal/Daily", "system": "98-System"}.get
    assert physical_path("00-DailyNote/2026/10/a.md", None) == "00-DailyNote/2026/10/a.md"
    assert physical_path("00-DailyNote/2026/10/a.md", layout) == "Journal/Daily/2026/10/a.md"
    assert physical_path("98-System/x.md", layout) == "98-System/x.md"
    for bad in ("Other/a.md", "00-DailyNote", "00-DailyNote/", "/00-DailyNote/a.md", 3,
                "00-dailynote/a.md"):
        with pytest.raises(ValueError):
            physical_path(bad, layout)
    with pytest.raises(ValueError):
        physical_path("00-DailyNote/a.md", lambda _r: "")


def test_daily_publish_writes_the_mapped_folder_and_keeps_a_logical_receipt(
        previous_jst_preview):
    store, setup, receipt = previous_jst_preview
    dav = MappedDAV(MAPPED_URL)
    published = publish_verified_daily(
        receipt, cas_root=setup.state / "cas", shared=store, transport=dav,
        layout={**DEFAULT_ROLE_FOLDERS, "daily": "Journal/Daily"}.get)
    assert published["status"] == "published" and published["publication_outcome"] == "applied"
    assert published["target_path"] == daily_target_path(JST_YESTERDAY)   # logical
    assert [m for m, *_ in dav.calls] == ["GET", "PUT", "GET"]           # all mapped


def test_daily_publish_default_layout_is_unchanged(previous_jst_preview):
    store, setup, receipt = previous_jst_preview
    dav = SyntheticDailyDAV()
    published = publish_verified_daily(
        receipt, cas_root=setup.state / "cas", shared=store, transport=dav,
        layout=DEFAULT_ROLE_FOLDERS.get)
    assert published["publication_outcome"] == "applied"


def test_daily_publish_stops_before_any_request_on_a_bad_record(previous_jst_preview):
    store, setup, receipt = previous_jst_preview
    dav = SyntheticDailyDAV()

    def bad(_role):
        raise vb.BindingsError("unreadable_record")
    with pytest.raises(DailyProgressError):
        publish_verified_daily(receipt, cas_root=setup.state / "cas", shared=store,
                               transport=dav, layout=bad)
    assert dav.calls == []


def test_publisher_writes_the_folder_its_own_guard_verified(previous_jst_preview, tmp_path):
    store, setup, receipt = previous_jst_preview
    state = tmp_path / "bindings"
    state.mkdir()
    write_record(state, daily="Journal/Daily")
    guard = vb.RoleGuard(_root(state, folders(state, daily="Journal/Daily")),
                         ("project", "daily"))
    dav = MappedDAV(MAPPED_URL)

    def publish():
        write_record(state, daily="Elsewhere")   # saved after the check
        return publish_verified_daily(
            receipt, cas_root=setup.state / "cas", shared=store, transport=dav,
            layout=lambda role: vb.physical_folder(state, role))
    assert guard.wrap(publish)()["publication_outcome"] == "applied"


def test_publisher_is_guarded_and_b4_no_longer_refuses_a_mapping():
    source = inspect.getsource(snapshot_app.main)
    assert "make_daily_publisher(daily_setup, daily_state, store,\n" in source
    assert "daily_guard, layout)" in source
    guards = snapshot_app.job_guards(object())
    assert guards["daily"]._unmapped == ()


def test_real_publisher_refusal_sends_nothing_and_is_classified(previous_jst_preview,
                                                                 tmp_path):
    from obsidian_automation.github_daily_job import DailyStageFailure
    store, setup, receipt = previous_jst_preview
    state = tmp_path / "bindings"
    state.mkdir()
    write_record(state, daily="Journal/Daily")
    server = folders(state, daily="Journal/Daily")
    server["ObsidianVault/Journal/Daily"] = "00000999recreated"
    guard = vb.RoleGuard(_root(state, server), ("project", "daily"))
    publisher = snapshot_app.make_daily_publisher(
        setup, setup.state, store, guard, lambda role: vb.physical_folder(state, role))
    with pytest.raises(DailyStageFailure) as err:
        publisher(receipt)
    assert (err.value.stage, err.value.code) == ("publication", "vault_root_refused")

    from test_b4_failure_stage_receipts import terminal
    row = terminal(tmp_path / "job", lambda: receipt, publisher)
    assert row["status"] == "publish_failed"
    assert (row["failure_stage"], row["failure_code"]) == ("publication", "vault_root_refused")
    assert row["publication_outcome"] is None


def test_other_publisher_stage_markers_stay_unconfirmed(tmp_path):
    from obsidian_automation.github_daily_job import DailyStageFailure
    from test_b4_failure_stage_receipts import preview, terminal

    def publisher(_data):
        raise DailyStageFailure("source_pull", "pull_failed")
    row = terminal(tmp_path, preview, publisher)
    assert (row["failure_stage"], row["failure_code"]) == ("publication", "outcome_unconfirmed")


def test_real_publisher_writes_the_mapped_folder(previous_jst_preview, tmp_path, monkeypatch):
    store, setup, receipt = previous_jst_preview
    state = tmp_path / "bindings"
    state.mkdir()
    write_record(state, daily="Journal/Daily")
    guard = vb.RoleGuard(_root(state, folders(state, daily="Journal/Daily")),
                         ("project", "daily"))
    dav = MappedDAV(MAPPED_URL)
    import obsidian_automation.snapshot_app as app
    real = app.publish_verified_daily
    monkeypatch.setattr(app, "publish_verified_daily",
                        lambda *a, **kw: real(*a, transport=dav, **kw))
    publisher = app.make_daily_publisher(
        setup, setup.state, store, guard, lambda role: vb.physical_folder(state, role))
    assert publisher(receipt)["publication_outcome"] == "applied"
    assert [m for m, *_ in dav.calls] == ["GET", "PUT", "GET"]


def test_mapped_ambiguous_put_reads_back_the_mapped_url(previous_jst_preview):
    store, setup, receipt = previous_jst_preview
    dav = MappedDAV(MAPPED_URL)
    dav.ambiguous_after_effect = True
    layout = {**DEFAULT_ROLE_FOLDERS, "daily": "Journal/Daily"}.get
    published = publish_verified_daily(receipt, cas_root=setup.state / "cas", shared=store,
                                       transport=dav, layout=layout)
    assert published["publication_verified"] is True
    assert [m for m, *_ in dav.calls] == ["GET", "PUT", "GET"]
    again = publish_verified_daily(receipt, cas_root=setup.state / "cas", shared=store,
                                   transport=dav, layout=layout)
    assert again["publication_outcome"] == "already_desired"


def test_physical_path_rejects_unsafe_layout_folders():
    for bad in ("../x", "a/../b", "/abs", "a//b", "a/./b"):
        with pytest.raises(ValueError):
            physical_path("00-DailyNote/a.md", lambda _r, bad=bad: bad)
