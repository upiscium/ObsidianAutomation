from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import _decode_json_object, _utc_now
from .core_promotion import (
    PromotionChange,
    PromotionError,
    PromotionPlan,
    _tree_blob,
    parse_promotion_plan,
    promotion_plan_sha256,
    verify_plan_against_core,
)
from .core_promotion_transport import (
    HTTPTransport,
    MAX_MUTATION_RESPONSE_BYTES,
    MAX_REMOTE_RESPONSE_BYTES,
    PreflightChange,
    PromotionCheckpoint,
    PromotionOutcome,
    PromotionReceipt,
    PromotionTransportConflict,
    PromotionTransportError,
    PromotionTransportNetworkError,
    RemoteState,
    _advance_checkpoint,
    _perform_mutation,
    _preflight_disposition,
    _real_http_request,
    _strong_etag,
    load_checkpoint,
    store_receipt,
)
from .webdav_create import WebDAVCreateError, build_target_url


APPEARANCE_PATH = ".obsidian/appearance.json"
APPEARANCE_MANAGED_KEYS = frozenset({"theme", "cssTheme", "enabledCssSnippets"})
APPEARANCE_MANAGED_SNIPPETS = (
    "obsidian-core",
    "obsidian-core-mobile",
    "callout-colors",
    "expense-dashboard-lite",
    "mobile-home-buttons",
    "monthly-expanse",
    "task-button",
    "task-controls",
    "task-status",
    "work-time",
)
_APPEARANCE_MANAGED_SNIPPET_SET = frozenset(APPEARANCE_MANAGED_SNIPPETS)


@dataclass(frozen=True)
class AppearanceProjection:
    theme: str
    css_theme: str
    managed_snippets: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedChange:
    entry: PreflightChange
    remote_bytes: bytes | None = None


def _decode_appearance(data: bytes, *, label: str, core_owned: bool = False) -> dict[str, object]:
    try:
        value = _decode_json_object(data, label=label)
    except Exception as exc:
        raise PromotionTransportError(f"{label} is not a valid JSON object: {exc}") from exc

    if core_owned and set(value) != APPEARANCE_MANAGED_KEYS:
        raise PromotionTransportError(
            f"{label} must contain exactly the managed appearance keys: "
            + ", ".join(sorted(APPEARANCE_MANAGED_KEYS))
        )

    theme = value.get("theme")
    css_theme = value.get("cssTheme")
    snippets = value.get("enabledCssSnippets")
    if not isinstance(theme, str) or not isinstance(css_theme, str):
        raise PromotionTransportError(f"{label} theme and cssTheme must be strings")
    if not isinstance(snippets, list) or any(not isinstance(item, str) for item in snippets):
        raise PromotionTransportError(f"{label} enabledCssSnippets must be a list of strings")
    if len(snippets) != len(set(snippets)):
        raise PromotionTransportError(f"{label} enabledCssSnippets must not contain duplicates")
    return value


def _appearance_projection(value: dict[str, object]) -> AppearanceProjection:
    snippets = value["enabledCssSnippets"]
    assert isinstance(snippets, list)
    return AppearanceProjection(
        theme=str(value["theme"]),
        css_theme=str(value["cssTheme"]),
        managed_snippets=tuple(
            item for item in snippets if isinstance(item, str) and item in _APPEARANCE_MANAGED_SNIPPET_SET
        ),
    )


def _core_bytes(
    core_repository: Path,
    commit: str,
    path: str,
    expected_sha256: str | None,
    *,
    label: str,
) -> bytes:
    if expected_sha256 is None:
        raise PromotionTransportError(f"{label} has no expected SHA-256")
    try:
        data = _tree_blob(core_repository, commit, path)
    except PromotionError as exc:
        raise PromotionTransportError(str(exc)) from exc
    if data is None:
        raise PromotionTransportError(f"{label} is missing from Core: {path}")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise PromotionTransportError(f"{label} bytes do not match the Promotion Plan: {path}")
    return data


