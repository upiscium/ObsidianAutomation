"""Disposable real-UID ACL / SQLite WAL test for the proposed #321 Observer.

Uses a private Linux user namespace with subordinate UID mappings. Changes
ONLY temporary /tmp fixtures. Does not create host users, touch host ACLs,
enable systemd services, handle Nextcloud credentials, or perform remote IO.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

OBS = 1001
SYNC = 1002
SCHEDULER = 1003
UNAUTHORIZED = 1004
ROLES = ("reader", "generator", "validator", "evaluator", "reviewer", "executor", "sync")
CHILD_STARTED = "OBS321_CHILD_STARTED"
CHILD_PASSED = "OBS321_REAL_UID_AND_WAL_PASS"

PROBE = r"""
import os
from pathlib import Path
import sys
operation, raw_path = sys.argv[1:]
path = Path(raw_path)
try:
    if operation == "read":
        assert path.read_bytes().startswith(b"OBS321")
    elif operation == "write":
        with path.open("ab") as stream:
            stream.write(b"FORBIDDEN")
    elif operation == "list":
        os.listdir(path)
    elif operation == "lock":
        from obsidian_automation.production_io import canonical_io_lock_observer
        with canonical_io_lock_observer(path):
            assert (path / "24-Locks" / "canonical-io.lock").is_file()
    elif operation == "mkdir":
        (path / "owned-test").mkdir()
    else:
        raise RuntimeError("invalid fixture operation")
except PermissionError:
    sys.exit(23)
"""


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), text=True, capture_output=True, timeout=12,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(
            f"fixture command {args[0]} failed rc={result.returncode}: "
            f"{result.stdout[:1500]} {result.stderr[:1500]}"
        )
    return result


def _acl(path: Path, uid: int, rights: str, *, default: bool = False) -> None:
    record = f"{'d:' if default else ''}u:{uid}:{rights}"
    _run("setfacl", "-m", record, str(path))


def _directory(path: Path, *observers: tuple[int, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    for uid, rights in observers:
        _acl(path, uid, rights)


def _source(path: Path) -> None:
    path.write_bytes(b"OBS321 synthetic evidence only")
    path.chmod(0o600)
    _acl(path, OBS, "r--")


def _run_uid(
    uid: int, operation: str, path: Path, *, allowed: bool,
    package: Path,
) -> None:
    env = {
        "PYTHONPATH": str(package),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    result = subprocess.run(
        [
            "setpriv", "--bounding-set=-all",
            f"--reuid={uid}", f"--regid={uid}", "--clear-groups",
            sys.executable, "-c", PROBE, operation, str(path),
        ],
        env=env, text=True, capture_output=True, timeout=10,
        check=False,
    )
    expected = 0 if allowed else 23
    assert result.returncode == expected, (
        f"{operation} UID={uid} file={path.name} expected={expected}, "
        f"got {result.returncode}: {result.stderr[:900]} {result.stdout[:900]}"
    )


def _query_db(uid: int, db: Path, expected: int) -> None:
    script = """
import sqlite3, sys
from pathlib import Path
db = Path(sys.argv[1])
expect = int(sys.argv[2])
conn = sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True, timeout=2)
try:
    assert conn.execute("SELECT n FROM observations").fetchone()[0] == expect
    try:
        conn.execute("UPDATE observations SET n = 10000")
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("readonly SQLite connection allowed UPDATE")
finally:
    conn.close()
