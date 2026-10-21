"""Read-only, no-LLM Daily preview CLI: never calls the Nextcloud Writer."""
from __future__ import annotations

import argparse
from datetime import datetime,timedelta
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .github_daily_deterministic_digest import (
    DeterministicDigestError, build_digest_from_verified_capture,
)
from .github_daily_progress import (
    DailyProgressError, MAX_DAILY_BYTES, prepare_daily_update,
)

MAX_INPUT_BYTES=MAX_DAILY_BYTES


def _regular_input(path: Path) -> bytes:
    flags=os.O_RDONLY | getattr(os,"O_NOFOLLOW",0) | getattr(os,"O_NONBLOCK",0)
    fd=os.open(path,flags)
    with os.fdopen(fd,"rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DeterministicDigestError("local Daily must be a regular file")
        data=stream.read(MAX_INPUT_BYTES+1)
    if len(data)>MAX_INPUT_BYTES:
        raise DeterministicDigestError("local Daily exceeds original Writer byte budget")
    return data


def _output_file(path: Path,data: bytes):
    # Create-only private artifacts: never replace existing user/Writer files.
    try:
        fd=os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                   getattr(os,"O_NOFOLLOW",0),0o600)
    except FileExistsError:
        if _regular_input(path)!=data:
            raise DeterministicDigestError("existing preview artifact differs")
        return
    with os.fdopen(fd,"wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(
        description="Render yesterday's Issue/PR progress from exact CAS, no LLM or Writer.")
    parser.add_argument("--capture-sha",required=True,
                        help="Exact verified Collector CAS Capture Index SHA-256")
    parser.add_argument("--target-date",help="Require YYYY-MM-DD JST source day")
    parser.add_argument("--yesterday",action="store_true",
                        help="Require source date to be yesterday in Asia/Tokyo")
    parser.add_argument("--output-dir",type=Path,
                        help="Existing private directory for immutable Markdown/JSON preview")
    parser.add_argument("--preview-daily",type=Path,
                        help="Read a local Daily Note and prepare section-only update OFFLINE")
    args=parser.parse_args(argv)
    if args.preview_daily is not None and args.output_dir is None:
        parser.error("--preview-daily requires --output-dir")
    try:
        result=build_digest_from_verified_capture(args.capture_sha)
        if args.yesterday:
            expected=(datetime.now(ZoneInfo("Asia/Tokyo")).date()-
                      timedelta(days=1)).isoformat()
            if result.date!=expected:
                raise DeterministicDigestError("capture date is not yesterday JST")
        if args.target_date and args.target_date!=result.date:
            raise DeterministicDigestError("capture date does not match selected JST day")
        body=result.render()
        if args.output_dir is None:
            sys.stdout.write(body)
            return 0
        root=args.output_dir
        if root.is_symlink() or not root.is_dir():
            raise DeterministicDigestError("private preview target must exist and be a directory")
        md=root/f"{result.sha256}.deterministic-daily.md"
        js=root/f"{result.sha256}.deterministic-daily.json"
        _output_file(md,body.encode("utf-8"))
        _output_file(js,result.canonical_bytes)
        print(f"PREVIEW_MARKDOWN={md}")
        print(f"PREVIEW_EVIDENCE={js}")
        print(f"ORIGINAL_EVENT_COUNT={result.original_event_count}")
        print(f"ENTITY_COUNT={len(result.items)}")
        print(f"MODEL_USED=false PUBLICATION_AUTHORIZED=false")
        if args.preview_daily is not None:
            remote=_regular_input(args.preview_daily)
            stub=SimpleNamespace(section_body=body.rstrip("\n")+"\n\n")
            status,updated=prepare_daily_update(stub,remote)
            draft=root/f"{result.sha256}.daily-note.preview.md"
            _output_file(draft,updated)
            print(f"LOCAL_DAILY_DIFF={status} PREVIEW_DAILY={draft}")
        return 0
    except (DeterministicDigestError,DailyProgressError,OSError,ValueError) as exc:
        print(f"deterministic-daily-preview: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
