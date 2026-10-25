"""B4 verified Standard CAS → deterministic section → existing WebDAV CAS Writer.

All remote providers are synthetic. No Nextcloud, Gitea or live Vault effects.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
import threading
import uuid

import pytest

from obsidian_automation.core_promotion_transport import (
    HTTPResponse, PromotionTransportNetworkError,
)
from obsidian_automation.github_daily_deterministic_writer import publish_verified_daily
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.github_daily_progress import (
    DailyProgressConflict, DailyProgressError, DailyProgressTargetMissing,
    daily_target_path,
)
from obsidian_automation.github_daily_runonce import run_once
from obsidian_automation.github_daily_setup import DailySetup
from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation import github_daily_source_route as route
from test_github_daily_setup import TOKEN, project_copy
from test_github_daily_source_route import _FakeClient

DAY = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)
JST_YESTERDAY = "2026-10-05"
ORIGINAL = (b"---\ntype: daily-review\n---\n# Note\nKeep my personal note.\n\n"
            b"# Project Progress\nOld daily progress.\n\n"
            b"# Other\nKeep this section exactly.\n")
EXPECTED_URL = ("https://cloud.invalid/remote.php/dav/files/user/"
                "Vault/00-DailyNote/2026/10/2026-10-05.md")


class SyntheticDailyDAV:
    def __init__(self, content=ORIGINAL, *, etag='"before"'):
        self.content = content
        self.etag = etag
        self.calls = []
        self.stale_etag = False
        self.ambiguous_after_effect = False
        self.block_put = False

    def __call__(self, *, method, target_url, username, password,
                 headers, body, timeout, response_limit):
        self.calls.append((method, target_url, headers, body))
        assert target_url == EXPECTED_URL
        assert username == "user" and password == "NC-SYNTHETIC"
        assert timeout > 0
        if method == "GET":
            return HTTPResponse(status=200, body=self.content, etag=self.etag)
        assert method == "PUT"
        assert headers["If-Match"] == '"before"' or self.etag != '"before"'
        assert headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert isinstance(body, bytes)
        if self.stale_etag:
            return HTTPResponse(status=412, body=b"", etag='"changed"')
        if self.block_put:
            return HTTPResponse(status=403, body=b"", etag=self.etag)
        if headers["If-Match"] != self.etag:
            return HTTPResponse(status=412, body=b"", etag=self.etag)
        self.content = body
        self.etag = '"after"'
        if self.ambiguous_after_effect:
            raise PromotionTransportNetworkError("synthetic ambiguous PUT")
        return HTTPResponse(status=204, body=b"", etag=self.etag)

    @property
    def puts(self):
        return [x for x in self.calls if x[0] == "PUT"]


@pytest.fixture
def previous_jst_preview(tmp_path, monkeypatch):
    # B4-only source, no Gitea credentials or rclone/network provider.
    state = tmp_path / "private-app"
    store = SetupStore(state, probe=lambda _: True, obscure=lambda _: "encoded")
    store.nextcloud_probe = lambda _: True
    source = {"webdav_url": "https://cloud.invalid/remote.php/dav/files/user/",
              "vault_folder": "Vault",
              "nextcloud_user": "user",
              "nextcloud_password": "NC-SYNTHETIC"}
    assert store.save_nextcloud(source)
    assert store.load() is None  # B2 Snapshot really remains unconfigured.
    assert store.load_nextcloud() == source
    setup = DailySetup(state / "github-daily", store, probe=lambda _: True,
                       copier=project_copy)
    assert setup.save({"github_token": TOKEN})
    monkeypatch.setattr(route, "_utc_now", lambda: DAY)
    with setup.project_source() as (config, token):
        assert token == TOKEN
        receipt = run_once(
            config, _FakeClient(visible_pr=True, include_commit=True),
            cas_root=setup.state / "cas",
            preview_root=setup.state / "preview",
            now=DAY,
        )
    assert receipt["status"] == "preview_ready"
    assert receipt["date_jst"] == JST_YESTERDAY
    assert receipt["rendered_entity_count"] >= 1
    assert receipt["publication_authorized"] is False
    assert receipt["vault_modified"] is False
    assert store.load() is None
    return store, setup, receipt


def publish(fixture, dav):
    store, setup, receipt = fixture
    return publish_verified_daily(
        receipt, cas_root=setup.state / "cas", shared=store, transport=dav,
    )


def test_verified_cas_applies_section_only_strong_etag_and_is_idempotent(previous_jst_preview):
    dav = SyntheticDailyDAV()
    published = publish(previous_jst_preview, dav)
    assert published["status"] == "published"
    assert published["publication_outcome"] == "applied"
    assert published["publication_verified"] is True
    assert published["vault_modified"] is True
    assert published["model_used"] is False
    assert published["date_jst"] == JST_YESTERDAY
    assert published["target_path"] == daily_target_path(JST_YESTERDAY)
    assert len(dav.puts) == 1
    assert dav.puts[0][2]["If-Match"] == '"before"'
    assert dav.content.startswith(ORIGINAL.split(b"# Project Progress")[0] + b"# Project Progress\n")
    assert b"Old daily progress." not in dav.content
    assert dav.content.endswith(b"# Other\nKeep this section exactly.\n")
    assert b"https://github.com/" in dav.content

    replay = publish(previous_jst_preview, dav)
    assert replay["status"] == "published"
    assert replay["publication_outcome"] == "already_desired"
    assert replay["vault_modified"] is False
    assert replay["before_content_sha256"] == replay["after_content_sha256"]
    assert len(dav.puts) == 1  # no repeated write on identical desired body


def test_cas_rejects_missing_weak_etag_conflict_and_invalid_target(previous_jst_preview):
    for weak in (None, 'W/"weak"'):
        dav = SyntheticDailyDAV(etag=weak)
        with pytest.raises(DailyProgressError, match="strong ETag"):
            publish(previous_jst_preview, dav)
        assert not dav.puts and dav.content == ORIGINAL

    dav = SyntheticDailyDAV()
    dav.stale_etag = True
    with pytest.raises(DailyProgressConflict) as raised:
        publish(previous_jst_preview, dav)
    assert raised.value.reason_code == "etag_cas_conflict"
    assert dav.content == ORIGINAL

    class Missing:
        def __init__(self): self.methods = []
        def __call__(self, **kwargs):
            self.methods.append(kwargs["method"])
            return HTTPResponse(status=404, body=b"", etag=None)
    missing = Missing()
    with pytest.raises(DailyProgressTargetMissing):
        publish(previous_jst_preview, missing)
    assert missing.methods == ["GET"]  # never create a missing Daily Note

    duplicate = SyntheticDailyDAV(
        content=ORIGINAL + b"# Project Progress\nSecond.\n"
    )
    with pytest.raises(DailyProgressConflict, match="exactly one"):
        publish(previous_jst_preview, duplicate)
    assert not duplicate.puts


def test_ambiguous_remote_put_must_re_get_exact_desired_bytes(previous_jst_preview):
    dav = SyntheticDailyDAV()
    dav.ambiguous_after_effect = True
    outcome = publish(previous_jst_preview, dav)
    assert outcome["publication_outcome"] == "recovered"
    assert outcome["after_content_sha256"] == hashlib.sha256(dav.content).hexdigest()
    assert len(dav.puts) == 1


def test_bad_receipt_or_missing_standard_cas_never_fetches_remote(previous_jst_preview):
    store, setup, receipt = previous_jst_preview
    for damaged in (
        {**receipt, "capture_sha256": "0" * 64},
        {**receipt, "digest_sha256": "1" * 64},
        {**receipt, "date_jst": "2026-10-04"},
        {**receipt, "model_used": True},
        {**receipt, "publication_authorized": True},
    ):
        dav = SyntheticDailyDAV()
        with pytest.raises((DailyProgressError, ValueError)):
            publish_verified_daily(damaged, cas_root=setup.state / "cas",
                                   shared=store, transport=dav)
        assert not dav.calls

    # A corrupt immutable JSON payload with a retained filename digest
    # cannot be admitted from Standard CAS.
    cas = setup.state / "cas" / "capture"
    files = list(cas.iterdir())
    assert len(files) == 1
    old = files[0].read_bytes()
    files[0].write_bytes(old + b" ")
    dav = SyntheticDailyDAV()
    with pytest.raises((DailyProgressError, ValueError)):
        publish_verified_daily(receipt, cas_root=setup.state / "cas",
                               shared=store, transport=dav)
    assert not dav.calls


def test_daily_job_persists_verified_publication_and_keeps_preview_on_writer_failure(
    previous_jst_preview, tmp_path,
):
    store, setup, preview = previous_jst_preview
    dav = SyntheticDailyDAV()
    publisher = lambda proof: publish_verified_daily(
        proof, cas_root=setup.state / "cas", shared=store, transport=dav,
    )
    state = tmp_path / "persistent-daily-job"
    with DailyJob(state, lambda: preview, configured=lambda: True, publisher=publisher) as job:
        assert job.status()["publication_enabled"] is True
        kind, row = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(4)
        result = job.run(row["run_id"])
        assert result["status"] == "published"
        assert result["publication_outcome"] == "applied"
        assert result["date_jst"] == JST_YESTERDAY
        assert result["capture_sha256"] == preview["capture_sha256"]
        assert TOKEN not in json.dumps(result)
        assert len(dav.puts) == 1
    with DailyJob(state, lambda: preview, configured=lambda: True) as resumed:
        old = resumed.run(row["run_id"])
        assert resumed.status()["publication_enabled"] is False
        assert old["status"] == "published"
        assert old["publication_outcome"] == "applied"

    dav = SyntheticDailyDAV(etag='W/"weak"')
    publisher_bad = lambda proof: publish_verified_daily(
        proof, cas_root=setup.state / "cas", shared=store, transport=dav,
    )
    with DailyJob(tmp_path / "writer-failed", lambda: preview,
                  configured=lambda: True, publisher=publisher_bad) as job:
        _, row = job.submit(uuid.uuid4().hex)
        job._worker.join(4)
        saved = job.run(row["run_id"])
        assert saved["status"] == "publish_failed"
        assert saved["publication_outcome"] is None
        assert saved["digest_sha256"] == preview["digest_sha256"]
        assert saved["capture_sha256"] == preview["capture_sha256"]
        assert saved["date_jst"] == JST_YESTERDAY
        assert not dav.puts


def test_daily_job_rejects_false_writer_success_and_retains_preview(previous_jst_preview, tmp_path):
    _, _, preview = previous_jst_preview
    def liar(proof):
        return {"version": 1, "status": "published", "publication_verified": True,
                "model_used": False, "vault_modified": False,
                "publication_outcome": "already_desired",
                "date_jst": "2026-10-04",
                "capture_sha256": proof["capture_sha256"],
                "digest_sha256": proof["digest_sha256"],
                "before_content_sha256": "a" * 64,
                "after_content_sha256": "a" * 64}
    with DailyJob(tmp_path / "fake", lambda: preview,
                  configured=lambda: True, publisher=liar) as job:
        _, row=job.submit(uuid.uuid4().hex)
        job._worker.join(4)
        assert job.run(row["run_id"])["status"] == "publish_failed"
        assert job.run(row["run_id"])["digest_sha256"] == preview["digest_sha256"]
        assert job.run(row["run_id"])["publication_outcome"] is None


def test_timer_same_job_produces_published_receipt_only_when_injected(
    previous_jst_preview, tmp_path
):
    store, setup, preview = previous_jst_preview
    time_now = [1000.0]
    dav = SyntheticDailyDAV()
    publisher = lambda proof: publish_verified_daily(
        proof, cas_root=setup.state / "cas", shared=store, transport=dav)
    with DailyJob(tmp_path / "schedule", lambda: preview,
                  configured=lambda: True, publisher=publisher,
                  clock=lambda: time_now[0]) as job:
        assert job.update({"enabled": True, "interval_seconds": 86400})["next_due"] == 87400
        assert not job.tick()
        time_now[0]=87400.0
        assert job.tick()
        job._worker.join(4)
        assert job.status()["last_run"]["status"] == "published"
        assert job.status()["last_run"]["trigger"] == "scheduled"
        assert job.status()["settings"]["next_due"] == 173800.0
        assert len(dav.puts) == 1


def test_startup_mode_default_off_explicit_opt_in_requires_state(tmp_path, monkeypatch):
    from obsidian_automation import snapshot_app
    from obsidian_automation.admin_dashboard import JS
    import sys

    observed = []
    class FakeServer:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def serve_forever(self, **kwargs):
            assert self.daily_job.status()["last_run"] is None
            assert not self.daily_job.settings()["enabled"]
            observed.append(self.daily_job.publisher is not None)

    monkeypatch.setattr(snapshot_app, "make_server", lambda **kwargs: FakeServer())
    for flag in (False, True):
        state = tmp_path / ("enabled" if flag else "disabled")
        argv = ["obsidian-snapshot-app", "--state", str(state), "--port", "0"]
        if flag: argv.append("--daily-publish")
        monkeypatch.setattr(sys, "argv", argv)
        assert snapshot_app.main() == 0
    assert observed == [False, True]
    assert "published:" in JS and "publish_failed:" in JS
    assert "publication_enabled" in JS


def test_private_lan_bind_and_writer_opt_in_can_coexist(tmp_path, monkeypatch):
    """Merged #461 --bind and #462 --daily-publish must not drop either flag."""
    import sys
    from obsidian_automation import snapshot_app

    created = []

    class FakeServer:
        def __init__(self, kwargs):
            created.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def serve_forever(self, **kwargs):
            assert created[-1]["bind_address"] == "10.12.2.15"
            assert self.daily_job.publisher is not None
            assert self.daily_job.settings()["enabled"] is False
            assert self.daily_job.status()["publication_enabled"] is True
            assert self.snapshot_job.settings()["configured"] is False
            assert self.snapshot_job.settings()["enabled"] is False
            assert self.snapshot_job._scheduler is not None
            assert not hasattr(self.daily_job, "_scheduler")

    monkeypatch.setattr(snapshot_app, "make_server", lambda **kwargs: FakeServer(kwargs))
    monkeypatch.setattr(sys, "argv", [
        "snapshot-app", "--state", str(tmp_path / "fresh-app"),
        "--port", "0", "--bind", "10.12.2.15", "--daily-publish",
    ])
    assert snapshot_app.main() == 0
    assert len(created) == 1
    assert created[0]["port"] == 0
