"""Synthetic-only regression coverage for bounded Formatter skip-reason telemetry.

Never access a real Vault, remote provider, or production state.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import sqlite3
import threading
import uuid

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.vault_format_job import FormatterJob
from obsidian_automation.vault_formatter import create_plan


def _plan(tmp_path):
    vault = tmp_path / "synthetic-vault"
    system = vault / "98-System"
    system.mkdir(parents=True)
    cases = {
        "secret-marker-valid.md": b"# heading\n  \nlast\n",
        "secret-marker-unchanged.md": b"Plain text\n",
        "secret-marker-raw-html.md": b"<div>\n  \n</div>\n",
        "secret-marker-unclosed-fence.md": bytes([96]) * 3 + b"\n  \n",
        "secret-marker-binary.md": b"\x00\x01",
        "secret-marker-extension.txt": b"not supported\n",
    }
    for name, payload in cases.items():
        (system / name).write_bytes(payload)
    plan = create_plan(vault)
    assert plan["counts"] == {"skipped": 4, "unchanged": 1, "would_change": 1}
    return vault, plan


def _job(tmp_path, plan):
    return FormatterJob(
        tmp_path / "private-job", lambda: plan,
        configured=lambda: True,
        source_generation=lambda: "a" * 64,
    )


def _complete(job):
    status, receipt = job.submit(uuid.uuid4().hex)
    assert status == "accepted"
    job._worker.join(5)
    return job.run(receipt["run_id"])


def test_skip_codes_count_persist_without_path_or_body(tmp_path):
    vault, plan = _plan(tmp_path)
    source_digest = hashlib.sha256(
        (vault / "98-System/secret-marker-valid.md").read_bytes()
    ).hexdigest()
    expected = {
        "binary-content": 1,
        "unsupported-extension": 1,
        "unsupported-html-block": 1,
        "unterminated-protected-block": 1,
    }
    with _job(tmp_path, plan) as job:
        final = _complete(job)
        assert final["status"] == "would_change"
        assert final["skipped"] == 4
        assert final["skip_reasons"] == expected
        assert sum(final["skip_reasons"].values()) == final["skipped"]
        assert job.status()["last_run"]["skip_reasons"] == expected
        encoded = job.db.execute(
            "SELECT skip_reasons_json FROM runs WHERE run_id=?", (final["run_id"],)
        ).fetchone()[0]
        assert json.loads(encoded) == expected
        assert "secret-marker" not in encoded
        assert "secret-marker" not in json.dumps(job.status())
    with _job(tmp_path, plan) as reopened:
        assert reopened.run(final["run_id"])["skip_reasons"] == expected
        assert reopened.settings()["mode"] == "off"
    assert hashlib.sha256(
        (vault / "98-System/secret-marker-valid.md").read_bytes()
    ).hexdigest() == source_digest


def test_unknown_or_untrusted_reason_only_emits_unclassified(tmp_path):
    _, original = _plan(tmp_path)
    plan = copy.deepcopy(original)
    payload = "SYNTHETIC_SECRET_AND_USER_PRIVATE_FILENAME"
    changed = False
    for row in plan["results"]:
        if row["status"] == "skipped" and row["reason"] == "unsupported-html-block":
            row["reason"] = payload
            changed = True
    assert changed
    canonical = {key: value for key, value in plan.items() if key not in ("plan_sha256", "diffs")}
    plan["plan_sha256"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    with _job(tmp_path, plan) as job:
        final = _complete(job)
        assert final["status"] == "would_change"
        assert final["skipped"] == 4
        assert final["skip_reasons"] == {
            "binary-content": 1,
            "unclassified": 1,
            "unsupported-extension": 1,
            "unterminated-protected-block": 1,
        }
        stored = job.db.execute(
            "SELECT skip_reasons_json FROM runs WHERE run_id=?", (final["run_id"],)
        ).fetchone()[0]
        assert payload not in stored
        assert payload not in json.dumps(final)


def test_legacy_run_migrates_without_inventing_old_reasons(tmp_path):
    state = tmp_path / "legacy"
    state.mkdir(mode=0o700)
    run_id = "b" * 32
    with sqlite3.connect(state / "formatter.sqlite3") as con:
        con.execute(
            "CREATE TABLE runs ("
            "run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,"
            "trigger TEXT NOT NULL, run_mode TEXT NOT NULL,"
            "status TEXT NOT NULL, created REAL NOT NULL, completed REAL,"
            "policy_version TEXT, scope TEXT, plan_sha256 TEXT,"
            "would_change INTEGER, skipped INTEGER, errors INTEGER,"
            "writer_outcome TEXT, reason_code TEXT)"
        )
        con.execute(
            "INSERT INTO runs(run_id,request_id,trigger,run_mode,status,created,"
            "completed,would_change,skipped,errors) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, "c" * 32, "manual", "dry-run", "no_changes", 1.0, 2.0, 0, 27, 0),
        )
    with FormatterJob(state, lambda: None) as job:
        last = job.status()["last_run"]
        assert last["skipped"] == 27
        assert last["skip_reasons"] is None  # legacy coverage is unknown
        assert job.run(run_id)["skip_reasons"] is None
        assert "skip_reasons_json" in {
            info[1] for info in job.db.execute("PRAGMA table_info(runs)")
        }
        assert job.settings()["mode"] == "off"


def test_http_and_ui_include_only_aggregate_reason_counts(tmp_path):
    _, plan = _plan(tmp_path)
    with _job(tmp_path, plan) as job:
        final = _complete(job)
        server = make_server(
            port=0, formatter_job=job,
            observer=lambda: [], ai_observer=lambda: {},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                client.request("GET", "/api/v1/formatter")
                response = client.getresponse()
                assert response.status == 200
                body = response.read().decode()
            finally:
                client.close()
            data = json.loads(body)
            assert data["settings"]["mode"] == "off"
            assert data["settings"]["write_capable"] is False
            assert data["last_run"]["run_id"] == final["run_id"]
            assert data["last_run"]["skip_reasons"] == final["skip_reasons"]
            assert "secret-marker" not in body
            assert "SYNTHETIC_SECRET" not in body
        finally:
            server.shutdown()
            thread.join(4)
            server.server_close()
    assert 'id="formatter-skip-reasons"' in HTML
    assert '"formatter-skip-reasons").textContent' in JS
    assert "旧記録では未取得" in JS
    assert "innerHTML" not in JS



def test_no_skipped_files_records_empty_map_not_unknown(tmp_path):
    root = tmp_path / "clean-vault" / "98-System"
    root.mkdir(parents=True)
    (root / "ordinary.md").write_bytes(b"simple text\n")
    with _job(tmp_path, create_plan(root.parent)) as job:
        final = _complete(job)
        assert final["status"] == "no_changes"
        assert final["skipped"] == 0
        assert final["skip_reasons"] == {}
        persisted = job.db.execute(
            "SELECT skip_reasons_json FROM runs WHERE run_id=?", (final["run_id"],)
        ).fetchone()[0]
        assert persisted == "{}"


def test_corrupt_persisted_reason_data_never_leaks(tmp_path):
    _, plan = _plan(tmp_path)
    with _job(tmp_path, plan) as job:
        final = _complete(job)
        for raw in (
            "private-text-not-json",
            '{"private-filename":4}',
            '{"binary-content":999}',
            '{"binary-content":true}',
            '{"binary-content":-1}',
        ):
            with job.db:
                job.db.execute(
                    "UPDATE runs SET skip_reasons_json=? WHERE run_id=?",
                    (raw, final["run_id"]),
                )
            readback = job.run(final["run_id"])
            assert readback["skip_reasons"] is None
            assert "private-" not in json.dumps(readback)
            assert readback["skipped"] == 4
