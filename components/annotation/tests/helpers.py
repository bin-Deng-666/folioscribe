from __future__ import annotations

from ocr_agent.constants import FIELD_REVIEW_VERSION, SCHEMA_VERSION
from ocr_agent.schemas import add_coordinate_views


def make_page() -> dict:
    page = {
        "schema_version": SCHEMA_VERSION,
        "sample_id": "sample",
        "page": 1,
        "page_image": {"path": "", "sha256": "x", "width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 500.0, "height_pt": 700.0},
        "pdf_path": "",
        "evidence": {
            "pdf": [{
                "evidence_id": "pdf.line.p1.0001",
                "kind": "pdf_line",
                "bbox_image_px": [100, 100, 600, 150],
                "text": "Heading",
                "required_coverage": True,
            }],
            "mineru": [{
                "evidence_id": "mineru.block.p1.0001",
                "kind": "model_block",
                "model": "mineru",
                "source_type": "text",
                "type_candidate": "title",
                "bbox_image_px": [90, 90, 620, 160],
                "text": "Heading",
                "required_coverage": False,
            }],
            "paddle": [], "ovis": [], "anydoc": [], "tools": [],
        },
        "current_candidate": {"blocks": []},
        "field_reviews": {
            "schema_version": FIELD_REVIEW_VERSION,
            "blocks": {},
        },
        "detected_conflicts": [],
        "document_context": {"confirmed_titles": [], "candidate_titles": []},
    }
    block = {
        "block_id": "candidate.block.p1.0001",
        "type": "title",
        "level": 1,
        "bbox_image_px": [100, 100, 600, 150],
        "text": "Heading",
        "source_refs": ["pdf.line.p1.0001"],
        "support_refs": ["mineru.block.p1.0001"],
        "reading_order": 0,
    }
    add_coordinate_views(block, page)
    page["current_candidate"]["blocks"] = [block]
    page["field_reviews"]["blocks"][block["block_id"]] = {
        "type": {"status": "PASS", "method": "model_type_comparison", "evidence_refs": ["mineru.block.p1.0001"]},
        "level": {"status": "PASS", "method": "document_heading_sequence", "evidence_refs": ["mineru.block.p1.0001"]},
        "bbox": {"status": "PASS", "method": "pdf_span_union", "evidence_refs": ["pdf.line.p1.0001"]},
        "text": {"status": "PASS", "method": "pdf_text_exact", "evidence_refs": ["pdf.line.p1.0001"]},
    }
    return page
