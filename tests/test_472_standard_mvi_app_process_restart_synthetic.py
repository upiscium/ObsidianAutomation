"""#472: subprocess-level snapshot_app.main() synthetic lifecycle acceptance.

Real CLI entrypoint, real dashboard/Job/SQLite/Scheduler, separate OS processes.
Only the Formatter SOURCE is replaced by a disposable local fixture; provider
entrypoints are forbidden. A test-injected SIGTERM handler permits cooperative
shutdown; this does NOT prove the unmodified service handles SIGTERM gracefully.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid


# This code runs in a FRESH child Python interpreter, not pytest's process.
# It executes snapshot_app.main(), with only provider-safe local test seams.
CHILD_CODE = r"""
import os
from pathlib import Path
import signal
import sys
import threading

from obsidian_automation import snapshot_app
from obsidian_automation.vault_formatter import create_plan


def forbidden_provider(*args, **kwargs):
    Path(os.environ["OA_TEST_FORBIDDEN"]).write_text("unexpected-provider-invocation")
    raise AssertionError("external_provider_not_authorized")


for name in (
    "SnapshotAdapter", "GitHubClient", "collect_b3_read_only",
    "run_once", "publish_verified_daily",
):
    setattr(snapshot_app, name, forbidden_provider)


class DisposableFormatterSource:
    def __init__(self, state, store):
        self.vault = Path(os.environ["OA_TEST_VAULT"])

    def configured(self):
        return True

    def generation(self):
        return "c" * 64

    def __call__(self):
        Path(os.environ["OA_TEST_SCANNED"]).write_text("entered", encoding="utf-8")
        if os.environ.get("OA_TEST_BLOCK_SCAN") == "1":
            threading.Event().wait(120)
        return create_plan(self.vault)


snapshot_app.FormatterSource = DisposableFormatterSource
original_make_server = snapshot_app.make_server


def instrumented_make_server(**kwargs):
    assert kwargs["bind_address"] == "127.0.0.1"
    assert kwargs["port"] == 0
    server = original_make_server(**kwargs)
    Path(os.environ["OA_TEST_PORT"]).write_text(str(server.server_address[1]))

    # Only a synthetic test harness: the real application does not register
    # this cooperative SIGTERM-to-server.shutdown() handler itself.
    def graceful_stop(_signal_number, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, graceful_stop)
    return server


snapshot_app.make_server = instrumented_make_server
original_start = snapshot_app.SnapshotJob.start_scheduler


def instrumented_start(self, *, tick_hooks=()):
    assert len(tick_hooks) == 4
    result = original_start(self, tick_hooks=tick_hooks)
    assert self._scheduler is not None and self._scheduler.is_alive()
    Path(os.environ["OA_TEST_SCHEDULER"]).write_text("real-shared-thread:4")
    return result


snapshot_app.SnapshotJob.start_scheduler = instrumented_start
sys.argv = [
    "obsidian-snapshot-app", "--state", os.environ["OA_TEST_STATE"],
    "--bind", "127.0.0.1", "--port", "0",
]
raise SystemExit(snapshot_app.main())
"""


def http_json(port: int, method: str, path: str, body: dict | None = None,
              csrf: str | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Host": f"127.0.0.1:{port}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = f"http://127.0.0.1:{port}"
    if csrf is not None:
        headers["X-Obsidian-CSRF"] = csrf
    try:
        conn.request(method, path, headers=headers,
                     body=json.dumps(body) if body is not None else None)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def await_response(port: int, run_id: str, wanted: set[str], *,
                   seconds: float = 8.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        code, run = http_json(port, "GET", "/api/v1/formatter/runs/" + run_id)
        assert code == 200
        if run["status"] in wanted:
            return run
        time.sleep(0.04)
    raise AssertionError("formatter_run_did_not_reach_" + "_".join(sorted(wanted)))


def await_scan_entry(process: subprocess.Popen, marker: Path, *,
                     seconds: float = 7.0) -> None:
    """Wait for complete local scan entry, not merely SQLite running status."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert process.poll() is None, "scan_child_terminated_before_entry"
        try:
            if marker.read_text(encoding="utf-8") == "entered":
                return
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    raise AssertionError("blocked_scan_not_entered")