def _observe_remote_bytes(
    *,
    base_url: str,
    path: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> tuple[RemoteState, bytes | None]:
    try:
        target = build_target_url(base_url, path)
    except WebDAVCreateError as exc:
        raise PromotionTransportError(str(exc)) from exc
    request = transport or _real_http_request
    response = request(
        method="GET",
        target_url=target,
        username=username,
        password=password,
        headers={"Accept": "application/octet-stream"},
        body=None,
        timeout=timeout,
        response_limit=MAX_REMOTE_RESPONSE_BYTES,
    )
    if response.status == 404:
        return RemoteState(path=path, content_sha256=None, etag=response.etag, status_code=404), None
    if response.status != 200:
        raise PromotionTransportError(
            f"WebDAV GET returned unexpected HTTP status {response.status} for {path}"
        )
    digest = hashlib.sha256(response.body).hexdigest()
    return (
        RemoteState(path=path, content_sha256=digest, etag=response.etag, status_code=200),
        response.body,
    )


def _appearance_context(
    plan: PromotionPlan,
    change: PromotionChange,
    core_repository: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    if change.action != "update":
        raise PromotionTransportError("semantic appearance handling supports update changes only")
    base_bytes = _core_bytes(
        core_repository,
        plan.base_commit,
        change.path,
        change.before_sha256,
        label="Core base appearance",
    )
    head_bytes = _core_bytes(
        core_repository,
        plan.head_commit,
        change.path,
        change.after_sha256,
        label="Core head appearance",
    )
    return (
        _decode_appearance(base_bytes, label="Core base appearance", core_owned=True),
        _decode_appearance(head_bytes, label="Core head appearance", core_owned=True),
    )


def _appearance_disposition(
    *,
    plan: PromotionPlan,
    change: PromotionChange,
    core_repository: Path,
    remote: RemoteState,
    remote_bytes: bytes | None,
) -> str:
    if change.action != "update" or remote_bytes is None:
        return _preflight_disposition(change, remote)
    if remote.content_sha256 == change.before_sha256:
        return "apply"
    if remote.content_sha256 == change.after_sha256:
        return "already_applied"

    base_value, head_value = _appearance_context(plan, change, core_repository)
    remote_value = _decode_appearance(remote_bytes, label="remote appearance")
    remote_projection = _appearance_projection(remote_value)
    base_projection = _appearance_projection(base_value)
    head_projection = _appearance_projection(head_value)
    if remote_projection == head_projection:
        return "already_applied"
    if remote_projection == base_projection:
        return "apply"
    return "conflict"


def _merge_appearance(remote_bytes: bytes, head_value: dict[str, object]) -> bytes:
    remote_value = _decode_appearance(remote_bytes, label="remote appearance")
    merged = dict(remote_value)
    merged["theme"] = head_value["theme"]
    merged["cssTheme"] = head_value["cssTheme"]

    remote_snippets = remote_value["enabledCssSnippets"]
    head_snippets = head_value["enabledCssSnippets"]
    assert isinstance(remote_snippets, list)
    assert isinstance(head_snippets, list)
    desired_managed = [
        item for item in head_snippets if isinstance(item, str) and item in _APPEARANCE_MANAGED_SNIPPET_SET
    ]

    output: list[str] = []
    inserted = False
    for item in remote_snippets:
        assert isinstance(item, str)
        if item in _APPEARANCE_MANAGED_SNIPPET_SET:
            if not inserted:
                output.extend(desired_managed)
                inserted = True
            continue
        output.append(item)
    if not inserted:
        output.extend(desired_managed)
    merged["enabledCssSnippets"] = output
    return (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _semantic_appearance_reached(
    *,
    remote_bytes: bytes | None,
    head_value: dict[str, object],
) -> bool:
    if remote_bytes is None:
        return False
    remote_value = _decode_appearance(remote_bytes, label="remote appearance after update")
    return _appearance_projection(remote_value) == _appearance_projection(head_value)


def _perform_appearance_mutation(
    *,
    change: PromotionChange,
    preflight: PreflightChange,
    remote_bytes: bytes,
    plan: PromotionPlan,
    core_repository: Path,
    base_url: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> str:
    _, head_value = _appearance_context(plan, change, core_repository)
    body = _merge_appearance(remote_bytes, head_value)
    etag = _strong_etag(preflight.remote.etag)
    if etag is None:
        raise PromotionTransportError(
            f"remote {change.path} lost its required strong ETag before semantic update"
        )
    try:
        target = build_target_url(base_url, change.path)
    except WebDAVCreateError as exc:
        raise PromotionTransportError(str(exc)) from exc

    request = transport or _real_http_request
    response_status: int | None = None
    network_ambiguous = False
    try:
        response = request(
            method="PUT",
            target_url=target,
            username=username,
            password=password,
            headers={
                "If-Match": etag,
                "Content-Type": "application/json; charset=utf-8",
                "X-NC-WebDAV-Auto-Mkcol": "1",
            },
            body=body,
            timeout=timeout,
            response_limit=MAX_MUTATION_RESPONSE_BYTES,
        )
        response_status = response.status
    except PromotionTransportNetworkError:
        network_ambiguous = True

    remote, observed_bytes = _observe_remote_bytes(
        base_url=base_url,
        path=change.path,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    if _semantic_appearance_reached(remote_bytes=observed_bytes, head_value=head_value):
        return "recovered" if network_ambiguous or not (response_status and 200 <= response_status < 300) else "applied"

    if response_status == 412:
        raise PromotionTransportConflict(
            f"remote {change.path} changed after semantic preflight; conditional update rejected"
        )
    if network_ambiguous or response_status is None or not 200 <= response_status < 300:
        base_value, _ = _appearance_context(plan, change, core_repository)
        if observed_bytes is not None:
            observed_value = _decode_appearance(observed_bytes, label="remote appearance after ambiguous update")
            if _appearance_projection(observed_value) == _appearance_projection(base_value):
                raise PromotionTransportNetworkError(
                    f"ambiguous semantic update for {change.path}: desired managed state is not observable remotely"
                )
        raise PromotionTransportConflict(
            f"remote {change.path} diverged after ambiguous semantic update outcome"
        )

    raise PromotionTransportConflict(
        f"remote semantic verification failed after successful update: {change.path}"
    )


def _preflight_managed(
    plan: PromotionPlan,
    *,
    core_repository: Path,
    base_url: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> tuple[_PreparedChange, ...]:
    if not username:
        raise PromotionTransportError("promotion WebDAV username must not be empty")
    if not password:
        raise PromotionTransportError("promotion WebDAV password must not be empty")
    if timeout <= 0 or timeout > 600:
        raise PromotionTransportError("promotion timeout must be in (0, 600] seconds")

    prepared: list[_PreparedChange] = []
    for change in plan.changes:
        remote, remote_bytes = _observe_remote_bytes(
            base_url=base_url,
            path=change.path,
            username=username,
            password=password,
            timeout=timeout,
            transport=transport,
        )
        if change.path == APPEARANCE_PATH and change.action == "update":
            disposition = _appearance_disposition(
                plan=plan,
                change=change,
                core_repository=core_repository,
                remote=remote,
                remote_bytes=remote_bytes,
            )
        else:
            disposition = _preflight_disposition(change, remote)

        if disposition == "apply" and change.action in {"update", "delete"}:
            if _strong_etag(remote.etag) is None:
                raise PromotionTransportError(
                    f"remote {change.path} does not provide a strong ETag required for conditional {change.action}"
                )
        prepared.append(
            _PreparedChange(
                entry=PreflightChange(change=change, remote=remote, disposition=disposition),
                remote_bytes=remote_bytes if change.path == APPEARANCE_PATH else None,
            )
        )

    conflicts = [item.entry.change.path for item in prepared if item.entry.disposition == "conflict"]
    if conflicts:
        raise PromotionTransportConflict(
            "promotion preflight found divergent remote paths; no mutation attempted: "
            + ", ".join(conflicts)
        )
    return tuple(prepared)


def execute_managed_promotion(
    *,
    plan: PromotionPlan,
    core_repository: Path,
    config_path: Path,
    base_url: str,
    username: str,
    password: str,
    checkpoint_path: Path,
    receipt_directory: Path,
    timeout: float = 30.0,
    transport: HTTPTransport | None = None,
) -> tuple[str, Path, PromotionReceipt, PromotionCheckpoint]:
    try:
        normalized_plan = parse_promotion_plan(plan.to_json_bytes())
        verify_plan_against_core(normalized_plan, core_repository, config_path=config_path)
    except PromotionError as exc:
        raise PromotionTransportError(str(exc)) from exc

    checkpoint = load_checkpoint(checkpoint_path)
    if checkpoint.last_observed_core_commit != normalized_plan.base_commit:
        raise PromotionTransportError(
            "promotion plan base_commit does not match last_observed_core_commit checkpoint"
        )
    if (
        checkpoint.policy_version != normalized_plan.policy_version
        or checkpoint.policy_sha256 != normalized_plan.policy_sha256
    ):
        raise PromotionTransportError("promotion plan policy does not match checkpoint policy")

    prepared = _preflight_managed(
        normalized_plan,
        core_repository=core_repository,
        base_url=base_url,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )

    outcomes: list[PromotionOutcome] = []
    for item in prepared:
        entry = item.entry
        change = entry.change
        if entry.disposition == "already_applied":
            result = "already_applied"
        elif entry.disposition == "apply":
            if change.path == APPEARANCE_PATH and change.action == "update":
                if item.remote_bytes is None:
                    raise PromotionTransportConflict(
                        f"remote {change.path} disappeared after semantic preflight"
                    )
                result = _perform_appearance_mutation(
                    change=change,
                    preflight=entry,
                    remote_bytes=item.remote_bytes,
                    plan=normalized_plan,
                    core_repository=core_repository,
                    base_url=base_url,
                    username=username,
                    password=password,
                    timeout=timeout,
                    transport=transport,
                )
            else:
                result = _perform_mutation(
                    change=change,
                    preflight=entry,
                    plan=normalized_plan,
                    core_repository=core_repository,
                    base_url=base_url,
                    username=username,
                    password=password,
                    timeout=timeout,
                    transport=transport,
                )
        else:
            raise PromotionTransportConflict(f"unexpected conflict after preflight: {change.path}")
        outcomes.append(
            PromotionOutcome(
                action=change.action,
                path=change.path,
                before_sha256=change.before_sha256,
                after_sha256=change.after_sha256,
                result=result,
            )
        )

    receipt = PromotionReceipt(
        plan_sha256=promotion_plan_sha256(normalized_plan),
        base_commit=normalized_plan.base_commit,
        head_commit=normalized_plan.head_commit,
        policy_version=normalized_plan.policy_version,
        policy_sha256=normalized_plan.policy_sha256,
        outcomes=tuple(outcomes),
        completed_at=_utc_now(),
    )
    receipt_sha, receipt_path = store_receipt(receipt_directory, receipt)
    advanced = _advance_checkpoint(checkpoint_path, normalized_plan)
    return receipt_sha, receipt_path, receipt, advanced
