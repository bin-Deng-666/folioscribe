from __future__ import annotations

import json

import pytest

from ocr_agent.batch_registry import load_registry


def entry():
    return {"sample_id": "demo", "short_id": "short", "full_id": "full", "pages": 2,
            "pdf": "source.pdf", "mineru_root": "mineru", "paddle_root": "paddle", "ovis_root": "ovis"}


def test_registry_paths_resolve_relative_to_manifest(tmp_path):
    path = tmp_path / "documents.json"
    path.write_text(json.dumps({"documents": [entry()]}))
    documents = load_registry(path)
    assert documents[0]["pdf"] == str(tmp_path / "source.pdf")
    assert documents[0]["mineru_root"] == str(tmp_path / "mineru")
    assert documents[0]["source_docx"] is None


@pytest.mark.parametrize("problem", ["escape", "duplicate", "missing_root"])
def test_registry_rejects_unsafe_or_incomplete_entries_before_batch_writes(tmp_path, problem):
    row = entry()
    rows = [row]
    if problem == "escape":
        row["sample_id"] = "../outside"
    elif problem == "duplicate":
        rows.append(dict(row))
    else:
        del row["paddle_root"]
    path = tmp_path / "documents.json"
    path.write_text(json.dumps({"documents": rows}))
    with pytest.raises(ValueError):
        load_registry(path)