def submit_manual(port: int) -> str:
    code, token = http_json(port, "GET", "/api/v1/operation-session")
    assert code == 200
    assert isinstance(token.get("csrf_token"), str)
    code, admitted = http_json(
        port, "POST", "/api/v1/formatter/runs",
        {"request_id": uuid.uuid4().hex},
        csrf=token["csrf_token"],
    )
    assert code == 202
    assert admitted["outcome"] == "accepted"
    assert isinstance(admitted["run_id"], str)
    return admitted["run_id"]


def start_app(tmp_path: Path, state: Path, vault: Path, label: str,
              *, blocked: bool, processes: list[subprocess.Popen]) -> tuple[subprocess.Popen, int, Path, Path]:
    port_file = tmp_path / (label + "-port")
    scheduler_marker = tmp_path / (label + "-scheduler")
    scanned = tmp_path / (label + "-scanned")
    forbidden = tmp_path / (label + "-forbidden")
    stderr_path = tmp_path / (label + "-stderr")
    env = dict(os.environ)
    env.update({
        "OA_TEST_STATE": str(state),
        "OA_TEST_VAULT": str(vault),
        "OA_TEST_PORT": str(port_file),
        "OA_TEST_SCHEDULER": str(scheduler_marker),
        "OA_TEST_SCANNED": str(scanned),
        "OA_TEST_FORBIDDEN": str(forbidden),
        "OA_TEST_BLOCK_SCAN": "1" if blocked else "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    with stderr_path.open("wb") as logfile:
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD_CODE],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=logfile, env=env,
        )
    processes.append(process)
    deadline = time.monotonic() + 9
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("synthetic_app_exited_at_startup")
        if port_file.exists() and scheduler_marker.exists():
            # Existence alone races the child's opening/write/close sequence.
            # Admit startup only after a complete marker and live HTTP reply.
            try:
                if scheduler_marker.read_text() != "real-shared-thread:4":
                    time.sleep(0.04)
                    continue
                port = int(port_file.read_text())
                code, data = http_json(port, "GET", "/api/v1/formatter")
                if code == 200 and data["job"] == "vault_formatter":
                    return process, port, scanned, forbidden
            except (OSError, TimeoutError, ValueError):
                pass
        time.sleep(0.04)
    raise AssertionError("synthetic_app_failed_to_serve_loopback")


def stop_cooperatively(process: subprocess.Popen) -> None:
    assert process.poll() is None
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=7) == 0