"""
    completed = subprocess.run(
        [
            "setpriv", "--bounding-set=-all",
            f"--reuid={uid}", f"--regid={uid}", "--clear-groups",
            sys.executable, "-c", script, str(db), str(expected),
        ],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, (
        f"observer SQLite WAL mode=ro failed: "
        f"{completed.stderr[:1100]} {completed.stdout[:500]}"
    )


def _test_in_namespace() -> None:
    print(CHILD_STARTED, flush=True)
    with tempfile.TemporaryDirectory(prefix="obs321-disposable-acl-", dir="/tmp") as temp:
        base = Path(temp)
        base.chmod(0o711)
        package = base / "code"
        module_dir = package / "obsidian_automation"
        module_dir.mkdir(parents=True, mode=0o755)
        package.chmod(0o755)
        module_dir.chmod(0o755)
        (module_dir / "__init__.py").write_text("", encoding="utf-8")
        shutil.copyfile(
            Path(__file__).resolve().with_name("production_io.py"),
            module_dir / "production_io.py",
        )
        (module_dir / "production_io.py").chmod(0o644)

        state = base / "state"
        _directory(state, (OBS, "--x"), (SYNC, "--x"), (SCHEDULER, "--x"))
        for name in ("00-Untrusted", "05-Context", "10-Validation", "17-Human-Projection-Result"):
            target = state / name
            _directory(target, (OBS, "r-x"))
            _source(target / "original.json")

        requests = state / "16-Human-Projection"
        _directory(requests, (OBS, "--x"))
        for role in ROLES:
            role_dir = requests / role
            _directory(role_dir, (OBS, "r-x"))
            _source(role_dir / "original.projection.json")

        orchestration = state / "02-Orchestration"
        _directory(orchestration, (OBS, "--x"), (SCHEDULER, "rwx"))
        os.chown(orchestration, SCHEDULER, SCHEDULER)
        for name in ("input-selections", "semantic-selections"):
            selected = orchestration / name
            _directory(selected, (OBS, "r-x"))
            _source(selected / "selection.json")

        locks = state / "24-Locks"
        _directory(locks, (OBS, "--x"))
        lock_file = locks / "canonical-io.lock"
        lock_file.touch(mode=0o600)
        lock_file.chmod(0o600)
        _acl(lock_file, OBS, "r--")

        secure_vault = state / "vault"
        _directory(secure_vault)
        (secure_vault / "secret.txt").write_bytes(b"OBS321 unrelated private vault")
        (secure_vault / "secret.txt").chmod(0o600)
        secret_config = base / "secrets"
        _directory(secret_config)
        (secret_config / "credential").write_bytes(b"OBS321 never expose")
        (secret_config / "credential").chmod(0o600)

        output_parent = state / "18-Rejection-Preflight"
        _directory(output_parent, (OBS, "--x"), (SYNC, "--x"))
        output = output_parent / "observer"
        _directory(output, (SYNC, "r-x"))
        os.chown(output, OBS, OBS)

        for item in (
            state / "00-Untrusted" / "original.json",
            state / "05-Context" / "original.json",
            state / "10-Validation" / "original.json",
            state / "17-Human-Projection-Result" / "original.json",
            orchestration / "input-selections" / "selection.json",
            orchestration / "semantic-selections" / "selection.json",
        ):
            _run_uid(OBS, "read", item, allowed=True, package=package)
            _run_uid(OBS, "write", item, allowed=False, package=package)
        for role in ROLES:
            _run_uid(
                OBS, "read", requests / role / "original.projection.json",
                allowed=True, package=package,
            )
        # A real producer with a separate UID must publish *future* files
        # with a readable effective ACL, not merely old files retrofitted by
        # the bootstrap script. Existing immutable sources use mode=0644.
        context_dir = state / "05-Context"
        _acl(context_dir, OBS, "r-x", default=True)
        os.chown(context_dir, SCHEDULER, SCHEDULER)
        future = context_dir / "new-context.json"
        _run(
            "setpriv", "--bounding-set=-all", f"--reuid={SCHEDULER}",
            f"--regid={SCHEDULER}", "--clear-groups",
            sys.executable, "-c",
            "from pathlib import Path; import os,sys; "
            "fd=os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644); "
            "os.write(fd,b'OBS321 new producer evidence'); os.close(fd)", str(future),
        )
        _run_uid(OBS, "read", future, allowed=True, package=package)
        _run_uid(OBS, "write", future, allowed=False, package=package)
        # Explicit chmod 0600 masks named ACL read; this is a danger when
        # applying a final chmod after named/default ACL provisioning.
        _run(
            "setpriv", "--bounding-set=-all", f"--reuid={SCHEDULER}",
            f"--regid={SCHEDULER}", "--clear-groups",
            sys.executable, "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).chmod(0o600)",
            str(future),
        )
        _run_uid(OBS, "read", future, allowed=False, package=package)
        _run(
            "setpriv", "--bounding-set=-all", f"--reuid={SCHEDULER}",
            f"--regid={SCHEDULER}", "--clear-groups",
            "setfacl", "-m", f"u:{OBS}:r--", str(future),
        )
        _run_uid(OBS, "read", future, allowed=True, package=package)
        _run_uid(OBS, "lock", state, allowed=True, package=package)
        for denied in (secure_vault / "secret.txt", secret_config / "credential"):
            _run_uid(OBS, "read", denied, allowed=False, package=package)
            _run_uid(SYNC, "read", denied, allowed=False, package=package)
        _run_uid(SYNC, "read", state / "05-Context" / "original.json", allowed=False, package=package)

        proof = output / "attested.json"
        _run_uid(OBS, "mkdir", output, allowed=True, package=package)
        # Do not let Sync author evidence: observer creates the proof, then
        # owns the named read-only ACL publication for Sync.
        _run(
            "setpriv", "--bounding-set=-all", f"--reuid={OBS}",
            f"--regid={OBS}", "--clear-groups",
            sys.executable, "-c",
            "from pathlib import Path; import sys; "
            "p=Path(sys.argv[1]); p.write_bytes(b'OBS321 immutable preflight proof'); "
            "p.chmod(0o600)", str(proof),
        )
        _acl(proof, SYNC, "r--")
        _run_uid(SYNC, "read", proof, allowed=True, package=package)
        _run_uid(SYNC, "write", proof, allowed=False, package=package)
        _run_uid(UNAUTHORIZED, "read", proof, allowed=False, package=package)
        _run_uid(UNAUTHORIZED, "lock", state, allowed=False, package=package)
        acl = _run("getfacl", "-cp", str(proof)).stdout
        assert "user:1002:r--" in acl
        assert "mask::r--" in acl
        # ACL grant changes the group-class permission bits in stat mode;
        # chmod 0600 after ACL installation would erase the Sync grant.
        assert proof.stat().st_mode & 0o040

        writer_code = """
