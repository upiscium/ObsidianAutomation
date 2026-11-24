"""Simple AI knowledge proposal pipeline ("B1-lite"): one text -> one note.

Design: docs/knowledge-lite-design.md. This module composes pieces that already
exist (immutable context, OpenAI-compatible generator, Knowledge note policy)
and adds the few invariants that matter: the result is only ever a *proposal*
under ``04-AI/Proposals/``, it is checked for active (executable) Obsidian
content before it can be written, and a file is created, never overwritten.
No Vault, WebDAV or scheduler access here; the caller decides where bytes go.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from .artifact_lifecycle import ArtifactLifecycleError, ensure_artifact_layout
from .chatgpt_export_normalize import (
    NormalizeError, PARSER_VERSIONS, SelectedConversation, normalize_export,
)
from .context_bundle import (
    MAX_SOURCE_BYTES, ContextBundle, ContextSource, store_context_bundle,
)
from .knowledge_note_layout import KNOWLEDGE_METADATA_EMBED
from .openai_compatible import (
    DEFAULT_TIMEOUT_SECONDS, JSONTransport, OpenAICompatibleProviderError,
)
from .openai_generator import generate_knowledge_note_with_openai_compatible

PROPOSAL_ROOT = "04-AI/Proposals"
PROPOSAL_EMBED = "```meta-bind-embed\n[[knowledge-proposal-meta]]\n```\n"
PROVENANCE_VERSION = 1
# The generator binds each artifact to an implementation revision, which the
# artifact contract requires to be a 40..64 character lowercase hex digest. A
# fixed digest of this pipeline's name stands in for a commit id.
IMPLEMENTATION_REVISION = hashlib.sha256(b"obsidian-automation knowledge-proposal-v0").hexdigest()
QUERY = (
    "Write one durable knowledge note from the conversation below. Keep only "
    "knowledge worth keeping; omit chit-chat, secrets and personal data."
)

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# Content that Obsidian plugins would execute or that triggers actions. A
# proposal written by an LLM must never carry any of it.
_ACTIVE_CONTENT = (
    re.compile(r"(?m)^[ \t]*(```|~~~)[ \t]*(meta-bind[\w-]*|dataview\w*|templater\w*|button\w*)\b", re.I),
    re.compile(r"<%|%>"),
    re.compile(r"\b(BUTTON|INPUT|VIEW|JS)\["),
    re.compile(r"<\s*(script|iframe|object|embed)\b", re.I),
    re.compile(r"javascript:|obsidian://", re.I),
)


class ProposalError(ValueError):
    """Fixed error codes only; never source text, model output or paths."""


@dataclass(frozen=True)
class SourceText:
    scope: str       # e.g. "private-claude"
    text: str        # the conversation or note text (UTF-8)


@dataclass(frozen=True)
class Proposal:
    target_path: str          # always under PROPOSAL_ROOT
    content: bytes            # the complete proposal note
    context_sha256: str
    proposal_sha256: str
    generation_sha256: str
    model_identifier: str
    source_sha256: str


def conversation_text(conversation: SelectedConversation) -> str:
    return "\n\n".join(f"[{m.role}]\n{m.text}" for m in conversation.messages)


def check_active_content(text: str) -> None:
    for pattern in _ACTIVE_CONTENT:
        if pattern.search(text):
            raise ProposalError("active_content_in_generated_note")


def _provenance(source: SourceText, source_sha256: str, proposal: Mapping[str, str]) -> str:
    record = {
        "v": PROVENANCE_VERSION, "scope": source.scope,
        "source_sha256": source_sha256,
        "context_sha256": proposal["context_sha256"],
        "generation_sha256": proposal["generation_sha256"],
        "model": proposal["model"],
    }
    data = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if "--" in data:  # cannot close the HTML comment early
        raise ProposalError("provenance_not_comment_safe")
    return f"<!-- oa-proposal: {data} -->\n"


def render_proposal(note_text: str, source: SourceText, source_sha256: str,
                    ids: Mapping[str, str]) -> str:
    """Turn the generator's validated Knowledge note into a proposal note."""
    match = _FRONT_MATTER.match(note_text)
    if match is None:
        raise ProposalError("unexpected_front_matter")
    front, rest = match.group(1), note_text[match.end():]
    swapped = []
    for old, new in (("type: knowledge-note", "type: knowledge-proposal"),
                     ("status: active", "status: proposed")):
        if front.count(old) != 1:
            raise ProposalError("unexpected_front_matter")
        swapped.append((old, new))
    for old, new in swapped:
        front = front.replace(old, new, 1)
    # Layout contract: front matter, blank line, the metadata embed, blank line.
    scaffold = "\n" + KNOWLEDGE_METADATA_EMBED + "\n"
    if not rest.startswith(scaffold):
        raise ProposalError("unexpected_layout")
    body = rest[len(scaffold):]
    check_active_content(body)
    check_active_content(front)
    text = ("---\n" + front + "\n---\n\n" + PROPOSAL_EMBED + "\n" + body.rstrip("\n")
            + "\n\n" + _provenance(source, source_sha256, ids))
    if "\r" in text or len(text.encode("utf-8")) > 256 * 1024:
        raise ProposalError("proposal_size_or_newlines_invalid")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def propose(
    ai_root: Path, source: SourceText, *, base_url: str, model: str,
    implementation_revision: str = IMPLEMENTATION_REVISION,
    options: Mapping[str, object] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS, api_key: str | None = None,
    transport: JSONTransport | None = None,
) -> Proposal:
    """Generate one proposal. Writes only below ``ai_root`` (private artifacts)."""
    if (not isinstance(source.scope, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", source.scope) is None):
        raise ProposalError("invalid_scope")
    if not isinstance(source.text, str) or not source.text.strip():
        raise ProposalError("empty_source")
    encoded = source.text.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise ProposalError("source_too_large")
    source_sha256 = hashlib.sha256(encoded).hexdigest()
    try:
        ai_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (ai_root / "05-Context").mkdir(mode=0o700, exist_ok=True)
        ensure_artifact_layout(ai_root)
        # The context contract only accepts Knowledge/Project source paths, so
        # the text is presented under a synthetic Knowledge path. It is never
        # written there.
        bundle = ContextBundle(
            query=QUERY, created_at=_utc_now(),
            sources=(ContextSource(
                path=f"11-Knowledge/{source.scope}-{source_sha256[:12]}.md",
                content_sha256=source_sha256, content=source.text),),
        )
        context_sha256, _ = store_context_bundle(ai_root, bundle)
        result = generate_knowledge_note_with_openai_compatible(
            ai_root, context_sha256=context_sha256, base_url=base_url, model=model,
            implementation_revision=implementation_revision, options=options,
            timeout=timeout, api_key=api_key, transport=transport,
        )
        stored = json.loads(result.proposal_path.read_bytes())
        note_text = stored["content"]
        title_file = PurePosixPath(stored["target"]["path"]).name
    except (ArtifactLifecycleError, OpenAICompatibleProviderError) as exc:
        raise ProposalError("generation_failed") from exc
    except (OSError, ValueError, KeyError, TypeError):
        raise ProposalError("generation_failed") from None
    if not title_file.endswith(".md") or "/" in title_file:
        raise ProposalError("unexpected_layout")
    ids = {"context_sha256": context_sha256,
           "generation_sha256": result.generation_sha256,
           "model": result.model_identifier}
    text = render_proposal(note_text, source, source_sha256, ids)
    return Proposal(
        target_path=f"{PROPOSAL_ROOT}/{title_file}", content=text.encode("utf-8"),
        context_sha256=context_sha256, proposal_sha256=result.proposal_sha256,
        generation_sha256=result.generation_sha256,
        model_identifier=result.model_identifier, source_sha256=source_sha256,
    )


def write_proposal(directory: Path, proposal: Proposal) -> Path:
    """Create the proposal file; an existing file is never overwritten."""
    name = PurePosixPath(proposal.target_path).name
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise ProposalError("proposal_exists") from None
    except OSError:
        raise ProposalError("proposal_not_written") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(proposal.content)
        stream.flush()
        os.fsync(stream.fileno())
    return path


# ---------------------------------------------------------------- manual CLI

def _conversations(zip_path: Path, provider: str) -> tuple[SelectedConversation, ...]:
    return normalize_export(zip_path, scope="private-" + provider,
                            provider=provider).conversations


def _first_timestamp(conversation: SelectedConversation) -> str:
    for message in conversation.messages:
        if isinstance(message.created_at, (int, float)):
            return datetime.fromtimestamp(message.created_at, timezone.utc).strftime("%Y-%m-%d")
    return "-"


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ProposalError("invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("action", choices=("list", "propose"))
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--provider", choices=tuple(PARSER_VERSIONS), required=True)
    parser.add_argument("--index", type=int)
    parser.add_argument("--ai-root", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--timeout", type=float, default=300.0)
    try:
        args = parser.parse_args(argv)
        conversations = _conversations(args.zip, args.provider)
        if args.action == "list":
            for index, item in enumerate(conversations):
                size = len(conversation_text(item).encode("utf-8"))
                fits = "ok" if size <= MAX_SOURCE_BYTES else "too_large"
                print(json.dumps({"index": index, "messages": len(item.messages),
                                  "bytes": size, "first_day": _first_timestamp(item),
                                  "fits": fits}, sort_keys=True))
            return 0
        needed = (args.index, args.ai_root, args.out, args.base_url, args.model)
        if any(value is None for value in needed) or not 0 <= args.index < len(conversations):
            raise ProposalError("invalid_arguments")
        api_key = None
        if args.api_key_file is not None:
            api_key = args.api_key_file.read_text(encoding="utf-8").strip() or None
        proposal = propose(
            args.ai_root, SourceText("private-" + args.provider,
                                     conversation_text(conversations[args.index])),
            base_url=args.base_url, model=args.model, timeout=args.timeout,
            api_key=api_key)
        written = write_proposal(args.out, proposal)
        print(json.dumps({"status": "proposed", "bytes": len(proposal.content),
                          "file": written.name, "vault_write": False}, sort_keys=True))
        return 0
    except (ProposalError, NormalizeError) as exc:
        print(json.dumps({"status": "error", "error_code": str(exc),
                          "vault_write": False}, sort_keys=True))
        return 1
    except OSError:
        print(json.dumps({"status": "error", "error_code": "io_error",
                          "vault_write": False}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
