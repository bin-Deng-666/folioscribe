"""Corpus selection gates: a document is only runnable when assets and content allow it."""
from __future__ import annotations

from pathlib import Path

import pytest

from ocr_agent import corpus_selection as SELECT


def _assets(tmp_path: Path, short: str, full: str, *, pages: int = 6) -> dict:
    sample_dir = tmp_path / "gt" / "manual::guide" / f"rd_{short}"
    (sample_dir / "pdf").mkdir(parents=True)
    (sample_dir / "pdf" / "document.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "mineru" / full).mkdir(parents=True)
    for root, pattern in (("paddle", "images"), ("paddle", "json"), ("ovis", "images"), ("ovis", "markdown")):
        directory = tmp_path / root / pattern / short
        directory.mkdir(parents=True)
        for index in range(1, pages + 1):
            name = f"page_{index:04d}_res.json" if pattern == "json" else f"page_{index:04d}.md" if pattern == "markdown" else f"page_{index:04d}.png"
            (directory / name).write_text("x")
    return sample_dir


def _sources(tmp_path: Path, *, short: str, full: str, perceptual: dict, docx: dict | None = None, mineru_scene: dict | None = None, gt_dir: Path | None = None) -> dict:
    return {
        "difficulty": {},
        "perceptual": {short: {"page_count": 6, "doc_features": perceptual}},
        "docx": {f"rd_{short}": docx or {}},
        "mineru_scene": {full: mineru_scene or {}},
        "full_ids": {short: full},
        "gt_dirs": {f"rd_{short}": gt_dir or tmp_path / "gt" / "manual::guide" / f"rd_{short}"},
        "roots": {"mineru": tmp_path / "mineru", "paddle": tmp_path / "paddle", "ovis": tmp_path / "ovis", "docx": tmp_path / "docx"},
    }


BASE_FEATURES = {"text_layer_coverage": 0.2, "image_area_coverage": 0.05, "scan_likelihood": 0.0, "table_page_rate": 0.4}


def test_table_rich_document_is_core(tmp_path: Path) -> None:
    short, full = "aaaaaaaabbbb", "aaaaaaaabbbbcccc"
    _assets(tmp_path, short, full)
    doc = SELECT.evaluate({"sample_id": f"rd_{short}"}, _sources(tmp_path, short=short, full=full, perceptual=BASE_FEATURES))
    assert doc["tier"] == "core"
    assert doc["exclusions"] == []
    assert doc["flags"]["table_rich"] is True
    assert doc["assets"]["pdf"].endswith("document.pdf")


def test_plain_document_is_extended_not_core(tmp_path: Path) -> None:
    short, full = "ccccccccdddd", "ccccccccddddeeee"
    _assets(tmp_path, short, full)
    features = {"text_layer_coverage": 0.12, "image_area_coverage": 0.02, "scan_likelihood": 0.0}
    doc = SELECT.evaluate({"sample_id": f"rd_{short}"}, _sources(tmp_path, short=short, full=full, perceptual=features))
    assert doc["tier"] == "extended"
    assert doc["flags"]["table_rich"] is False


@pytest.mark.parametrize(
    "features,pages,expected",
    [
        ({**BASE_FEATURES, "scan_likelihood": 0.8}, 6, "whole_page_scan"),
        ({**BASE_FEATURES, "text_layer_coverage": 0.01}, 6, "text_layer_too_thin"),
        ({**BASE_FEATURES, "image_area_coverage": 0.6, "text_layer_coverage": 0.04}, 6, "image_dominant_canvas"),
    ],
)
def test_content_gates_exclude(tmp_path: Path, features: dict, pages: int, expected: str) -> None:
    short, full = "eeeeeeeeffff", "eeeeeeeeffff0000"
    _assets(tmp_path, short, full)
    doc = SELECT.evaluate({"sample_id": f"rd_{short}"}, _sources(tmp_path, short=short, full=full, perceptual=features))
    assert expected in doc["exclusions"]
    assert doc["tier"] == ""


def test_image_rich_but_text_healthy_document_survives(tmp_path: Path) -> None:
    short, full = "111122223333", "1111222233334444"
    _assets(tmp_path, short, full)
    features = {"text_layer_coverage": 0.26, "image_area_coverage": 0.68, "scan_likelihood": 0.21}
    doc = SELECT.evaluate({"sample_id": f"rd_{short}"}, _sources(tmp_path, short=short, full=full, perceptual=features))
    assert doc["exclusions"] == []
    assert doc["tier"] in {"core", "extended"}


def test_missing_evidence_sources_are_reported(tmp_path: Path) -> None:
    short, full = "555566667777", "5555666677778888"
    sources = _sources(tmp_path, short=short, full=full, perceptual=BASE_FEATURES)
    doc = SELECT.evaluate({"sample_id": f"rd_{short}"}, sources)
    assert {"missing_pdf", "missing_mineru_raw", "missing_paddle_pages", "missing_ovis_pages"} <= set(doc["exclusions"])


def test_tiny_documents_are_flagged_not_dropped(tmp_path: Path) -> None:
    short, full = "9999aaaabbbb", "9999aaaabbbbcccc"
    _assets(tmp_path, short, full, pages=2)
    doc = SELECT.evaluate({"sample_id": f"rd_{short}"}, _sources(tmp_path, short=short, full=full, perceptual=BASE_FEATURES))
    assert doc["tiny"] is True
    assert doc["tier"] == "core"


def test_policy_thresholds_are_declared() -> None:
    gate = SELECT.POLICY["content_gate"]
    assert gate["max_pages"] == 100
    assert gate["min_text_layer_coverage"] == 0.05
    assert gate["max_scan_likelihood"] == 0.5
    assert set(SELECT.POLICY["capability_flags"]) == {
        "table_rich",
        "has_tables",
        "has_headings",
        "has_figures",
        "has_furniture",
        "cross_page_flow",
        "multi_column",
        "has_toc",
    }
