"""#303 V1/A-gate: bind the saved shared Nextcloud account (enrollment).

One read-only ``PROPFIND Depth:0`` of the account's DAV files root records:
- the instance ID (suffix of ``oc:id``), so a different Nextcloud behind the
  same URL is refused later;
- the account root's ``oc:id``, the trusted container for every walk;
- the authenticated user ID from OCS ``cloud/user``, which must equal the user
  segment of the saved ``/remote.php/dav/files/<user>/`` URL and the saved login.

P3-A (Nextcloud 35.0.2, 2026-11-13): ``DAV:current-user-principal`` is empty
on a user's own files root, so it cannot bind the account. A PROPFIND of
another user's files root returns a 207 stub without ``oc:fileid``/``oc:id``,
which this parser rejects.

The record is private (0600), holds no secret and is valid only for the saved
WebDAV URL + user it was made under. Like every provider read in #303 it is
refused before any I/O unless the proof register admits it. Nothing here is
wired into the running app; the picker/activation stay default OFF.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .snapshot_setup import SetupStore, _atomic
from .vault_root_activation import (
    _origin, _pin, _read_binding, _source_digest, read_private_json,
)
from .vault_root_dav_fixture import DAV, OC, _ACCOUNT, _Blocked, _canonical_href, _parse_xml
from .vault_root_dav_verifier import (
    CURRENT_PROVIDER_PROOF, NC, PER_CALL_TIMEOUT_S, DavResponse, DavTransport,
    DavTransportError, ProviderProof, TrustedDavAccount, read_root_status,
)
from .vault_root_dav_adapter import MAX_PAGE_BYTES
from .vault_root_preview import _identifier
from .vault_root_selection import TrustedRootContext

ENROLLMENT_FILE = "vault_root_account.json"
ENROLL_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"'
    b' xmlns:nc="http://nextcloud.org/ns"><d:prop>'
    b"<d:resourcetype/><oc:fileid/><oc:id/><nc:mount-type/></d:prop></d:propfind>"
)
_INSTANCE = re.compile(r"[a-z0-9]{3,64}\Z", re.ASCII)
_USER_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z", re.ASCII)
MAX_OCS_BYTES = 64 * 1024


@dataclass(frozen=True)
class AccountEnrollment:
    instance_id: str
    root_resource_id: str
    user_id: str
    source_sha256: str


def _account_user(prefix: str) -> str:
    return unquote(prefix.rstrip("/").rsplit("/", 1)[-1])


def _parse_ocs_user(body: bytes) -> str:
    """OCS v1 ``cloud/user`` JSON: ``ocs.meta.statuscode == 100`` and a
    plain ``ocs.data.id``. Anything else is ``account_unverified``."""
    try:
        data = json.loads(body.decode("utf-8"))
        meta, payload = data["ocs"]["meta"], data["ocs"]["data"]
        status, user = meta["statuscode"], payload["id"]
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise _Blocked("account_unverified") from None
    if type(status) is not int or status != 100 or type(user) is not str \
            or _USER_ID.fullmatch(user) is None:
        raise _Blocked("account_unverified")
    return user


def _parse_enrollment(body: bytes, prefix: str) -> tuple[str, str]:
    rows = _parse_xml(body)
    if len(rows) != 1:
        raise _Blocked("account_unverified")
    row = rows[0]
    hrefs = row.findall(DAV + "href")
    if len(hrefs) != 1 or _canonical_href(hrefs[0].text) != _canonical_href(prefix):
        raise _Blocked("account_unverified")
    ok = [ps for ps in row.findall(DAV + "propstat")
          if (ps.findtext(DAV + "status") or "") == "HTTP/1.1 200 OK"]
    if len(ok) != 1 or len(ok[0].findall(DAV + "prop")) != 1:
        raise _Blocked("account_unverified")
    prop = ok[0].find(DAV + "prop")

    def one(tag: str) -> ET.Element:
        found = prop.findall(tag)
        if len(found) != 1:
            raise _Blocked("account_unverified")
        return found[0]
    types = [child.tag for child in one(DAV + "resourcetype")]
    if types != [DAV + "collection"] or (one(NC + "mount-type").text or "").strip():
        raise _Blocked("account_unverified")
    fileid, resource_id = one(OC + "fileid").text or "", one(OC + "id").text or ""
    padded = fileid.zfill(8)
    if (not fileid.isdecimal() or not resource_id.startswith(padded)
            or not _identifier(resource_id)):
        raise _Blocked("identity_unverified")
    instance = resource_id[len(padded):]
    if _INSTANCE.fullmatch(instance) is None:
        raise _Blocked("identity_unverified")
    return instance, resource_id


def _ok(response: object, status: int, limit: int) -> bool:
    return (type(response) is DavResponse and response.redirected is False
            and response.status == status and type(response.body) is bytes
            and len(response.body) <= limit)


def enroll_account(
    store: SetupStore,
    transport_factory: Callable[[dict], DavTransport],
    *,
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.time,
) -> tuple[str, AccountEnrollment | None]:
    """Returns (``enrolled`` | denial reason, record). Refuses without I/O
    while the register does not admit provider reads."""
    if type(proof) is not ProviderProof or not proof.admits(restricted_coherence=True):
        return "provider_unproven", None
    saved = store.load_nextcloud()
    if saved is None:
        return "binding_unavailable", None
    origin, prefix = _origin(saved["webdav_url"]), urlsplit(saved["webdav_url"]).path
    if _ACCOUNT.fullmatch(prefix) is None:
        return "account_unverified", None
    # The transport carries the SAVED credentials, so the OCS user check binds
    # the login actually used to the URL's user segment.
    transport = transport_factory(saved)
    if not callable(getattr(transport, "ocs_current_user", None)):
        return "provider_unavailable", None  # cannot bind the account
    try:
        response = transport.propfind(origin, prefix, "0", ENROLL_BODY,
                                      timeout=PER_CALL_TIMEOUT_S, max_bytes=MAX_PAGE_BYTES)
        if not _ok(response, 207, MAX_PAGE_BYTES):
            return "provider_unavailable", None
        instance, root_id = _parse_enrollment(response.body, prefix)
        who = transport.ocs_current_user(origin, timeout=PER_CALL_TIMEOUT_S,
                                         max_bytes=MAX_OCS_BYTES)
        if not _ok(who, 200, MAX_OCS_BYTES):
            return "provider_unavailable", None
        user = _parse_ocs_user(who.body)
    except DavTransportError:
        return "provider_unavailable", None
    except _Blocked as err:
        return err.code, None
    if user != _account_user(prefix) or user != saved["nextcloud_user"]:
        return "account_mismatch", None  # V1-S1: login name must equal user ID
    record = AccountEnrollment(instance, root_id, user, _source_digest(saved))
    existing = load_enrollment(store)
    if existing is not None and (existing.instance_id, existing.root_resource_id) != (
            instance, root_id):
        # Same URL + user now answers as another instance or account root
        # (restore, migration, DNS). Never accept that silently.
        return "instance_changed", None
    try:
        _atomic(store.state / ENROLLMENT_FILE, json.dumps({
            "version": 1, "instance_id": instance, "root_resource_id": root_id,
            "user_id": user, "source_sha256": record.source_sha256,
            "enrolled_at": int(clock()),
        }, sort_keys=True))
    except OSError:
        return "enrollment_unrecorded", None
    return "enrolled", record


def load_enrollment(store: SetupStore) -> AccountEnrollment | None:
    """The record, only if it was made under the currently saved URL + user."""
    data = read_private_json(store.state / ENROLLMENT_FILE)
    saved = store.load_nextcloud()
    if (type(data) is not dict or data.get("version") != 1 or saved is None
            or data.get("source_sha256") != _source_digest(saved)):
        return None
    instance, root_id, user = (data.get("instance_id"), data.get("root_resource_id"),
                               data.get("user_id"))
    if (type(instance) is not str or _INSTANCE.fullmatch(instance) is None
            or not _identifier(root_id) or not root_id.endswith(instance)
            or type(user) is not str or user != _account_user(
                urlsplit(saved["webdav_url"]).path)):
        return None
    return AccountEnrollment(instance, root_id, user, data["source_sha256"])


def context_provider(
    store: SetupStore,
    transport_factory: Callable[[dict], DavTransport],
    *,
    jobs_busy: Callable[[], bool],
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
) -> Callable[[], TrustedRootContext | str]:
    """Server-side ``TrustedRootContext`` for the V1/C picker and V1/D.

    When no context can be built it returns a reason string instead, so the
    UI can say why: ``binding_unavailable`` (no saved connection),
    ``not_enrolled``, ``source_changed``, ``active_root_shared`` (the current
    Vault is a received share or mount, outside V1-S1) or
    ``active_root_unreadable``.
    """
    def build() -> TrustedRootContext | str:
        # Read the revision first and again after any I/O: every field must
        # come from one saved connection, never a mix across a concurrent save.
        revision = store.nextcloud_revision()
        saved = store.load_nextcloud()
        if saved is None or revision is None:
            return "binding_unavailable"
        enrollment = load_enrollment(store)
        if enrollment is None:
            return "not_enrolled"
        account = TrustedDavAccount(_origin(saved["webdav_url"]),
                                    urlsplit(saved["webdav_url"]).path,
                                    enrollment.instance_id)
        data = _read_binding(store.state)
        pin = _pin(data.get("current")) if data else None
        if (pin is not None and pin.chain and pin.path == saved["vault_folder"]
                and pin.instance_id == enrollment.instance_id
                and pin.chain[-1].endswith(enrollment.instance_id)
                and data.get("outcome") in ("activated", "rolled_back")
                and data.get("source_sha256") == _source_digest(saved)):
            active_id = pin.chain[-1]
        else:
            active_id, why = read_root_status(account, transport_factory(saved),
                                              saved["vault_folder"], proof=proof)
            if active_id is None:
                if store.nextcloud_revision() != revision:
                    return "source_changed"
                return {"shared_root": "active_root_shared",
                        "provider_unproven": "provider_unproven"}.get(
                            why, "active_root_unreadable")
        if store.nextcloud_revision() != revision:
            return "source_changed"
        busy = jobs_busy()
        return TrustedRootContext(
            account=account, active=saved["vault_folder"], active_resource_id=active_id,
            container_resource_id=enrollment.root_resource_id,
            source_revision=revision, jobs_in_flight=busy is not False,
        )
    return build
