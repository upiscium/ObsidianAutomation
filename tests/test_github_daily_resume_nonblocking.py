"""Nonblocking special-inode guard for verified resume CAS inputs."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError, sha256_bytes
from obsidian_automation.github_daily_resume import _request_bytes, _root, load_resume


def _attempt_resume_read(root: str, request: dict[str, object]) -> None:
    try:
        load_resume(Path(root), request=request)
    except ArtifactLifecycleError:
        os._exit(0)
    os._exit(76)


def test_fifo_resume_pointer_fails_closed_without_blocking(tmp_path: Path) -> None:
    """An existing FIFO at the exact key must not hang prior to fstat."""
    request: dict[str, object] = {
        "stage": "partial",
        "input_context_sha256": "0" * 64,
        "prompt_template_sha256": "1" * 64,
        "implementation_revision": "a" * 40,
        "model_provider": "ollama",
        "model_identifier": "gemma4:12b",
        "model_revision": "2" * 64,
        "model_config": {},
    }
    directory = _root(tmp_path, create=True)
    assert directory is not None
    sha = sha256_bytes(_request_bytes(request))
    fifo = directory / f"{sha}.github-daily-resume.json"
    os.mkfifo(fifo, 0o600)

    proc = multiprocessing.get_context("fork").Process(
        target=_attempt_resume_read, args=(str(tmp_path), request)
    )
    proc.start()
    proc.join(timeout=3)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)
        raise AssertionError("resume reader blocked on untrusted FIFO")
    assert proc.exitcode == 0
    assert fifo.is_fifo()
