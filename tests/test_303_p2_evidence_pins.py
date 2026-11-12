"""#303 fixed F1/P2 evidence: digests and counts are pinned, never re-run.

The receipts are owner-private (outside the repository). When they are not
present the live check is skipped and the result is NOT_REVERIFIED; the
tamper checks always run on in-memory copies.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

BASE = Path.home() / ".local/state"
RUN = BASE / "oa303-nextcloud-disposable/p1b-20261110T075339Z-43fc930c13"
CONTRACT = BASE / "oa303-v1-root-contract-20261107"
PINS = {
    RUN / "p1b-f1-seed-receipt.jsonl":
        "7b5316f0a5a9c404ea993d95465297f9d341ebd7a58ba0219bc17fb7877bbf4c",
    RUN / "p2-readonly-20261111-receipt.jsonl":
        "6e839f3eb7ed0ca407c92e3a3e48f8b96f3dabe353063bfb0837a04b70a4a574",
    CONTRACT / "oa303-p1b-synthetic-dav-fixture-candidate-20261110.json":
        "61d9d0ae41c684622f0e1a969f51b9bd9d66e9ed09f7684f8215ae7a0ed1b1be",
    CONTRACT / "oa303-p2-readonly-one-shot-client-20261111.php":
        "de78bf31886170eae4e2100eecfc10931abee0777261d6b07a3220cd5d5c0737",
    CONTRACT / "oa303-p2-execution-guard-20261111.py":
        "f7420299aeda91eca2ab8f24f83c804de6300567c44a121d54d8e7938461d07d",
}
P2_EXPECTED = {"attempted": 7, "valid_207": 7, "observed_distinct": 9,
               "observed_occurrences": 14, "uncertain": False,
               "state": "P2_METADATA_OBSERVATION_COMPLETE"}
P2_METADATA = {"supported": 89, "unknown": 9, "observed_fileids": 14, "observed_etags": 14}


def check_p2(data: bytes) -> str:
    if hashlib.sha256(data).hexdigest() != PINS[RUN / "p2-readonly-20261111-receipt.jsonl"]:
        return "DIGEST_MISMATCH"
    lines = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    if len(lines) != 2 or lines[0].get("replay") != "FORBIDDEN":
        return "SHAPE_MISMATCH"
    terminal = lines[1]
    if any(terminal.get(k) != v for k, v in P2_EXPECTED.items()):
        return "COUNT_MISMATCH"
    if any(terminal["metadata"].get(k) != v for k, v in P2_METADATA.items()):
        return "COUNT_MISMATCH"
    if len(terminal["probes"]) != 7 or any(p["http"] != 207 for p in terminal["probes"]):
        return "COUNT_MISMATCH"
    return "PINNED"


@pytest.mark.parametrize("path", sorted(PINS, key=str))
def test_private_evidence_matches_pinned_digest(path):
    if not path.is_file():
        pytest.skip("NOT_REVERIFIED: owner-private evidence absent")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PINS[path]


def test_p2_counts_match_and_tampering_is_detected():
    path = RUN / "p2-readonly-20261111-receipt.jsonl"
    if not path.is_file():
        pytest.skip("NOT_REVERIFIED: owner-private evidence absent")
    data = path.read_bytes()
    assert check_p2(data) == "PINNED"
    assert check_p2(data.replace(b'"valid_207":7', b'"valid_207":6')) == "DIGEST_MISMATCH"
    assert check_p2(data + b"\n") == "DIGEST_MISMATCH"


def test_checker_rejects_wrong_counts_even_with_matching_shape(monkeypatch):
    fake = (b'{"replay":"FORBIDDEN"}\n'
            b'{"attempted":7,"valid_207":7,"observed_distinct":9,"observed_occurrences":14,'
            b'"uncertain":false,"state":"P2_METADATA_OBSERVATION_COMPLETE",'
            b'"metadata":{"supported":89,"unknown":9,"observed_fileids":13,'
            b'"observed_etags":14},"probes":[]}')
    monkeypatch.setitem(PINS, RUN / "p2-readonly-20261111-receipt.jsonl",
                        hashlib.sha256(fake).hexdigest())
    assert check_p2(fake) == "COUNT_MISMATCH"
