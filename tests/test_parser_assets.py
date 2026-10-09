from __future__ import annotations

import json

import pytest

pymupdf = pytest.importorskip("pymupdf")

from ocr_agent.evidence_builder import build_document_evidence
from ocr_agent.parser_assets import audit_parser_assets


@pytest.fixture
def parser_inputs(tmp_path):
    pdf = tmp_path / "source.pdf"
    document = pymupdf.open()
    for _ in range(2):
        page = document.new_page(width=300, height=400)
        page.insert_text((20, 30), "Example paragraph")
    document.save(pdf)
    roots = {name: tmp_path / name for name in ("mineru", "paddle", "ovis")}
    for name in ("paddle", "ovis"):
        folder = roots[name] / "images" / "short"
        folder.mkdir(parents=True)
        for index in range(2):
            document[index].get_pixmap(dpi=144, alpha=False).save(folder / f"page_{index + 1:04d}.png")
    document.close()
    mineru = roots["mineru"] / "full/extracted/full/vlm/full_content_list.json"
    mineru.parent.mkdir(parents=True)
    mineru.write_text(json.dumps([
        {"page_idx": page, "type": "text", "text": "Example paragraph", "bbox": [60, 40, 500, 100]}
        for page in (0, 1)
    ]))
    for page in (1, 2):
        path = roots["paddle"] / "json/short" / f"page_{page:04d}_res.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"width": 600, "height": 800, "parsing_res_list": [
            {"block_bbox": [40, 35, 300, 80], "block_label": "text", "block_content": "Example paragraph"}
        ]}))
        markdown = roots["ovis"] / "markdown/short" / f"page_{page:04d}.md"
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text("Example paragraph")
    return pdf, roots


def audit(pdf, roots):
    return audit_parser_assets(pdf, full_id="full", short_id="short", mineru_root=roots["mineru"],
                               paddle_root=roots["paddle"], ovis_root=roots["ovis"])


def test_synthetic_parser_assets_feed_evidence_builder(parser_inputs, tmp_path):
    pdf, roots = parser_inputs
    report = audit(pdf, roots)
    assert report["ok"], report
    assert report["page_count"] == 2
    pages = build_document_evidence(
        tmp_path / "output", sample_id="demo", full_id="full", short_id="short", source_pdf=pdf,
        mineru_root=roots["mineru"], paddle_root=roots["paddle"], ovis_root=roots["ovis"],
    )
    assert len(pages) == 2
    for path in pages:
        evidence = json.loads(path.read_text())
        assert evidence["current_candidate"]["blocks"]
        assert evidence["evidence"]["mineru"] and evidence["evidence"]["paddle"] and evidence["evidence"]["ovis"]


def test_page_number_gap_is_detected_even_when_count_matches(parser_inputs):
    pdf, roots = parser_inputs
    folder = roots["paddle"] / "json/short"
    (folder / "page_0002_res.json").rename(folder / "page_0003_res.json")
    report = audit(pdf, roots)
    assert not report["ok"]
    assert any("missing_page:" in problem and "0002" in problem for problem in report["problems"])
    assert any("unexpected_page:" in problem and "0003" in problem for problem in report["problems"])


def test_wrong_coordinate_units_and_pdf_hash_are_detected(parser_inputs):
    pdf, roots = parser_inputs
    path = roots["paddle"] / "json/short/page_0001_res.json"
    payload = json.loads(path.read_text())
    payload["parsing_res_list"][0]["block_bbox"] = [10, 10, 9000, 9000]
    path.write_text(json.dumps(payload))
    (roots["ovis"] / "short.manifest.json").write_text(json.dumps({"source_sha256": "wrong"}))
    report = audit(pdf, roots)
    assert not report["ok"]
    assert "invalid_paddle_bbox:page=1:block=0" in report["problems"]
    assert "source_pdf_mismatch:ovis" in report["problems"]
