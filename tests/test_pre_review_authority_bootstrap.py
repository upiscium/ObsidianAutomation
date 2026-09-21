from pathlib import Path


SCRIPT = Path("examples/ai/bootstrap-pre-review-authority.sh")


def test_embedded_acl_backfill_is_valid_python():
    source = SCRIPT.read_text()
    embedded = source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    compile(embedded, str(SCRIPT), "exec")


def test_acl_backfill_is_limited_to_issue_148_artifact_classes():
    source = SCRIPT.read_text()
    assert "\"$EVALUATOR_PROJECTIONS\" '.projection.json' \"$REVIEWER_USER\"" in source
    assert "\"$PROJECTION_RESULTS\" '.projection-result.json' \"$REVIEWER_USER\"" in source
    assert "\"$REVIEWS\" '.approval.json' \"$READER_USER\"" in source
    assert "\"$RECEIPTS\" '.receipt.json' \"$READER_USER\"" in source
