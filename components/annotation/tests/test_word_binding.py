"""Word binding policy: evidence is admitted per table, not per document.

A document-level binding score of 0.70-0.95 used to discard every native table (the build aborted
and the batch fell back to PDF-only), even when each individual table matched its native
counterpart exactly. These tests pin the replacement policy:

  * ``word_table_ref`` / ``word_row_range`` come from table-local evidence,
  * the model HTML is only replaced when the document itself binds strongly,
  * row coverage that fails the fragment consistency check is withdrawn, never silently kept.
"""
from ocr_agent.evidence_builder import _attach_word_tables, _withdraw_unverified_word_rows
from ocr_agent.word_reader import normalize


def word_table(rows: list[list[str]]) -> dict:
    cells = [
        {"row": row, "col": col, "rowspan": 1, "colspan": 1, "text": text}
        for row, values in enumerate(rows)
        for col, text in enumerate(values)
    ]
    table_text = "\n".join(cell["text"] for cell in cells if cell["text"])
    return {
        "evidence_id": "word.table.0000",
        "kind": "word_native_table",
        "table_index": 0,
        "rows": len(rows),
        "cols": max(len(values) for values in rows),
        "cells": cells,
        "text": table_text,
        "normalized_text": normalize(table_text),
        "html": "<table><tr><td>native</td></tr></table>",
        "required_coverage": False,
    }


ROWS = [
    ["序号", "说明"],
    ["01", "机组启动检查"],
    ["02", "阀门开度确认"],
    ["03", "压力表读数"],
]


def page_with_fragment(lines: list[str], html: str = "<table><tr><td>model</td></tr></table>") -> tuple[dict, list[dict], dict]:
    evidence = {f"pdf.line.{index}": {"evidence_id": f"pdf.line.{index}", "kind": "pdf_line", "text": text}
                for index, text in enumerate(lines)}
    page = {"evidence": {"pdf": list(evidence.values())}}
    block = {
        "block_id": "candidate.block.p1.0000",
        "type": "table",
        "source_refs": list(evidence),
        "bbox_image_px": [100, 200, 900, 600],
        "html": html,
    }
    reviews = {"blocks": {block["block_id"]: {}}}
    return page, [block], reviews


def fragment_texts(row_range: range) -> list[str]:
    return ["".join(row) for row in ROWS[row_range.start:row_range.stop]]


def test_weak_document_binding_still_yields_exact_row_coverage() -> None:
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.82)
    block = blocks[0]
    assert block["word_table_ref"] == "word.table.0000"
    assert block["word_table_shape"] == [4, 2]
    assert block["word_row_range"] == [0, 2]
    assert block["word_table_binding_scope"] == "table_fragment"
    assert block["word_table_match_score"] >= 0.75
    # the model HTML is *not* replaced: that stays gated on the document level score
    assert block["html"] == "<table><tr><td>model</td></tr></table>"
    assert "html" not in reviews["blocks"][block["block_id"]]


def test_missing_document_score_is_treated_as_table_local() -> None:
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], None)
    assert blocks[0]["word_row_range"] == [0, 2]
    assert blocks[0]["word_table_binding_scope"] == "table_fragment"


def test_weak_document_binding_rejects_a_loosely_matching_table() -> None:
    page, blocks, reviews = page_with_fragment(["完全无关的另一段文字内容", "与任何原生表格行都不匹配"])
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.60)
    assert "word_table_ref" not in blocks[0]


def test_strong_document_binding_replaces_the_model_html() -> None:
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.995)
    block = blocks[0]
    assert block["word_row_range"] == [0, 2]
    assert block["word_table_binding_scope"] == "document"
    assert "机组启动检查" in block["html"]
    review = reviews["blocks"][block["block_id"]]["html"]
    assert review["method"] == "word_native_rows_pdf_fragment_binding"
    assert review["document_binding_score"] == 0.995


def test_inconsistent_row_coverage_is_withdrawn() -> None:
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.80)
    assert blocks[0]["word_row_range"] == [0, 2]
    pages = [{"page": 1, "current_candidate": {"blocks": blocks}, "field_reviews": reviews, "evidence": {"word": []}}]
    issues = [{"word_table_ref": "word.table.0000", "block_id": blocks[0]["block_id"], "issue": "word_fragment_row_gap_or_overlap"}]
    withdrawn = _withdraw_unverified_word_rows(pages, issues)
    assert withdrawn and withdrawn[0]["word_table_ref"] == "word.table.0000"
    assert "word_row_range" not in blocks[0]
    assert blocks[0]["word_table_row_evidence"] == "withdrawn"
    # the withdrawal is evidence bookkeeping: it must not invent a field review status
    assert "word_row_coverage" not in reviews["blocks"][blocks[0]["block_id"]]


def test_consistent_row_coverage_is_kept() -> None:
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.80)
    pages = [{"page": 1, "current_candidate": {"blocks": blocks}, "field_reviews": reviews, "evidence": {"word": []}}]
    assert _withdraw_unverified_word_rows(pages, []) == []
    assert blocks[0]["word_row_range"] == [0, 2]


