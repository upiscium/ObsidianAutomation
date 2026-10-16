"""Disposable real root-vs-nobody AF_UNIX peer authorization proof.

Run explicitly with root on an authorized Linux test host. Never installs,
starts/stops/reloads a systemd unit or writes production directories. Creates
an unpredictable private directory under /run and removes it afterward.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading

from obsidian_automation.admin_control_queue_socket import OperationQueueSocket
from obsidian_automation.admin_operation_queue import DurableOperationQueue

RID = "12345678-1234-4234-8234-123456789abc"

RAW_CLIENT = r"""
import json,socket,struct,sys
path=sys.argv[1]
body=json.dumps({
    "version":1,"op":"submit","request_id":"12345678-1234-4234-8234-123456789abc",
    "service_id":"github-sync","action":"pause",
    "confirmation":"operator_confirmed_v1"
}).encode()
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
    s.settimeout(3)
    s.connect(path)
    s.sendall(struct.pack("!I",len(body))+body)
    data=s.recv(4)
    if len(data)!=4:
        raise SystemExit("no_broker_reply")
    size=struct.unpack("!I",data)[0]
    if not 1<=size<=2048: raise SystemExit("bad_frame")
    content=bytearray()
    while len(content)<size:
        item=s.recv(size-len(content))
        if not item: raise SystemExit("truncated_reply")
        content.extend(item)
    value=json.loads(content)
    assert value=={
        "version":1,"request_id":"12345678-1234-4234-8234-123456789abc",
        "service_id":"github-sync","action":"pause","state":"accepted",
        "authority":"durable_queue_only","can_execute":False
    },value
print("NONROOT_PEER_ACCEPTED")
"""

def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("root_required_for_disposable_crossuid_test")
    principal = pwd.getpwnam("nobody")
    if principal.pw_uid == 0 or principal.pw_gid == 0:
        raise RuntimeError("invalid_nobody_identity")
    temp = Path(tempfile.mkdtemp(prefix="obsidian-queue-crossuid-", dir="/run"))
    if not temp.name.startswith("obsidian-queue-crossuid-"):
        raise RuntimeError("unexpected_disposable_location")
    try:
        os.chown(temp, 0, principal.pw_gid)
        temp.chmod(0o710)
        queue_root = temp / "private-queue"
        queue_root.mkdir(mode=0o700)
        path = temp / "control.sock"
        store = DurableOperationQueue(queue_root=queue_root, enabled=True)
        with OperationQueueSocket(
            path,
            queue=store,
            allowed_peer_uid=principal.pw_uid,
            allowed_peer_gid=principal.pw_gid,
            socket_gid=principal.pw_gid,
        ) as listener:
            # Linux checks both pathname ACL and SO_PEERCRED. The actual
            # nonroot peer launches in another OS process, not a monkeypatch.
            output: list[subprocess.CompletedProcess] = []
            def peer():
                output.append(subprocess.run(
                    ["sudo", "-n", "-u", "nobody", sys.executable, "-c",
                     RAW_CLIENT, str(path)],
                    check=False, capture_output=True, text=True, timeout=5,
                    env={"PATH": "/run/current-system/sw/bin:/usr/bin:/bin"},
                ))
            t = threading.Thread(target=peer)
            t.start()
            assert listener.serve_once()
            t.join(timeout=6)
            assert not t.is_alive()
            assert len(output) == 1
            if output[0].returncode != 0:
                raise RuntimeError("nonroot_peer_did_not_complete")
            assert "NONROOT_PEER_ACCEPTED" in output[0].stdout
            assert store.receipt(RID)["state"] == "accepted"
            # Same socket rejects an untrusted root peer: authorization is
            # fixed to the particular nonroot Web service identity.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rogue:
                rogue.settimeout(2)
                rogue.connect(str(path))
                rogue.sendall(struct.pack("!I", len(b'{"version":1,"op":"capabilities"}')) +
                              b'{"version":1,"op":"capabilities"}')
                assert listener.serve_once()
                try:
                    response = rogue.recv(4)
                except (ConnectionResetError, BrokenPipeError):
                    response = b""
                assert response == b""
        assert not path.exists()
        assert (queue_root / "active.json").exists()
        print("CROSSUID_ROOT_TO_NONROOT_PEER_ACCEPTED; WRONG_PEER_REFUSED; NO_EFFECTS")
    finally:
        # Only remove the exact newly created /run test directory.
        if (
            temp.parent != Path("/run")
            or not temp.name.startswith("obsidian-queue-crossuid-")
            or temp.is_symlink()
        ):
            raise RuntimeError("unsafe_test_cleanup_path")
        shutil.rmtree(temp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
