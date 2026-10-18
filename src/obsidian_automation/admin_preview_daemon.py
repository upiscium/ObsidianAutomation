"""Separate, root-only local PREVIEW daemon for the #300 Broker split.

NOT installed or started by this repository. The process has no mutating
handler, never constructs OperationBroker and cannot run systemctl actions.
Production installation requires independent service-identity, OS ACL and
systemd unit/revision review.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import pwd
import signal
import subprocess
import threading
from typing import Callable, Mapping, Sequence

from .admin_broker_socket import DEFAULT_LOCAL_BROKER_SOCKET
from .admin_operation_preview import OperationPreviewSocket
from .admin_operation_broker import BrokerRejected, SystemctlAdapter
from .admin_status import SERVICES, aggregate_status

WEB_SERVICE_USER = "obsidian-admin-web"


def _production_observation(service_id: str | None = None) -> dict[str, object]:
    """Fixed, bounded systemd property reads, never client-provided state.

    Reuse the existing read-model classification while replacing its ordinary
    subprocess.run capture_output runner with the audited streaming bound from
    SystemctlAdapter (32 KiB and at most three seconds per fixed unit).
    """
    adapter = SystemctlAdapter()
    allowed = frozenset(
        unit for spec in SERVICES for unit in (spec.timer, spec.service)
    )

    def bounded_runner(argv, *, check, capture_output, text, timeout):
        if (
            type(argv) is not list or len(argv) < 5
            or argv[:2] != ["systemctl", "show"]
            or argv[2] not in allowed
            or type(timeout) is not int or not 0 < timeout <= 3
            or check is not False or capture_output is not True
            or text is not True
        ):
            return subprocess.CompletedProcess(argv, 1, "", "")
        props = tuple(
            item.split("=", 1)[1] for item in argv[4:]
            if type(item) is str and item.startswith("--property=")
        )
        if len(props) != len(argv)-4:
            return subprocess.CompletedProcess(argv, 1, "", "")
        try:
            values = adapter._read_properties(argv[2], props)
        except (BrokerRejected, ValueError, OSError):
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(
            argv, 0, "".join(f"{k}={values[k]}\n" for k in props), "",
        )

    services = aggregate_status(
        runner=bounded_runner,
        service_ids=(service_id,) if service_id is not None else None,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "services": services,
    }


def serve_preview(
    *, path: Path,
    allowed_peer_uid: int,
    allowed_peer_gid: int,
    stop: threading.Event,
    observation_provider: Callable[[], Mapping[str, object]] = _production_observation,
    require_root: bool = True,
) -> None:
    """Serve capabilities and no-effect previews until graceful shutdown."""
    if (
        type(allowed_peer_uid) is not int or allowed_peer_uid <= 0
        or type(allowed_peer_gid) is not int or allowed_peer_gid <= 0
    ):
        raise ValueError("nonroot_web_service_identity_required")
    with OperationPreviewSocket(
        path, allowed_peer_uid=allowed_peer_uid,
        allowed_peer_gid=allowed_peer_gid,
        socket_gid=allowed_peer_gid,
        require_root=require_root,
        observation_provider=observation_provider,
    ) as server:
        while not stop.is_set():
            server.serve_once()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local, non-effectful operation-preview broker (not installed)",
    )
    parser.add_argument("--web-user", default=WEB_SERVICE_USER)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if os.geteuid() != 0:
        raise SystemExit("privileged_preview_daemon_required")
    # Resolve an actual host-owned Unix identity; do not allow a browser or
    # arbitrary request to supply a peer UID. Refuse service account root.
    try:
        principal = pwd.getpwnam(args.web_user)
    except (KeyError, ValueError):
        raise SystemExit("web_service_identity_missing") from None
    if principal.pw_uid <= 0 or principal.pw_gid <= 0:
        raise SystemExit("nonroot_web_service_identity_required")

    stop = threading.Event()
    def ask_stop(_signum, _frame):
        stop.set()
    signal.signal(signal.SIGTERM, ask_stop)
    signal.signal(signal.SIGINT, ask_stop)
    os.umask(0o077)
    serve_preview(
        path=DEFAULT_LOCAL_BROKER_SOCKET,
        allowed_peer_uid=principal.pw_uid,
        allowed_peer_gid=principal.pw_gid,
        stop=stop,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
