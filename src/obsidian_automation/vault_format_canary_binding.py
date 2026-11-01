"""One-file, operator-bound adapter for the existing guarded Formatter Writer.

**Not wired into snapshot_app, the WebUI, or any scheduler.** In particular,
ordinary deployment still constructs FormatterJob(writer=None).

This source-only adapter prepares a future, individually approved 98-System
canary. It does not mint an approval, attest Nextcloud on its own, or create a
credential/receipt file. The operator supplies a human-reviewed immutable
grant, a digest of separate real-provider proof, and a private receipt root.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from urllib.parse import quote, urlsplit

from .core_promotion_transport import HTTPTransport, _real_http_request
from .snapshot_setup import validate_nextcloud
from .vault_format_writer import (
    FormatWriteError,
    _binding,
    _target,
    _validated_plan,
    apply_plan,
)

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_OPERATION = re.compile(r"[a-f0-9]{32}\Z")
_GENERATION_FIELDS = (
    "webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password",
)


@dataclass(frozen=True)
class CanaryGrant:
    """Explicit one-file, one-plan, one-operation future operator approval.

    No field is itself a human authorization; there must also be a separate
    approved Canary record before this object is constructed/invoked.
    The provider_proof_sha256 is a reference to an independently accepted
    private proof, not automatic cryptographic validation of that proof.
    """

    target_path: str
    plan_sha256: str
    before_sha256: str
    after_sha256: str
    source_generation: str
    account_binding_sha256: str
    provider_proof_sha256: str
    operation_id: str
    receipt_root: Path


def _grant(grant: CanaryGrant) -> None:
    if not isinstance(grant, CanaryGrant):
        raise FormatWriteError("invalid_canary_grant")
    for field in (
        "plan_sha256", "before_sha256", "after_sha256",
        "source_generation", "account_binding_sha256", "provider_proof_sha256",
    ):
        value = getattr(grant, field)
        if type(value) is not str or _SHA.fullmatch(value) is None:
            raise FormatWriteError("invalid_canary_grant")
    if grant.before_sha256 == grant.after_sha256:
        raise FormatWriteError("invalid_canary_grant")
    if type(grant.operation_id) is not str or _OPERATION.fullmatch(grant.operation_id) is None:
        raise FormatWriteError("invalid_canary_grant")
    if type(grant.target_path) is not str or _target(grant.target_path) != grant.target_path:
        raise FormatWriteError("invalid_canary_grant")
    if (not isinstance(grant.receipt_root, Path)
            or not grant.receipt_root.is_absolute()
            or ".." in grant.receipt_root.parts):
        raise FormatWriteError("invalid_canary_grant")
    # Writer's private dirfd/no-follow checks still apply at effect time.


def _generation(details: dict) -> str:
    """Match FormatterSource generation without logging private credentials."""
    encoded = json.dumps(
        {key: details[key] for key in _GENERATION_FIELDS},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _vault_root(details: dict) -> str:
    """Require a canonical, non-ambiguous Nextcloud account-root URL."""
    try:
        details = validate_nextcloud(details)
        parsed = urlsplit(details["webdav_url"])
        username = details["nextcloud_user"]
        if (username in {".", ".."} or any(char in username for char in ("/", chr(92), "%"))
                or any(ord(char) < 32 or ord(char) == 127 for char in username)
                or details["vault_folder"] != "ObsidianVault"
                or parsed.scheme != "https"
                or parsed.query or parsed.fragment
                or "?" in details["webdav_url"] or "#" in details["webdav_url"]
                or parsed.username or parsed.password):
            raise FormatWriteError("canary_scope_mismatch")
        expected = "/remote.php/dav/files/" + quote(username, safe="") + "/"
        if parsed.path != expected:
            # No dot-segment, encoded separator, duplicate-slash, proxy
            # spelling, or unusual account-root suffix normalization.
            raise FormatWriteError("canary_scope_mismatch")
        return details["webdav_url"].rstrip("/") + "/ObsidianVault"
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise FormatWriteError("canary_scope_mismatch") from None


class BoundedCanaryWriter:
    """Callable shaped for FormatterJob(writer=...), but NOT normally injected.

    There is no path, credential, authorization bit, or PUT payload supplied
    by the WebUI. Every invocation requires the same preapproved plan and
    exact file/hash/generation/account binding and fixed operation ID.
    """

    def __init__(
        self,
        shared,
        source_generation: Callable[[], str | None],
        grant: CanaryGrant,
        *,
        proof_verifier: Callable[[str, str, str], bool] | None = None,
        transport: HTTPTransport | None = None,
    ) -> None:
        _grant(grant)
        if not callable(getattr(shared, "load_nextcloud", None)) or not callable(source_generation):
            raise FormatWriteError("canary_source_unavailable")
        self._shared = shared
        self._source_generation = source_generation
        self._grant = grant
        # Normal app supplies neither a Writer nor this independent proof
        # verifier. Merely constructing a syntactically valid grant cannot
        # activate remote writes.
        self._proof_verifier = proof_verifier
        self._transport = transport

    def __call__(self, plan: dict) -> dict:
        # Take an independent JSON snapshot before grant admission. The caller
        # retains a mutable Plan dict and may mutate it (even while a trusted
        # verifier runs). Never pass that mutable reference to the CAS Writer.
        # A racing mutation must either yield a valid approved snapshot or
        # fail closed; the snapshot cannot be changed after admission.
        grant = self._grant
        try:
            frozen_plan = json.loads(json.dumps(
                plan, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ))
        except (TypeError, ValueError, UnicodeError, RuntimeError, RecursionError, OverflowError):
            raise FormatWriteError("canary_plan_snapshot_invalid") from None
        plan_sha, entries = _validated_plan(frozen_plan)
        if plan_sha != grant.plan_sha256:
            raise FormatWriteError("canary_plan_mismatch")
        item = entries.get(grant.target_path)
        if (item is None or item.get("status") != "would_change"
                or item.get("before_sha256") != grant.before_sha256
                or item.get("after_sha256") != grant.after_sha256
                or frozen_plan.get("counts", {}).get("error", 0) != 0):
            raise FormatWriteError("canary_target_mismatch")

        # Hold the shared SetupStore's in-process RLock across proof checking
        # and the complete Writer call, including the conditional PUT. This
        # serializes normal in-process credential updates. A future separate
        # canary operator must ALSO quiesce other credential-writing processes
        # and independently attest the service/state-generation boundary.
        mutex = getattr(self._shared, "_lock", None)
        if not callable(getattr(mutex, "__enter__", None)):
            raise FormatWriteError("canary_configuration_lock_missing")
        with mutex:
            if self._source_generation() != grant.source_generation:
                raise FormatWriteError("canary_source_generation_changed")
            details = self._shared.load_nextcloud()
            if type(details) is not dict:
                raise FormatWriteError("canary_connection_missing")
            try:
                base_url = _vault_root(details)
                generation = _generation(details)
                binding = _binding(base_url, details["nextcloud_user"])
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise FormatWriteError("canary_connection_invalid") from None
            if generation != grant.source_generation:
                raise FormatWriteError("canary_source_generation_changed")
            if binding != grant.account_binding_sha256:
                raise FormatWriteError("canary_account_binding_changed")

            # The proof digest is not self-authenticating. Human-approved
            # evidence verification is a distinct injected authority, not a
            # user-supplied boolean accepted by the normal app.
            verifier = self._proof_verifier
            if verifier is None:
                raise FormatWriteError("canary_provider_proof_unverified")
            try:
                approved = verifier(
                    grant.provider_proof_sha256,
                    grant.source_generation,
                    grant.account_binding_sha256,
                )
            except Exception:
                raise FormatWriteError("canary_provider_proof_unverified") from None
            if approved is not True:
                raise FormatWriteError("canary_provider_proof_unverified")

            def assert_generation() -> None:
                """Fence any observed epoch change before each HTTP effect."""
                if self._source_generation() != generation:
                    raise FormatWriteError("canary_source_generation_changed")
                current = self._shared.load_nextcloud()
                if type(current) is not dict:
                    raise FormatWriteError("canary_connection_missing")
                try:
                    current_base = _vault_root(current)
                    current_gen = _generation(current)
                    current_binding = _binding(current_base, current["nextcloud_user"])
                except (ValueError, TypeError, KeyError, UnicodeError):
                    raise FormatWriteError("canary_connection_invalid") from None
                if current_gen != generation:
                    raise FormatWriteError("canary_source_generation_changed")
                if current_binding != binding:
                    raise FormatWriteError("canary_account_binding_changed")

            # A malicious/defective verifier must not be able to rotate
            # credentials after admission and before the first network call.
            assert_generation()
            delegate = self._transport or _real_http_request

            def guarded_transport(**kwargs):
                # Each GET, conditional PUT, and readback receives a fresh
                # credential-generation check. No request uses new credentials
                # combined with the old grant.
                assert_generation()
                return delegate(**kwargs)

            return apply_plan(
                frozen_plan,
                selected_paths=(grant.target_path,),
                enabled=True,
                cas_server_verified=True,
                receipt_dir=grant.receipt_root,
                base_url=base_url,
                username=details["nextcloud_user"],
                password=details["nextcloud_password"],
                transport=guarded_transport,
                operation_id=grant.operation_id,
            )