import sqlite3, sys
conn=sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode=" + sys.argv[2])
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE observations(n INTEGER)")
conn.execute("INSERT INTO observations VALUES(1)")
print("WRITER_READY", flush=True)
for line in sys.stdin:
    if line.strip() == "update":
        conn.execute("UPDATE observations SET n=2")
        print("UPDATED", flush=True)
    if line.strip() == "stop":
        break
conn.close()
"""
        # Production currently uses DELETE journal mode; WAL is an explicit
        # stress condition because future schema changes must not silently
        # require the observer to write to the scheduler directory.
        for journal_mode in ("DELETE", "WAL"):
            db = orchestration / f"observations-{journal_mode.lower()}.sqlite3"
            writer = subprocess.Popen(
                [
                    "setpriv", "--bounding-set=-all", f"--reuid={SCHEDULER}",
                    f"--regid={SCHEDULER}", "--clear-groups",
                    sys.executable, "-u", "-c", writer_code, str(db), journal_mode,
                ],
                text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert writer.stdout is not None
                assert writer.stdout.readline().strip() == "WRITER_READY", (
                    f"separate-UID scheduler failed in {journal_mode}"
                )
                suffixes = ("", "-wal", "-shm") if journal_mode == "WAL" else ("",)
                for suffix in suffixes:
                    candidate = Path(str(db) + suffix)
                    assert candidate.is_file(), (
                        f"live {journal_mode} evidence missing {suffix}"
                    )
                    _acl(candidate, OBS, "r--")
                _query_db(OBS, db, 1)
                assert writer.stdin is not None
                writer.stdin.write("update\n")
                writer.stdin.flush()
                assert writer.stdout.readline().strip() == "UPDATED"
                _query_db(OBS, db, 2)
            finally:
                if writer.poll() is None:
                    writer.terminate()
                try:
                    writer.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    writer.kill()
                    writer.communicate(timeout=3)
        # The disposable fixtures have no authority to modify the production
        # implementation or host namespaces and are destroyed at test exit.
    print(CHILD_PASSED, flush=True)


def test_observer_real_uid_acl_and_sqlite_wal_in_disposable_namespace() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux-only real UID / ACL test")
    if not all(shutil.which(name) for name in ("unshare", "setpriv", "getfacl", "setfacl")):
        pytest.skip("required Linux user-namespace/ACL utilities unavailable")
    # In a subordinate UID mapping the developer's home directory may not
    # be traversable. Copy only this test and the pure lock module into a
    # disposable /tmp directory, never chmod or expose the source checkout.
    with tempfile.TemporaryDirectory(prefix="obs321-ns-source-", dir="/tmp") as location:
        source = Path(location)
        source.chmod(0o755)
        script = source / Path(__file__).name
        shutil.copyfile(Path(__file__).resolve(), script)
        script.chmod(0o644)
        module = source / "production_io.py"
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "src"
            / "obsidian_automation" / "production_io.py", module,
        )
        module.chmod(0o644)
        command = [
            "unshare", "--map-auto", "--setuid", "0", "--setgid", "0",
            "--", sys.executable, str(script), "--fixture-child",
        ]
        # A failed scheduler/writer inside a namespace must not leave orphan
        # test processes running. Kill the entire disposable process group
        # on timeout, not just the parent unshare helper.
        process = subprocess.Popen(
            command, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            pytest.fail("disposable user-namespace ACL fixture timed out")
        assert process.returncode is not None
        result = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr,
        )
    if (
        result.returncode != 0 and CHILD_STARTED not in result.stdout
        and ("unshare:" in result.stderr or "newuidmap:" in result.stderr)
    ):
        pytest.skip("rootless subordinate user namespace unsupported on this runner")
    assert result.returncode == 0, (
        f"disposable real UID/ACL fixture failed: "
        f"{result.stdout[-1200:]} {result.stderr[-3500:]}"
    )
    assert CHILD_PASSED in result.stdout


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--fixture-child":
        _test_in_namespace()
    else:
        raise SystemExit("fixture test child mode only")