def test_withdrawn_rows_revert_the_replaced_html() -> None:
    """A native HTML replacement is only valid together with the row coverage it came from."""
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.995)
    assert "机组启动检查" in blocks[0]["html"]
    pages = [{"page": 1, "current_candidate": {"blocks": blocks}, "field_reviews": reviews, "evidence": {"word": []}}]
    _withdraw_unverified_word_rows(pages, [{"word_table_ref": "word.table.0000", "block_id": blocks[0]["block_id"], "issue": "word_fragment_row_gap_or_overlap"}])
    assert blocks[0]["html"] == "<table><tr><td>model</td></tr></table>"
    assert "html" not in reviews["blocks"][blocks[0]["block_id"]]
    assert "_word_html_backup" not in blocks[0]


def test_table_with_an_embedded_image_keeps_the_model_html() -> None:
    """Native rows cannot carry a picture bound into a cell: row coverage yes, HTML replacement no."""
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    blocks[0]["embedded_images"] = [{"image_ref": "pdf.image.p1.0000", "row": 1, "col": 0, "rowspan": 1, "colspan": 1}]
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.995)
    assert blocks[0]["word_row_range"] == [0, 2]
    assert blocks[0]["html"] == "<table><tr><td>model</td></tr></table>"
    assert "html" not in reviews["blocks"][blocks[0]["block_id"]]


def test_table_level_failure_withdraws_every_fragment_of_that_table() -> None:
    """A table that never lines up (or never appears) must not keep row coverage anywhere."""
    page, blocks, reviews = page_with_fragment(fragment_texts(range(0, 3)))
    _attach_word_tables(page, blocks, reviews, [word_table(ROWS)], 0.80)
    pages = [{"page": 1, "current_candidate": {"blocks": blocks}, "field_reviews": reviews, "evidence": {"word": []}}]
    issues = [{"word_table_ref": "word.table.0000", "issue": "word_table_rows_incomplete", "covered_rows": 3, "expected_rows": 4}]
    withdrawn = _withdraw_unverified_word_rows(pages, issues)
    assert len(withdrawn) == 1
    assert "word_row_range" not in blocks[0]


def test_weak_document_binding_can_still_build_a_native_continuation_fragment() -> None:
    """Row coverage on the next page comes from the same native table, so the document score is not the gate."""
    from ocr_agent.evidence_builder import _add_word_continuation_fragments

    table = word_table(ROWS)
    def drawing(evidence_id: str, box: list[float]) -> dict:
        return {"evidence_id": evidence_id, "kind": "pdf_drawing", "bbox_image_px": box}

    line_a = {"evidence_id": "pdf.line.a", "kind": "pdf_line", "text": "03", "bbox_image_px": [110, 310, 190, 350]}
    line_b = {"evidence_id": "pdf.line.b", "kind": "pdf_line", "text": "压力表读数", "bbox_image_px": [210, 310, 640, 350]}
    first = {
        "page": 1,
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 500.0, "height_pt": 700.0},
        "evidence": {"pdf": [], "word": [table]},
        "field_reviews": {"blocks": {"first": {}}},
        "current_candidate": {"blocks": [{"block_id": "first", "type": "table", "bbox_image_px": [100, 200, 900, 600],
                                           "word_table_ref": "word.table.0000", "word_table_shape": [4, 2],
                                           "word_row_range": [0, 2], "source_refs": ["pdf.line.x"]}]},
    }
    second = {
        "page": 2,
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 500.0, "height_pt": 700.0},
        "evidence": {"pdf": [drawing("top", [100, 300, 900, 302]), drawing("bottom", [100, 560, 900, 562]),
                             drawing("left", [99, 300, 101, 562]), drawing("right", [899, 300, 901, 562]), line_a, line_b],
                     "word": [table]},
        "field_reviews": {"blocks": {"b1": {}, "b2": {}}},
        "detected_conflicts": [],
        "current_candidate": {"blocks": [
            {"block_id": "b1", "type": "text", "text": "03", "bbox_image_px": [110, 310, 190, 350], "reading_order": 0, "source_refs": ["pdf.line.a"], "support_refs": []},
            {"block_id": "b2", "type": "text", "text": "压力表读数", "bbox_image_px": [210, 310, 640, 350], "reading_order": 1, "source_refs": ["pdf.line.b"], "support_refs": []},
        ]},
    }
    _add_word_continuation_fragments([first, second], 0.70)  # weak document binding
    created = [block for block in second["current_candidate"]["blocks"] if block.get("word_table_ref") == "word.table.0000"]
    assert len(created) == 1
    continuation = created[0]
    assert continuation["word_row_range"] == [3, 3]
    assert "压力表读数" in continuation["html"]
    review = second["field_reviews"]["blocks"][continuation["block_id"]]["html"]
    assert review["binding_scope"] == "table_fragment"
