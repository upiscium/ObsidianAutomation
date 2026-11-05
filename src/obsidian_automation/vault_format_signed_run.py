"""R2/B2a: verify EXTERNALLY signed per-run approval; never issue approvals.

Unwired source-only verifier. Its signer private key MUST be kept outside the
Formatter worker and its accepted signer list controlled by a separate OS
identity. Ordinary Scheduler rows and boolean callbacks are not human grants.
This module does not make network requests, produce a key or call the Writer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from dataclasses import replace
from .vault_format_recurring_admission import RecurringPolicy, _policy_digest
from typing import Callable

from .vault_formatter import POLICY_VERSION, SCOPE

_RUN = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,128}\Z")
_NAMESPACE = "obsidian-vault-formatter-run"
_FIELDS = frozenset({
    "version", "run_id", "plan_sha256", "policy_sha256", "operation_id",
    "source_generation", "account_binding_sha256", "provider_proof_sha256",
    "receipt_root_sha256", "scope", "policy_version", "issued_at", "expires_at",
})
_MAX_GRANT = 4096
_MAX_SIG = 8192
_MAX_SIGNERS = 16384
_MAX_LIFETIME = 86400


class SignedRunDenied(ValueError):
    """Static error only: never include signer output or Vault data."""


def receipt_root_digest(path: Path) -> str:
    if (not isinstance(path, Path) or not path.is_absolute()
            or ".." in path.parts or str(path) == "/"):
        raise SignedRunDenied("invalid_receipt_identity")
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _secure_directory(path: Path, expected_uid: int) -> int:
    if (not isinstance(path, Path) or not path.is_absolute()
            or ".." in path.parts or type(expected_uid) is not int
            or expected_uid < 0):
        raise SignedRunDenied("invalid_authority_root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = -1
    transfer = False
    try:
        fd = os.open("/", flags)
        for name in path.parts[1:]:
            parent = os.fstat(fd)
            if (parent.st_uid not in (0, expected_uid)
                    or (parent.st_mode & 0o022)
                    and not (parent.st_uid == 0 and parent.st_mode & stat.S_ISVTX)):
                raise SignedRunDenied("unsafe_authority_ancestor")
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid
                or info.st_mode & 0o022):
            raise SignedRunDenied("unsafe_authority_root")
        transfer = True
        return fd
    except SignedRunDenied:
        raise
    except OSError:
        raise SignedRunDenied("unavailable_authority_root") from None
    finally:
        if fd >= 0 and not transfer:
            os.close(fd)


def _checked_read(directory_fd: int, name: str, limit: int, owner: int):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = -1
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != owner
                or meta.st_nlink != 1 or meta.st_mode & 0o022
                or meta.st_size < 1 or meta.st_size > limit):
            raise SignedRunDenied("unsafe_authority_file")
        # The fd is pinned. Use the very same open fd for the signature
        # verifier, instead of reopening a replaceable path.
        raw = os.read(fd, limit + 1)
        if not raw or len(raw) > limit or len(raw) != meta.st_size:
            raise SignedRunDenied("invalid_authority_file")
        return raw, fd
    except SignedRunDenied:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            os.close(fd)
        raise SignedRunDenied("unavailable_authority_file") from None


class SignedRunVerifier:
    """Read-only SSHSIG grant validator bound to one approved receipt root.

    A real deployment MUST have a separately owned, non-worker-writable trust
    directory with allowed_signers and detached grant/signature files. Passing
    a test-owned directory does not establish production trust. No signer or
    approval generator is provided by this class.
    """

    def __init__(
        self, authority_root: Path, receipt_root: Path, *,
        expected_policy: RecurringPolicy,
        signer_identity: str, trusted_owner_uid: int,
        ssh_keygen: Path = Path("/run/current-system/sw/bin/ssh-keygen"),
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(signer_identity) is not str or _SIGNER.fullmatch(signer_identity) is None:
            raise SignedRunDenied("invalid_signer_identity")
        if (not isinstance(ssh_keygen, Path) or not ssh_keygen.is_absolute()
                or not callable(clock)):
            raise SignedRunDenied("invalid_verifier_configuration")
        # Never allow the worker to replace ssh-keygen with an arbitrary
        # executable that simply exits successfully. Nix store binaries and
        # system OpenSSH are owned by root and not group/other writable.
        try:
            resolved_binary = ssh_keygen.resolve(strict=True)
            verifier_binary = resolved_binary.stat()
            ancestor = resolved_binary.parent
            while True:
                info = ancestor.stat()
                # /nix/store may be root-owned group-writable with the
                # sticky bit. Unprivileged users cannot replace an existing
                # root-owned store entry in that case.
                if (info.st_uid != 0 or
                        (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                    raise SignedRunDenied("untrusted_signature_verifier")
                if ancestor == ancestor.parent:
                    break
                ancestor = ancestor.parent
        except SignedRunDenied:
            raise
        except OSError:
            raise SignedRunDenied("untrusted_signature_verifier") from None
        if (not stat.S_ISREG(verifier_binary.st_mode)
                or verifier_binary.st_uid != 0
                or verifier_binary.st_mode & 0o022
                or not verifier_binary.st_mode & 0o111):
            raise SignedRunDenied("untrusted_signature_verifier")
        # Pin the complete expected policy once, independently of any
        # caller-owned dataclass that may be mutated later.
        if type(expected_policy) is not RecurringPolicy:
            raise SignedRunDenied("invalid_expected_policy")
        pinned = replace(expected_policy)
        try:
            self.policy_sha256 = _policy_digest(pinned)
        except ValueError:
            raise SignedRunDenied("invalid_expected_policy") from None
        self.source_generation = pinned.source_generation
        self.account_binding_sha256 = pinned.account_binding_sha256
        self.provider_proof_sha256 = pinned.provider_proof_sha256
        self.authority_root = authority_root
        self.receipt_hash = receipt_root_digest(receipt_root)
        self.signer_identity = signer_identity
        self.trusted_owner_uid = trusted_owner_uid
        self.ssh_keygen = resolved_binary
        self.clock = clock
        fd = _secure_directory(authority_root, trusted_owner_uid)
        os.close(fd)

    def verify(
        self, run_id: str, plan_sha256: str, policy_sha256: str, operation_id: str,
    ) -> bool:
        if (type(run_id) is not str or _RUN.fullmatch(run_id) is None
                or type(operation_id) is not str or _RUN.fullmatch(operation_id) is None
                or any(type(x) is not str or _SHA.fullmatch(x) is None
                       for x in (plan_sha256, policy_sha256))):
            raise SignedRunDenied("invalid_signed_run_request")
        fd = _secure_directory(self.authority_root, self.trusted_owner_uid)
        opened = []
        try:
            for name, limit in (
                ("allowed_signers", _MAX_SIGNERS),
                (run_id + ".json", _MAX_GRANT),
                (run_id + ".sshsig", _MAX_SIG),
            ):
                raw, handle = _checked_read(fd, name, limit, self.trusted_owner_uid)
                opened.append((raw, handle))
            # A revoked run remains denied even if the signed original
            # grant remains valid. A privileged stop command owns this marker.
            try:
                os.stat(run_id + ".revoked", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise SignedRunDenied("signed_run_revocation_unavailable") from None
            else:
                raise SignedRunDenied("signed_run_revoked")

            raw_grant = opened[1][0]
            try:
                grant = json.loads(raw_grant)
                encoded = json.dumps(
                    grant, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")
            except (ValueError, TypeError, UnicodeError):
                raise SignedRunDenied("invalid_signed_run") from None
            if type(grant) is not dict or encoded != raw_grant or set(grant) != _FIELDS:
                raise SignedRunDenied("invalid_signed_run")
            if (type(grant["version"]) is not int or grant["version"] != 1
                    or type(grant["run_id"]) is not str or grant["run_id"] != run_id
                    or type(grant["operation_id"]) is not str
                    or grant["operation_id"] != operation_id
                    or grant["plan_sha256"] != plan_sha256
                    or grant["policy_sha256"] != policy_sha256
                    or policy_sha256 != self.policy_sha256
                    or grant["source_generation"] != self.source_generation
                    or grant["account_binding_sha256"] != self.account_binding_sha256
                    or grant["provider_proof_sha256"] != self.provider_proof_sha256
                    or grant["receipt_root_sha256"] != self.receipt_hash
                    or type(grant["scope"]) is not str or grant["scope"] != SCOPE
                    or type(grant["policy_version"]) is not str
                    or grant["policy_version"] != POLICY_VERSION
                    or any(type(grant[x]) is not str or _SHA.fullmatch(grant[x]) is None
                           for x in ("source_generation", "account_binding_sha256",
                                     "provider_proof_sha256", "receipt_root_sha256"))):
                raise SignedRunDenied("signed_run_binding_mismatch")
            issued, expires = grant["issued_at"], grant["expires_at"]
            if (type(issued) is not int or type(expires) is not int
                    or not 0 < issued < expires or expires - issued > _MAX_LIFETIME):
                raise SignedRunDenied("invalid_signed_run_window")
            try:
                now = self.clock()
            except Exception:
                raise SignedRunDenied("signed_run_clock_unavailable") from None
            if type(now) not in (int, float) or not issued <= now < expires:
                raise SignedRunDenied("signed_run_outside_window")

            allowed_fd = opened[0][1]
            signature_fd = opened[2][1]
            args = [
                str(self.ssh_keygen), "-Y", "verify",
                "-f", f"/proc/self/fd/{allowed_fd}",
                "-I", self.signer_identity, "-n", _NAMESPACE,
                "-s", f"/proc/self/fd/{signature_fd}",
            ]
            try:
                result = subprocess.run(
                    args, input=raw_grant,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False, pass_fds=(allowed_fd, signature_fd),
                    env={"PATH": "/run/current-system/sw/bin:/usr/bin:/bin", "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired):
                raise SignedRunDenied("signed_run_signature_unavailable") from None
            if result.returncode != 0:
                raise SignedRunDenied("signed_run_signature_invalid")
            return True
        finally:
            for _, handle in opened:
                os.close(handle)
            os.close(fd)

    def __call__(self, run_id: str, plan_sha256: str,
                 policy_sha256: str, operation_id: str) -> bool:
        try:
            return self.verify(run_id, plan_sha256, policy_sha256, operation_id)
        except SignedRunDenied:
            return False