def test_real_app_process_clean_then_crashed_scan_then_restart(tmp_path: Path) -> None:
    """Actual app.main in three interpreters; synthetic no-write crash quarantine."""
    state = tmp_path / "fresh-one-lxc-state"
    vault = tmp_path / "synthetic-vault"
    note = vault / "98-System" / "startup.md"
    note.parent.mkdir(parents=True)
    source_bytes = b"# Startup\nparagraph\n  \ncontent\n"
    note.write_bytes(source_bytes)
    processes: list[subprocess.Popen] = []
    try:
        # Fresh actual main() run: normal source-only Off defaults; manually
        # run one dry-run to create a durable terminal receipt.
        first, port1, scanned1, denied1 = start_app(
            tmp_path, state, vault, "first", blocked=False, processes=processes,
        )
        code, status = http_json(port1, "GET", "/api/v1/formatter")
        assert code == 200
        assert status["settings"]["mode"] == "off"
        assert status["settings"]["write_capable"] is False
        assert status["settings"]["configured"] is True  # synthetic source only
        assert status["last_run"] is None
        run1 = submit_manual(port1)
        result1 = await_response(port1, run1, {"would_change"})
        assert result1["trigger"] == "manual"
        assert result1["run_mode"] == "dry-run"
        assert result1["would_change"] == 1
        assert scanned1.exists()
        assert note.read_bytes() == source_bytes
        assert not denied1.exists()
        stop_cooperatively(first)  # test-installed handler, NOT native SIGTERM
        assert first.returncode == 0

        # A different OS process reopens the same real SQLite state.
        # A deliberately blocking synthetic runner is interrupted by SIGKILL.
        second, port2, scanned2, denied2 = start_app(
            tmp_path, state, vault, "second", blocked=True, processes=processes,
        )
        code, status2 = http_json(port2, "GET", "/api/v1/formatter")
        assert code == 200
        assert status2["settings"]["mode"] == "off"
        assert status2["last_run"]["run_id"] == run1
        assert status2["last_run"]["status"] == "would_change"
        run2 = submit_manual(port2)
        result2 = await_response(port2, run2, {"running"})
        assert result2["run_mode"] == "dry-run"
        # The DB running transition precedes actual scanner entry. SIGKILL
        # must interrupt a scanner that is verifiably inside its blocking path.
        await_scan_entry(second, scanned2)
        code, active2 = http_json(port2, "GET", "/api/v1/formatter/runs/" + run2)
        assert code == 200 and active2["status"] == "running"
        assert second.poll() is None
        assert not denied2.exists()
        second.kill()
        assert second.wait(timeout=7) == -signal.SIGKILL
        assert note.read_bytes() == source_bytes

        # Third actual main() process: the pre-kill active run is UNKNOWN
        # (not success), the original receipt survives, and no automatic
        # retry occurs even while the real shared scheduler thread runs.
        third, port3, scanned3, denied3 = start_app(
            tmp_path, state, vault, "third", blocked=False, processes=processes,
        )
        code, restored = http_json(port3, "GET", "/api/v1/formatter")
        assert code == 200
        assert restored["settings"]["mode"] == "off"
        assert restored["settings"]["write_capable"] is False
        assert restored["last_run"]["run_id"] == run2
        assert restored["last_run"]["status"] == "unconfirmed"
        code, old = http_json(port3, "GET", "/api/v1/formatter/runs/" + run1)
        assert code == 200
        assert old["status"] == "would_change"
        time.sleep(1.3)  # beyond the real scheduler's 1-second wake cadence
        assert not scanned3.exists()  # neither run was re-executed
        code, still_unknown = http_json(
            port3, "GET", "/api/v1/formatter/runs/" + run2,
        )
        assert code == 200
        assert still_unknown["status"] == "unconfirmed"

        # A NEW, explicit manual Run Once is allowed after observation;
        # the old in-doubt run must never be silently replayed.
        run3 = submit_manual(port3)
        result3 = await_response(port3, run3, {"would_change"})
        assert run3 not in (run1, run2)
        assert result3["run_mode"] == "dry-run"
        assert result3["would_change"] == 1
        assert scanned3.exists()
        assert not denied3.exists()
        assert note.read_bytes() == source_bytes
        stop_cooperatively(third)
        assert third.returncode == 0

        # SQLite writes are durable across actual process termination.
        with sqlite3.connect(state / "vault-formatter/formatter.sqlite3") as db:
            rows = dict(db.execute("SELECT run_id,status FROM runs"))
            mode = db.execute("SELECT mode,next_due FROM settings WHERE id=1").fetchone()
        assert rows == {
            run1: "would_change", run2: "unconfirmed", run3: "would_change",
        }
        assert mode == ("off", None)
        for item in ("snapshot.sqlite3", "github-daily/daily.sqlite3",
                     "vault-formatter/formatter.sqlite3",
                     "github-b3/observer-job.sqlite3"):
            assert (state / item).is_file()
        assert note.read_bytes() == source_bytes
    finally:
        # No orphan child apps or listeners, even when an assertion fails.
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
