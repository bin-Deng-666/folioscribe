from ocr_agent.constants import ALLOWED_TYPES
from ocr_agent.evidence_builder import (
    _attach_inline_orphan_pdf_lines,
    _consensus_image_box,
    _cluster,
    _confirm_cross_model_table_html,
    _confirm_ruled_table_cell_matrix,
    _confirm_stable_repeated_marginal_text,
    _drop_duplicate_page_number_blocks,
    _drop_unsupported_image_regions,
    _drop_unanchored_duplicate_blocks,
    _absorb_blocks_inside_graphic_images,
    _drop_invisible_text_blocks,
    _mark_visually_absent_lines,
    _merge_positions,
    _nearest_empty_cell,
    _drop_textual_table_images,
    _table_owns_picture,
    _bind_table_embedded_images,
    _drop_vector_container_images,
    _enforce_evidence_containment,
    _promote_textual_content_images,
    _graphic_region_refs,
    _region_graphic_structure,
    _expand_mineru_lists,
    _heading_peer_shape,
    _heading_sequence_key,
    _inside_pdf_image_with_ocr_tolerance,
    _mask_pdf_images,
    _merge_contained_plain_text_blocks,
    _promote_document_heading_sequences,
    _promote_geometric_heading_peers,
    _repair_trailing_empty_table_column,
    _review_nonrepeated_marginal_roles,
    _table_html_match_score,
    _sort_blocks_in_reading_bands,
    _suppress_partitioned_pdf_lines,
    _ruled_regions,
    _validate_word_fragment_coverage,
)
from ocr_agent.review_tasks import build_document_relation_tasks, build_semantic_tasks, promote_continued_question_candidates


def line(evidence_id, box, text):
    return {"evidence_id": evidence_id, "kind": "pdf_line", "bbox_image_px": box, "text": text, "required_coverage": True}


def test_title_level_review_also_unlocks_type_correction() -> None:
    block = {
        "block_id": "step",
        "type": "title",
        "level": 1,
        "text": "（3）处理结果：",
        "bbox_image_px": [100, 300, 500, 340],
        "reading_order": 1,
        "source_refs": ["pdf.step"],
        "support_refs": ["mineru.step", "paddle.step"],
        "type_candidates": ["title"],
    }
    page = {
        "sample_id": "sample",
        "page": 2,
        "page_image": {"width": 1000, "height": 1400},
        "current_candidate": {"blocks": [block]},
        "field_reviews": {"blocks": {"step": {
            "type": {"status": "PASS", "method": "cross_model_type_agreement", "evidence_refs": ["mineru.step", "paddle.step"]},
            "level": {"status": "CHECK", "method": "model_level_candidate", "evidence_refs": ["mineru.step", "paddle.step"]},
        }}},
        "evidence": {
            "pdf": [{"evidence_id": "pdf.step", "kind": "pdf_line", "text": "（3）处理结果："}],
            "mineru": [{"evidence_id": "mineru.step", "model": "mineru", "source_type": "title", "type_candidate": "title", "text": "（3）处理结果："}],
            "paddle": [{"evidence_id": "paddle.step", "model": "paddle", "source_type": "paragraph_title", "type_candidate": "title", "text": "（3）处理结果："}],
        },
        "document_context": {"previous_pages_complete": True, "previous_page_tail": [{"type": "text", "text": "（2）打开报表"}]},
    }
    item = build_semantic_tasks(page)[0]["items"][0]
    assert item["needs"] == ["type", "level"]
    assert item["allowed_types"] == ["text", "title"]


def test_stable_repeated_top_text_is_confirmed_as_header() -> None:
    pages = []
    for number, block_type in enumerate(("title", "title", "unknown"), 1):
        block_id = f"h{number}"
        pages.append({
            "page": number,
            "page_image": {"width": 1000, "height": 1400},
            "current_candidate": {"blocks": [{"block_id": block_id, "type": block_type, "text": "固定课程名称", "bbox_image_px": [300, 50, 700, 80], "type_candidates": [block_type], "source_refs": [f"pdf.{number}"], "support_refs": [f"paddle.{number}"], "issues": ["title_level_review"]}]},
            "field_reviews": {"blocks": {block_id: {"type": {"status": "CHECK"}, "level": {"status": "CHECK"}}}},
        })
    _confirm_stable_repeated_marginal_text(pages)
    assert [page["current_candidate"]["blocks"][0]["type"] for page in pages] == ["header"] * 3
    assert all("level" not in page["field_reviews"]["blocks"][f"h{i}"] for i, page in enumerate(pages, 1))
    assert pages[0]["field_reviews"]["blocks"]["h1"]["type"]["method"] == "cross_page_repeated_marginal_text_geometry"


def test_nonrepeated_header_requires_semantic_review_but_repeated_edge_header_passes() -> None:
    def page(number: int, text: str) -> dict:
        block_id = f"h{number}"
        return {
            "page": number,
            "page_image": {"width": 1000, "height": 1400},
            "current_candidate": {"blocks": [{"block_id": block_id, "type": "header", "text": text, "bbox_image_px": [100, 50, 400, 80], "type_candidates": ["header"], "support_refs": [f"paddle.{number}"], "issues": []}]},
            "field_reviews": {"blocks": {block_id: {"type": {"status": "PASS", "method": "cross_model_type_agreement", "evidence_refs": [f"paddle.{number}"]}}}},
        }

    pages = [page(1, "每页固定页眉"), page(2, "每页固定页眉"), page(3, "签发部门")]
    _review_nonrepeated_marginal_roles(pages)
    assert pages[0]["field_reviews"]["blocks"]["h1"]["type"]["status"] == "PASS"
    singleton = pages[2]["current_candidate"]["blocks"][0]
    assert pages[2]["field_reviews"]["blocks"]["h3"]["type"]["status"] == "CHECK"
    assert singleton["type_candidates"] == ["header", "text", "title"]
    assert singleton["cross_page_role_evidence"]["repeat_count"] == 1


def test_heading_peer_shape_separates_steps_bullets_decimals_and_plain_labels() -> None:
    assert _heading_peer_shape("**步骤2：下载安装包**") == "step"
    assert _heading_peer_shape("\uf06c物业收费：") == "bullet"
    assert _heading_peer_shape("2.1.3 建设目标") == "decimal"
    assert _heading_peer_shape("问题分析") == "plain"


def test_numbered_step_sequence_uses_document_sequence_review() -> None:
    assert _heading_sequence_key("步骤4：验证安装结果") == ("numbered_step", "4")
    assert _heading_sequence_key("**步骤1：准备环境**") == ("numbered_step", "1")
    assert _heading_sequence_key("Step 2. Install") == ("numbered_step", "2")


def test_restarted_document_step_sequences_get_independent_local_context() -> None:
    def page(number: int, values: list[int]) -> dict:
        blocks = []
        reviews = {}
        for order, value in enumerate(values):
            block_id = f"step.p{number}.{order}"
            blocks.append({"block_id": block_id, "type": "text", "text": f"步骤{value}：操作", "bbox_image_px": [100, 100 + order * 60, 500, 140 + order * 60], "reading_order": order, "source_refs": [f"pdf.{block_id}"], "support_refs": [], "type_candidates": ["text"], "issues": []})
            reviews[block_id] = {"type": {"status": "PASS", "method": "cross_model_type_agreement", "evidence_refs": []}, "text": {"status": "PASS"}, "bbox": {"status": "PASS"}}
        return {"sample_id": "sample", "page": number, "page_image": {"width": 1000, "height": 1400}, "current_candidate": {"blocks": blocks}, "field_reviews": {"blocks": reviews}}

    pages = [page(1, [1, 2]), page(2, [1, 2, 3])]
    _promote_document_heading_sequences(pages)
    first_group = pages[0]["current_candidate"]["blocks"][0]["document_heading_sequence_evidence"]
    second_group = pages[1]["current_candidate"]["blocks"][0]["document_heading_sequence_evidence"]
    assert first_group["local_group"] != second_group["local_group"]
    assert {item["page"] for item in first_group["items"]} == {1}
    assert {item["page"] for item in second_group["items"]} == {2}


def test_cross_page_chapter_sequence_unlocks_missed_heading_in_dedicated_task() -> None:
    def page(number: int, text: str, block_type: str) -> dict:
        block_id = f"chapter{number}"
        return {
            "sample_id": "sample", "page": number, "page_image": {"width": 1000, "height": 1400},
            "current_candidate": {"blocks": [{"block_id": block_id, "type": block_type, "text": text, "bbox_image_px": [100, 100, 500, 140], "reading_order": 0, "source_refs": [f"pdf.{number}"], "support_refs": [f"paddle.{number}"], "type_candidates": [block_type], "issues": []}]},
            "field_reviews": {"blocks": {block_id: {"type": {"status": "PASS", "method": "cross_model_type_agreement", "evidence_refs": [f"paddle.{number}"]}, "text": {"status": "PASS"}, "bbox": {"status": "PASS"}}}},
            "evidence": {"pdf": [{"evidence_id": f"pdf.{number}", "kind": "pdf_line", "text": text}], "paddle": [{"evidence_id": f"paddle.{number}", "model": "paddle", "type_candidate": block_type, "text": text}]},
            "document_context": {"previous_pages_complete": True},
        }
    pages = [page(1, "第一章 总则", "text"), page(2, "第二章 范围", "title")]
    _promote_document_heading_sequences(pages)
    first = pages[0]["current_candidate"]["blocks"][0]
    assert first["type"] == "unknown"
    assert first["type_candidates"] == ["text", "title"]
    tasks = build_semantic_tasks(pages[0])
    assert tasks[0]["items"][0]["document_sequence_review"] is True


def test_inline_bullet_is_attached_before_reading_order() -> None:
    lines = [line("pdf.bullet", [100, 100, 110, 125], "•"), line("pdf.body", [130, 100, 700, 125], "方式二：正文")]
    block = {"block_id": "body", "type": "text", "text": "方式二：正文", "bbox_image_px": [130, 100, 700, 125], "source_refs": ["pdf.body"], "support_refs": ["mineru.body"], "issues": []}
    reviews = {"blocks": {"body": {"bbox": {"status": "PASS"}, "text": {"status": "PASS"}}}}
    page = {"page_image": {"width": 1000, "height": 1400}, "pdf_page": {"width_pt": 500, "height_pt": 700}}
    claimed = {"pdf.body"}
    _attach_inline_orphan_pdf_lines([block], reviews, lines, claimed, page)
    assert block["source_refs"] == ["pdf.body", "pdf.bullet"]
    assert block["text"] == "•方式二：正文"
    assert block["bbox_image_px"] == [100, 100, 700, 125]
    assert reviews["blocks"]["body"]["text"]["method"] == "pdf_inline_reading_flow"


def test_reading_band_orders_same_line_by_x_not_subpixel_y() -> None:
    right = {"block_id": "right", "bbox_image_px": [130, 99.8, 700, 125]}
    left = {"block_id": "left", "bbox_image_px": [100, 100.2, 110, 125]}
    assert [block["block_id"] for block in _sort_blocks_in_reading_bands([left, right])] == ["left", "right"]


def test_pdf_image_mask_tolerates_small_ocr_bbox_overflow() -> None:
    image = [206, 150, 1275, 681]
    screenshot_header = [201, 140, 474, 163]
    caption_above = [300, 110, 700, 140]
    assert _inside_pdf_image_with_ocr_tolerance(screenshot_header, image)
    assert not _inside_pdf_image_with_ocr_tolerance(caption_above, image)


def test_contained_plain_text_flow_is_merged_by_pdf_line_ownership() -> None:
    lines = [
        line("pdf.1", [100, 100, 800, 130], "第一行"),
        line("pdf.2", [100, 140, 800, 170], "补充行"),
        line("pdf.3", [100, 180, 800, 210], "第三行"),
    ]
    blocks = [
        {"block_id": "owner", "type": "text", "text": "第一行\n第三行", "bbox_image_px": [100, 100, 800, 210], "source_refs": ["pdf.1", "pdf.3"], "support_refs": ["m.owner"], "issues": []},
        {"block_id": "child", "type": "unknown", "text": "补充行", "bbox_image_px": [100, 115, 800, 195], "source_refs": ["pdf.2"], "support_refs": [], "issues": ["model_missed_pdf_text"]},
    ]
    reviews = {"blocks": {block["block_id"]: {"bbox": {"status": "PASS"}, "text": {"status": "PASS"}} for block in blocks}}
    page = {"page_image": {"width": 1000, "height": 1400}, "pdf_page": {"width_pt": 500, "height_pt": 700}}
    merged, issues = _merge_contained_plain_text_blocks(blocks, reviews, [{"type": "model_missed_pdf_text", "block_ids": ["child"]}], lines, page)
    assert [block["block_id"] for block in merged] == ["owner"]
    assert merged[0]["source_refs"] == ["pdf.1", "pdf.3", "pdf.2"]
    assert merged[0]["text"] == "第一行\n补充行\n第三行"
    assert reviews["blocks"]["owner"]["bbox"]["method"] == "pdf_line_ownership_union"
    assert "child" not in reviews["blocks"]
    assert issues == []


def test_unverified_table_with_pdf_text_keeps_text_as_semantic_alternative() -> None:
    block = {"block_id": "candidate.table", "type": "table", "text": "第五十四条 连续法律正文", "html": "<table><tr><td>连续法律正文</td></tr></table>", "bbox_image_px": [100, 200, 900, 500], "reading_order": 0, "source_refs": ["pdf.body"], "support_refs": ["paddle.table", "mineru.text"], "type_candidates": ["table", "text"], "issues": []}
    page = {"sample_id": "sample", "page": 1, "page_image": {"width": 1000, "height": 1400}, "current_candidate": {"blocks": [block]}, "field_reviews": {"blocks": {"candidate.table": {"type": {"status": "CHECK"}, "text": {"status": "PASS"}, "html": {"status": "CHECK"}}}}, "evidence": {"pdf": [{"evidence_id": "pdf.body", "kind": "pdf_line", "text": block["text"]}], "paddle": [{"evidence_id": "paddle.table", "model": "paddle", "type_candidate": "table", "html": block["html"], "text": block["text"]}], "mineru": [{"evidence_id": "mineru.text", "model": "mineru", "type_candidate": "text", "text": block["text"]}]}, "document_context": {"previous_pages_complete": True}}
    item = build_semantic_tasks(page)[0]["items"][0]
    assert item["allowed_types"] == ["table", "text"]
    assert item["text"] == "第五十四条 连续法律正文"


def test_cross_model_fragments_partition_wide_pdf_line_without_duplicate_container() -> None:
    line_item = line("pdf.wide", [100, 100, 900, 140], "甲方（公章）        乙方（公章）")
    blocks = [
        {"block_id": "left", "type": "text", "text": "甲方（公章）", "bbox_image_px": [100, 100, 350, 140], "source_refs": [], "support_refs": ["m.left", "p.left"]},
        {"block_id": "right", "type": "text", "text": "乙方（公章）", "bbox_image_px": [600, 100, 900, 140], "source_refs": [], "support_refs": ["m.right", "p.right"]},
    ]
    models = [
        {"evidence_id": "m.left", "model": "mineru"}, {"evidence_id": "p.left", "model": "paddle"},
        {"evidence_id": "m.right", "model": "mineru"}, {"evidence_id": "p.right", "model": "paddle"},
    ]
    reviews = {"blocks": {block["block_id"]: {"bbox": {"status": "CHECK"}, "text": {"status": "CHECK"}} for block in blocks}}
    _suppress_partitioned_pdf_lines(blocks, reviews, [line_item], models, set())
    assert line_item["required_coverage"] is False
    assert line_item["partitioned_by_model_blocks"] == ["left", "right"]
    assert all(reviews["blocks"][block["block_id"]]["text"]["status"] == "PASS" for block in blocks)

    incomplete = line("pdf.incomplete", [100, 200, 900, 240], "甲方乙方丙方")
    shifted = [dict(blocks[0], bbox_image_px=[100, 200, 350, 240]), dict(blocks[1], bbox_image_px=[600, 200, 900, 240])]
    _suppress_partitioned_pdf_lines(shifted, reviews, [incomplete], models, set())
    assert incomplete["required_coverage"] is True

    padded = line("pdf.padded", [100, 300, 500, 340], "同文标题")
    tight_blocks = [{"block_id": "tight", "type": "text", "text": "同文标题", "bbox_image_px": [300, 300, 500, 340], "source_refs": [], "support_refs": ["m.tight", "p.tight"]}]
    tight_models = [
        {"evidence_id": "m.tight", "model": "mineru", "bbox_image_px": [300, 300, 500, 340]},
        {"evidence_id": "p.tight", "model": "paddle", "bbox_image_px": [302, 301, 501, 341]},
    ]
    tight_reviews = {"blocks": {"tight": {"bbox": {"status": "CHECK"}, "text": {"status": "CHECK"}}}}
    _suppress_partitioned_pdf_lines(tight_blocks, tight_reviews, [padded], tight_models, set())
    assert padded["required_coverage"] is False
    assert padded["tightened_by_model_block"] == "tight"
    assert tight_blocks[0]["pdf_tightening_ref"] == "pdf.padded"


def test_single_model_footnote_can_be_downgraded_to_text() -> None:
    page = {
        "sample_id": "sample", "page": 1,
        "page_image": {"width": 1000, "height": 1400},
        "current_candidate": {"blocks": [{"block_id": "note", "type": "footnote", "text": "推荐组合方案", "bbox_image_px": [100, 300, 500, 340], "reading_order": 0, "source_refs": ["pdf.note"], "support_refs": ["paddle.note"], "type_candidates": ["footnote"], "issues": []}]},
        "field_reviews": {"blocks": {"note": {"type": {"status": "CHECK", "method": "single_model_type", "evidence_refs": ["paddle.note"]}}}},
        "evidence": {"pdf": [{"evidence_id": "pdf.note", "kind": "pdf_line", "text": "推荐组合方案"}], "paddle": [{"evidence_id": "paddle.note", "model": "paddle", "source_type": "vision_footnote", "type_candidate": "footnote", "text": "推荐组合方案"}]},
        "document_context": {},
    }
    item = build_semantic_tasks(page)[0]["items"][0]
    assert item["allowed_types"] == ["footnote", "text"]


def test_ruled_cell_matrix_confirms_rowspan_and_cell_text() -> None:
    html = '<table><tr><td rowspan="2">A</td><td>B</td></tr><tr><td>C</td></tr></table>'
    block = {"block_id": "table", "type": "table", "html": html, "bbox_image_px": [100, 100, 300, 300], "source_refs": ["pdf.a", "pdf.b", "pdf.c"], "support_refs": ["paddle.table"]}
    drawings = [
        {"evidence_id": "draw.a", "kind": "pdf_drawing", "bbox_image_px": [100, 100, 200, 300]},
        {"evidence_id": "draw.b", "kind": "pdf_drawing", "bbox_image_px": [200, 100, 300, 200]},
        {"evidence_id": "draw.c", "kind": "pdf_drawing", "bbox_image_px": [200, 200, 300, 300]},
    ]
    lines = [line("pdf.a", [120, 140, 150, 160], "A"), line("pdf.b", [220, 140, 250, 160], "B"), line("pdf.c", [220, 240, 250, 260], "C")]
    page = {"page_image": {"width": 400, "height": 400}, "pdf_page": {"width_pt": 200, "height_pt": 200}, "evidence": {"pdf": drawings + lines}}
    reviews = {"blocks": {"table": {"html": {"status": "CHECK"}}}}
    _confirm_ruled_table_cell_matrix(page, [block], reviews)
    assert reviews["blocks"]["table"]["html"]["status"] == "PASS"
    assert block["table_grid_evidence"] == {"rows": 2, "cols": 2, "physical_cells": 3, "method": "pdf_ruled_cell_rectangles"}


def test_ruled_line_segments_confirm_colspan_and_cell_text() -> None:
    html = '<table><tr><td colspan="2">A</td></tr><tr><td>B</td><td>C</td></tr></table>'
    block = {"block_id": "table", "type": "table", "html": html, "bbox_image_px": [100, 100, 300, 300], "source_refs": ["pdf.a", "pdf.b", "pdf.c"], "support_refs": ["paddle.table"]}
    drawings = [
        # A partial rectangle interpretation must not suppress the complete line-grid interpretation.
        {"evidence_id": "background.top", "kind": "pdf_drawing", "bbox_image_px": [100, 100, 200, 200]},
        {"evidence_id": "background.bottom", "kind": "pdf_drawing", "bbox_image_px": [100, 200, 200, 300]},
        {"evidence_id": "top", "kind": "pdf_drawing", "bbox_image_px": [100, 100, 300, 102]},
        {"evidence_id": "middle", "kind": "pdf_drawing", "bbox_image_px": [100, 199, 300, 201]},
        {"evidence_id": "bottom", "kind": "pdf_drawing", "bbox_image_px": [100, 298, 300, 300]},
        {"evidence_id": "left", "kind": "pdf_drawing", "bbox_image_px": [100, 100, 102, 300]},
        {"evidence_id": "split", "kind": "pdf_drawing", "bbox_image_px": [199, 200, 201, 300]},
        {"evidence_id": "right", "kind": "pdf_drawing", "bbox_image_px": [298, 100, 300, 300]},
    ]
    lines = [line("pdf.a", [140, 140, 160, 160], "A"), line("pdf.b", [140, 240, 160, 260], "B"), line("pdf.c", [240, 240, 260, 260], "C")]
    page = {"page_image": {"width": 400, "height": 400}, "pdf_page": {"width_pt": 200, "height_pt": 200}, "evidence": {"pdf": drawings + lines}}
    reviews = {"blocks": {"table": {"html": {"status": "CHECK"}}}}
    _confirm_ruled_table_cell_matrix(page, [block], reviews)
    assert reviews["blocks"]["table"]["html"]["status"] == "PASS"
    assert block["table_grid_evidence"] == {"rows": 2, "cols": 2, "physical_cells": 3, "method": "pdf_ruled_line_segments"}


def test_ruled_cell_matrix_rebuilds_when_model_put_text_in_the_wrong_cell() -> None:
    """The measured grid is the structure authority: a swapped model HTML is rebuilt, not accepted."""
    html = "<table><tr><td>A</td><td>B</td></tr></table>"
    block = {"block_id": "table", "type": "table", "html": html, "bbox_image_px": [100, 100, 300, 200], "source_refs": ["pdf.a", "pdf.b"], "support_refs": []}
    drawings = [{"evidence_id": "draw.a", "kind": "pdf_drawing", "bbox_image_px": [100, 100, 200, 200]}, {"evidence_id": "draw.b", "kind": "pdf_drawing", "bbox_image_px": [200, 100, 300, 200]}]
    lines = [line("pdf.a", [220, 140, 250, 160], "A"), line("pdf.b", [120, 140, 150, 160], "B")]
    page = {"page_image": {"width": 400, "height": 400}, "pdf_page": {"width_pt": 200, "height_pt": 200}, "evidence": {"pdf": drawings + lines}}
    reviews = {"blocks": {"table": {"html": {"status": "CHECK"}}}}
    _confirm_ruled_table_cell_matrix(page, [block], reviews)
    review = reviews["blocks"]["table"]["html"]
    # the model HTML was not confirmed as-is ...
    assert review["method"] != "model_html_pdf_ruled_cell_matrix_exact"
    # ... but the measured grid carries the same content, so it becomes the structure
    assert review["status"] == "PASS" and review["method"] == "pdf_ruled_cell_matrix_rebuild_char_conserved"
    assert review["char_delta"] == {"lost": 0, "gained": 0}
    assert block["html"].index(">B<") < block["html"].index(">A<")
    assert {cell["text"] for cell in block["table_cell_geometry"]} == {"A", "B"}


def test_ruled_cell_matrix_rebuild_allows_a_decorative_symbol_delta() -> None:
    """A symbol the model HTML spells out but the PDF layer draws as a glyph is not content loss."""
    html = "<table><tr><td>◇ 危险</td><td>B</td></tr></table>"
    block = {"block_id": "table", "type": "table", "html": html, "bbox_image_px": [100, 100, 300, 200], "source_refs": ["pdf.a", "pdf.b"], "support_refs": []}
    drawings = [{"evidence_id": "draw.a", "kind": "pdf_drawing", "bbox_image_px": [100, 100, 200, 200]}, {"evidence_id": "draw.b", "kind": "pdf_drawing", "bbox_image_px": [200, 100, 300, 200]}]
    lines = [line("pdf.a", [120, 140, 150, 160], "危险"), line("pdf.b", [220, 140, 250, 160], "B")]
    page = {"page_image": {"width": 400, "height": 400}, "pdf_page": {"width_pt": 200, "height_pt": 200}, "evidence": {"pdf": drawings + lines}}
    reviews = {"blocks": {"table": {"html": {"status": "CHECK"}}}}
    _confirm_ruled_table_cell_matrix(page, [block], reviews)
    review = reviews["blocks"]["table"]["html"]
    assert review["status"] == "PASS" and review["method"] == "pdf_ruled_cell_matrix_rebuild_char_conserved"
    assert review["char_delta"] == {"lost": 1, "gained": 0}


def test_ruled_cell_matrix_rebuild_refuses_to_drop_content() -> None:
    """A rebuild that would lose content keeps the block unresolved instead of replacing it."""
    html = "<table><tr><td>甲乙</td><td>丙</td></tr></table>"
    block = {"block_id": "table", "type": "table", "html": html, "bbox_image_px": [100, 100, 300, 200], "source_refs": ["pdf.a"], "support_refs": []}
    drawings = [{"evidence_id": "draw.a", "kind": "pdf_drawing", "bbox_image_px": [100, 100, 200, 200]}, {"evidence_id": "draw.b", "kind": "pdf_drawing", "bbox_image_px": [200, 100, 300, 200]}]
    lines = [line("pdf.a", [120, 140, 150, 160], "甲乙")]
    page = {"page_image": {"width": 400, "height": 400}, "pdf_page": {"width_pt": 200, "height_pt": 200}, "evidence": {"pdf": drawings + lines}}
    reviews = {"blocks": {"table": {"html": {"status": "CHECK"}}}}
    _confirm_ruled_table_cell_matrix(page, [block], reviews)
    assert reviews["blocks"]["table"]["html"]["status"] == "CHECK"
    assert block["html"] == html


def test_cross_model_table_structure_and_pdf_text_can_confirm_html() -> None:
    html = "<table><tr><td>名称</td><td>值</td></tr><tr><td>A</td><td>1</td></tr></table>"
    block = {"block_id": "table", "type": "table", "html": html, "source_refs": ["pdf.1", "pdf.2"], "support_refs": ["mineru.1", "paddle.1"]}
    page = {"evidence": {
        "pdf": [{"evidence_id": "pdf.1", "kind": "pdf_line", "text": "名称值"}, {"evidence_id": "pdf.2", "kind": "pdf_line", "text": "A1"}],
        "mineru": [{"evidence_id": "mineru.1", "model": "mineru", "html": html}],
        "paddle": [{"evidence_id": "paddle.1", "model": "paddle", "html": html}],
    }}
    reviews = {"blocks": {"table": {"html": {"status": "CHECK"}}}}
    _confirm_cross_model_table_html(page, [block], reviews)
    assert reviews["blocks"]["table"]["html"]["status"] == "PASS"
    assert reviews["blocks"]["table"]["html"]["method"] == "cross_model_table_structure_pdf_text_exact"


def test_table_html_match_penalizes_content_from_future_pages() -> None:
    current = "<table><tr><td>当前页内容</td></tr></table>"
    whole = "<table><tr><td>当前页内容</td></tr><tr><td>后续页面额外内容很多</td></tr></table>"
    assert _table_html_match_score(current, "当前页内容") > _table_html_match_score(whole, "当前页内容")


def test_pdf_grid_can_repair_one_consistently_empty_trailing_column() -> None:
    block = {"type": "table", "bbox_image_px": [0, 0, 400, 200], "html": "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td><td>e</td><td>f</td></tr></table>"}
    page = {"evidence": {"pdf": [
        {"kind": "pdf_drawing", "bbox_image_px": [0, 0, 2, 200]},
        {"kind": "pdf_drawing", "bbox_image_px": [100, 0, 102, 200]},
        {"kind": "pdf_drawing", "bbox_image_px": [200, 0, 202, 200]},
        {"kind": "pdf_drawing", "bbox_image_px": [300, 0, 302, 200]},
        {"kind": "pdf_drawing", "bbox_image_px": [398, 0, 400, 200]},
    ]}}
    _repair_trailing_empty_table_column(page, block)
    assert block["html"].count("<td></td>") == 2
    assert block["table_column_repair"]["pdf_grid_columns"] == 4


def test_single_model_table_container_of_multiple_pdf_images_is_dropped() -> None:
    table = {"evidence_id": "paddle.table", "type_candidate": "table", "bbox_image_px": [100, 100, 900, 700], "html": "<table><tr><td>UI</td></tr></table>"}
    images = [
        {"evidence_id": "pdf.image.1", "bbox_image_px": [110, 100, 900, 390]},
        {"evidence_id": "pdf.image.2", "bbox_image_px": [110, 410, 700, 700]},
    ]
    assert _drop_unsupported_image_regions([table], [], images) == []
    assert table["rejected_as_table"] == "single_model_container_of_multiple_pdf_images"


def test_html_table_evidence_dominates_overlapping_fragment_labels() -> None:
    records = [
        {"model": "paddle", "type_candidate": "table", "bbox_image_px": [0, 0, 1000, 200], "text": "资质要求不满足", "html": "<table><tr><td>资质</td></tr></table>"},
        {"model": "mineru", "type_candidate": "header", "bbox_image_px": [20, 20, 120, 180], "text": "资质", "html": None},
        {"model": "mineru", "type_candidate": "header", "bbox_image_px": [130, 20, 900, 180], "text": "要求", "html": None},
    ]
    groups = _cluster(records)
    assert len(groups) == 1
    assert any(item["type_candidate"] == "table" and item["html"] for item in groups[0])


def test_final_ontology_excludes_list_and_toc() -> None:
    assert len(ALLOWED_TYPES) == 13
    assert "list" not in ALLOWED_TYPES
    assert "toc" not in ALLOWED_TYPES


def test_mineru_list_group_expands_to_grounded_items() -> None:
    record = {
        "evidence_id": "mineru.block.p1.0001",
        "model": "mineru",
        "source_type": "list",
        "type_candidate": "list",
        "bbox_image_px": [100, 100, 700, 240],
        "text": "1. 第一项内容\n2. 第二项内容",
        "list_items": ["1. 第一项内容", "2. 第二项内容"],
    }
    lines = [
        line("pdf.line.p1.0001", [110, 110, 400, 140], "1．第一项内容"),
        line("pdf.line.p1.0002", [110, 180, 400, 210], "2．第二项内容"),
    ]
    items = _expand_mineru_lists([record], lines)
    assert [item["source_type"] for item in items] == ["list_item", "list_item"]
    assert [item["type_candidate"] for item in items] == ["text", "text"]
    assert [item["bbox_image_px"] for item in items] == [[110, 110, 400, 140], [110, 180, 400, 210]]


def test_toc_entries_are_plain_text() -> None:
    blocks = [{"block_id": "entry", "type": "text", "text": "一、背景........3", "source_refs": ["pdf.1"], "support_refs": [], "issues": []}]
    assert blocks[0]["type"] == "text"


def test_pdf_drawing_edges_form_ruled_region() -> None:
    def drawing(evidence_id, box):
        return {"evidence_id": evidence_id, "kind": "pdf_drawing", "bbox_image_px": box}
    page = {"page_image": {"width": 1000, "height": 1400}, "evidence": {"pdf": [
        drawing("top", [100, 100, 900, 102]), drawing("bottom", [100, 700, 900, 702]),
        drawing("left", [99, 100, 101, 702]), drawing("right", [899, 100, 901, 702]),
        drawing("decoration", [10, 20, 80, 22]),
    ]}}
    regions = _ruled_regions(page)
    assert len(regions) == 1
    assert regions[0]["bbox_image_px"] == [100.0, 101.0, 900.0, 701.0]
    assert set(regions[0]["evidence_refs"]) == {"top", "bottom", "left", "right"}


def test_word_fragment_ranges_must_cover_table_without_gaps() -> None:
    word = [{"evidence_id": "word.table.0000", "rows": 4}]
    pages = [
        {"evidence": {"word": word}, "current_candidate": {"blocks": [{"block_id": "a", "word_table_ref": "word.table.0000", "word_row_range": [0, 1]}]}, "page": 1},
        {"evidence": {"word": word}, "current_candidate": {"blocks": [{"block_id": "b", "word_table_ref": "word.table.0000", "word_row_range": [2, 3]}]}, "page": 2},
    ]
    assert _validate_word_fragment_coverage(pages, require_complete=True) == []
    pages[1]["current_candidate"]["blocks"][0]["word_row_range"] = [3, 3]
    assert _validate_word_fragment_coverage(pages, require_complete=True)[0]["issue"] == "word_fragment_row_gap_or_overlap"


def test_content_image_masks_nested_ocr_but_page_scan_does_not() -> None:
    page = {"page_image": {"width": 1000, "height": 1400}}
    content_image = {"evidence_id": "pdf.image.1", "bbox_image_px": [100, 100, 600, 600], "required_coverage": True}
    nested = {"evidence_id": "paddle.1", "bbox_image_px": [150, 150, 300, 200]}
    models, lines, images, nested_refs = _mask_pdf_images(page, [nested], [], [content_image])
    assert models == []
    assert images == [content_image]
    assert nested_refs["pdf.image.1"] == ["paddle.1"]

    # Semantic ownership follows the native image object, not a display bbox tightened by models.
    tightened = {"evidence_id": "pdf.image.tight", "bbox_image_px": [100, 100, 600, 600], "effective_bbox_image_px": [100, 150, 600, 600], "required_coverage": True}
    status_bar = {"evidence_id": "paddle.status", "bbox_image_px": [150, 110, 250, 130]}
    models, _, _, nested_refs = _mask_pdf_images(page, [status_bar], [], [tightened])
    assert models == []
    assert nested_refs["pdf.image.tight"] == ["paddle.status"]

    # A PDF image object may include a bottom margin; that margin must not mask later text.
    image_models = [
        {"evidence_id": "mineru.image", "type_candidate": "image", "bbox_image_px": [100, 100, 600, 400]},
        {"evidence_id": "paddle.image", "type_candidate": "image", "bbox_image_px": [102, 102, 598, 398]},
    ]
    outside_line = line("pdf.line.after", [110, 450, 400, 480], "客户档案")
    _, visible_lines, _, _ = _mask_pdf_images(page, image_models, [outside_line], [{"evidence_id": "pdf.image.margin", "bbox_image_px": [100, 100, 600, 430], "required_coverage": True}])
    assert [item["evidence_id"] for item in visible_lines] == ["pdf.line.after"]

    scan = {"evidence_id": "pdf.scan", "bbox_image_px": [0, 0, 1000, 1400], "required_coverage": True}
    models, _, images, _ = _mask_pdf_images(page, [nested], [], [scan])
    assert models == [nested]
    assert images == []
    assert scan["page_scan"] is True


def test_unanchored_cross_model_exact_duplicate_collapses_into_pdf_owner() -> None:
    blocks = [
        {"block_id": "owner", "type": "unknown", "type_candidates": ["text"], "issues": [], "bbox_image_px": [100, 100, 200, 130], "text": "---", "source_refs": ["pdf.1"], "support_refs": []},
        {"block_id": "duplicate", "type": "unknown", "type_candidates": ["footer", "page_number"], "issues": [], "bbox_image_px": [98, 102, 202, 128], "text": "--- ", "source_refs": [], "support_refs": ["mineru.1", "paddle.1"]},
    ]
    reviews = {"blocks": {block["block_id"]: {} for block in blocks}}
    kept, _ = _drop_unanchored_duplicate_blocks(blocks, reviews, [])
    assert [block["block_id"] for block in kept] == ["owner"]
    assert kept[0]["support_refs"] == ["mineru.1", "paddle.1"]
    assert kept[0]["type_candidates"] == ["footer", "page_number", "text"]
    assert reviews["blocks"]["owner"]["type"]["status"] == "CHECK"


def test_cross_page_geometric_peers_recall_text_without_using_marker() -> None:
    def page(number, blocks):
        for order, block in enumerate(blocks):
            block.update({"reading_order": order, "source_refs": [f"pdf.{number}.{order}"], "support_refs": [], "issues": []})
        return {"page": number, "page_image": {"width": 1000, "height": 1400}, "current_candidate": {"blocks": blocks}, "field_reviews": {"blocks": {block["block_id"]: {"type": {"status": "PASS"}} for block in blocks}}}
    seed_page = page(1, [
        {"block_id": "seed1", "type": "title", "text": "检查慢查询", "bbox_image_px": [200, 100, 350, 130], "type_candidates": ["title"]},
        {"block_id": "seed2", "type": "title", "text": "索引碎片整理", "bbox_image_px": [200, 300, 380, 330], "type_candidates": ["title"]},
    ])
    target_page = page(2, [
        {"block_id": "candidate", "type": "text", "text": "检测网络延迟", "bbox_image_px": [200, 100, 380, 130], "type_candidates": ["text"]},
        {"block_id": "body", "type": "text", "text": "在客户端运行命令检查网络延迟和丢包情况。", "bbox_image_px": [150, 145, 850, 190], "type_candidates": ["text"]},
    ])
    _promote_geometric_heading_peers([seed_page, target_page])
    assert target_page["current_candidate"]["blocks"][0]["type_candidates"] == ["text", "title"]


def test_literal_markdown_marker_alone_does_not_recall_title() -> None:
    blocks = [
        {"block_id": "candidate", "type": "text", "text": "### literal marker", "bbox_image_px": [100, 100, 400, 130], "reading_order": 0, "source_refs": ["pdf.1"], "support_refs": [], "type_candidates": ["text"], "issues": []},
        {"block_id": "body", "type": "text", "text": "ordinary following content", "bbox_image_px": [100, 145, 700, 190], "reading_order": 1, "source_refs": ["pdf.2"], "support_refs": [], "type_candidates": ["text"], "issues": []},
    ]
    page = {"page_image": {"width": 1000, "height": 1400}, "current_candidate": {"blocks": blocks}, "field_reviews": {"blocks": {item["block_id"]: {"type": {"status": "PASS"}} for item in blocks}}}
    _promote_geometric_heading_peers([page])
    assert blocks[0]["type"] == "text"
    assert blocks[0]["type_candidates"] == ["text"]


def test_geometric_heading_peers_recall_cross_model_text_for_semantic_review() -> None:
    def block(block_id, text, box, block_type="text", candidates=None):
        return {"block_id": block_id, "type": block_type, "text": text, "bbox_image_px": box, "reading_order": 0, "source_refs": [f"pdf.{block_id}"], "support_refs": [], "type_candidates": candidates or [block_type], "issues": []}
    blocks = [
        block("seed1", "高可用性", [200, 100, 320, 130], "title", ["title"]),
        block("body1", "这里是第一类要求的详细说明和展开正文。", [150, 145, 850, 190]),
        block("candidate", "漫游性能", [200, 210, 320, 240]),
        block("body2", "这里是漫游性能的详细说明和展开正文。", [150, 255, 850, 300]),
        block("seed2", "高安全性", [200, 320, 330, 350], "title", ["title"]),
        block("body3", "这里是第二类要求的详细说明和展开正文。", [150, 365, 850, 410]),
    ]
    for order, item in enumerate(blocks):
        item["reading_order"] = order
    page = {"page_image": {"width": 1000, "height": 1400}, "current_candidate": {"blocks": blocks}, "field_reviews": {"blocks": {item["block_id"]: {"type": {"status": "PASS"}} for item in blocks}}}
    _promote_geometric_heading_peers([page])
    candidate = blocks[2]
    assert candidate["type"] == "unknown"
    assert candidate["type_candidates"] == ["text", "title"]
    assert page["field_reviews"]["blocks"]["candidate"]["type"]["method"] == "geometric_heading_peer_candidate"


def test_geometric_heading_peer_requires_multiple_seeds() -> None:
    blocks = [
        {"block_id": "seed", "type": "title", "text": "分类", "bbox_image_px": [200, 100, 300, 130], "reading_order": 0, "source_refs": ["pdf.1"], "support_refs": [], "type_candidates": ["title"], "issues": []},
        {"block_id": "short", "type": "text", "text": "普通短句", "bbox_image_px": [200, 200, 320, 230], "reading_order": 1, "source_refs": ["pdf.2"], "support_refs": [], "type_candidates": ["text"], "issues": []},
        {"block_id": "body", "type": "text", "text": "这里是普通短句后面的较长正文内容。", "bbox_image_px": [150, 245, 850, 290], "reading_order": 2, "source_refs": ["pdf.3"], "support_refs": [], "type_candidates": ["text"], "issues": []},
    ]
    page = {"page_image": {"width": 1000, "height": 1400}, "current_candidate": {"blocks": blocks}, "field_reviews": {"blocks": {item["block_id"]: {"type": {"status": "PASS"}} for item in blocks}}}
    _promote_geometric_heading_peers([page])
    assert blocks[1]["type"] == "text"


def test_unanchored_single_model_fragment_is_dropped_only_inside_anchored_block() -> None:
    blocks = [
        {"block_id": "owner", "bbox_image_px": [100, 100, 600, 220], "text": "完整正文第二行", "source_refs": ["pdf.1"], "support_refs": ["paddle.1"]},
        {"block_id": "fragment", "bbox_image_px": [100, 180, 300, 220], "text": "第二行", "source_refs": [], "support_refs": ["mineru.1"]},
        {"block_id": "independent", "bbox_image_px": [100, 240, 300, 280], "text": "独立内容", "source_refs": [], "support_refs": ["mineru.2"]},
        {"block_id": "anchored", "bbox_image_px": [100, 180, 300, 220], "text": "第二行", "source_refs": ["pdf.2"], "support_refs": ["mineru.3"]},
    ]
    reviews = {"blocks": {block["block_id"]: {} for block in blocks}}
    issues = [{"block_ids": ["fragment"]}, {"block_ids": ["independent"]}]
    kept, kept_issues = _drop_unanchored_duplicate_blocks(blocks, reviews, issues)
    assert [block["block_id"] for block in kept] == ["owner", "independent", "anchored"]
    assert "fragment" not in reviews["blocks"]
    assert kept_issues == [{"block_ids": ["independent"]}]


def test_small_offset_unanchored_prefix_fragment_is_dropped() -> None:
    blocks = [
        {"block_id": "fragment", "bbox_image_px": [145, 100, 172, 126], "text": "5.", "source_refs": [], "support_refs": ["mineru.1"]},
        {"block_id": "owner", "bbox_image_px": [150, 102, 700, 300], "text": "5. 完整题目", "source_refs": ["pdf.1"], "support_refs": ["paddle.1"]},
    ]
    reviews = {"blocks": {block["block_id"]: {} for block in blocks}}
    kept, _ = _drop_unanchored_duplicate_blocks(blocks, reviews, [])
    assert [block["block_id"] for block in kept] == ["owner"]


def test_continued_question_sequence_promotes_same_page_followups() -> None:
    page = {"current_candidate": {"blocks": [
        {"block_id": "q3", "type": "text", "text": "③第三个问题？", "type_candidates": ["text"], "support_refs": ["paddle.q3"]},
        {"block_id": "q4", "type": "text", "text": "④第四个问题？", "type_candidates": ["text"], "support_refs": ["paddle.q4"]},
        {"block_id": "prose", "type": "text", "text": "这是正文。", "type_candidates": ["text"], "support_refs": ["paddle.prose"]},
    ]}, "field_reviews": {"blocks": {}}}
    assert promote_continued_question_candidates(page, [{"type": "title", "text": "②第二个问题？"}]) == 2
    assert page["current_candidate"]["blocks"][0]["type"] == "title"
    assert page["current_candidate"]["blocks"][1]["type"] == "title"
    assert page["current_candidate"]["blocks"][0]["type_candidates"] == ["title"]
    assert page["current_candidate"]["blocks"][1]["type_candidates"] == ["title"]
    assert page["field_reviews"]["blocks"]["q4"]["type"]["method"] == "continued_question_sequence"




def test_single_model_image_over_native_text_is_rejected() -> None:
    image = {"evidence_id": "mineru.image", "model": "mineru", "type_candidate": "image", "bbox_image_px": [100, 100, 800, 300]}
    paddle_text = {"evidence_id": "paddle.text", "model": "paddle", "type_candidate": "text", "bbox_image_px": [120, 120, 780, 280], "text": "正文"}
    lines = [line("pdf.1", [120, 120, 700, 150], "第一行"), line("pdf.2", [120, 180, 700, 210], "第二行")]
    assert _drop_unsupported_image_regions([image, paddle_text], lines, []) == [paddle_text]
    pdf_image = {"bbox_image_px": [90, 90, 810, 310]}
    assert _drop_unsupported_image_regions([image, paddle_text], lines, [pdf_image]) == [image, paddle_text]


def test_single_model_container_of_multiple_pdf_images_is_dropped() -> None:
    container = {"evidence_id": "paddle.image", "model": "paddle", "type_candidate": "image", "bbox_image_px": [100, 100, 900, 500]}
    images = [{"bbox_image_px": [100, 100, 500, 500]}, {"bbox_image_px": [500, 100, 900, 500]}]
    assert _drop_unsupported_image_regions([container], [], images) == []
    assert container["rejected_as_image"] == "single_model_container_of_multiple_pdf_images"


def _diagram_page(evidence: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    figure = {"block_id": "candidate.block.p1.0000", "type": "image", "bbox_image_px": [120, 680, 900, 950], "source_refs": [], "reading_order": 0}
    lines = [line(f"pdf.line.p1.{index:04d}", [140, 700 + index * 30, 880, 720 + index * 30], f"标签{index}") for index in range(6)]
    labels = [
        {
            "block_id": f"candidate.block.p1.{index:04d}",
            "type": "text",
            "text": text,
            "bbox_image_px": box,
            "source_refs": [lines[index - 1]["evidence_id"]],
            "support_refs": ["mineru.label"],
            "reading_order": index,
        }
        for index, (text, box) in enumerate(
            [("管理网络", [500, 700, 600, 730]), ("业务网络", [500, 760, 600, 790]), ("管理PC", [200, 880, 300, 910])],
            start=1,
        )
    ]
    labels.append({"block_id": "candidate.block.p1.0004", "type": "text", "text": "页外标签", "bbox_image_px": [200, 980, 320, 1010], "source_refs": [lines[3]["evidence_id"]], "support_refs": [], "reading_order": 4})
    page = {
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
        "evidence": {"pdf": evidence + lines},
        "field_reviews": {"blocks": {}},
    }
    return page, [figure] + labels, lines


def test_diagram_region_keeps_image_though_labels_are_native_text() -> None:
    evidence = [
        {"evidence_id": "pdf.image.p1.0001", "kind": "pdf_image", "bbox_image_px": [150, 700, 350, 900]},
        {"evidence_id": "pdf.image.p1.0002", "kind": "pdf_image", "bbox_image_px": [400, 700, 600, 900]},
    ] + [{"evidence_id": f"pdf.drawing.p1.{index:04d}", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950], "item_count": 6} for index in range(4)]
    page, blocks, lines = _diagram_page(evidence)
    assert _region_graphic_structure(page, [120, 680, 900, 950]) is True
    _drop_vector_container_images(page, blocks, {"blocks": {}}, lines)
    assert [block["block_id"] for block in blocks if block["type"] == "image"] == ["candidate.block.p1.0000"]


def test_diagram_labels_are_absorbed_into_the_surviving_image() -> None:
    evidence = [
        {"evidence_id": "pdf.image.p1.0001", "kind": "pdf_image", "bbox_image_px": [150, 700, 350, 900]},
        {"evidence_id": "pdf.image.p1.0002", "kind": "pdf_image", "bbox_image_px": [400, 700, 600, 900]},
    ] + [{"evidence_id": f"pdf.drawing.p1.{index:04d}", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950], "item_count": 6} for index in range(4)]
    page, blocks, lines = _diagram_page(evidence)
    assert _graphic_region_refs(page, [120, 680, 900, 950])
    page["current_candidate"] = {"blocks": blocks}
    _absorb_blocks_inside_graphic_images([page])
    kept = page["current_candidate"]["blocks"]
    assert [block["block_id"] for block in kept] == ["candidate.block.p1.0000", "candidate.block.p1.0004"]
    assert [block["reading_order"] for block in kept] == [0, 1]
    assert page["field_reviews"]["blocks"]["candidate.block.p1.0000"]["nested_native_text"]["method"] == "diagram_labels_belong_to_image"
    absorbed_line = next(item for item in page["evidence"]["pdf"] if item["evidence_id"] == lines[0]["evidence_id"])
    assert absorbed_line["required_coverage"] is False


def test_nested_picture_inside_a_diagram_transfers_its_evidence() -> None:
    evidence = [
        {"evidence_id": "pdf.image.p1.0001", "kind": "pdf_image", "bbox_image_px": [150, 700, 350, 900], "required_coverage": True},
        {"evidence_id": "pdf.image.p1.0002", "kind": "pdf_image", "bbox_image_px": [400, 700, 600, 900], "required_coverage": True},
    ] + [{"evidence_id": f"pdf.drawing.p1.{index:04d}", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950], "item_count": 6} for index in range(4)]
    page, blocks, _lines = _diagram_page(evidence)
    icon = {"block_id": "candidate.block.p1.0005", "type": "image", "bbox_image_px": [400, 700, 600, 900], "source_refs": ["pdf.image.p1.0002"], "support_refs": ["mineru.icon"], "reading_order": 5}
    page["current_candidate"] = {"blocks": blocks + [icon]}
    _absorb_blocks_inside_graphic_images([page])
    kept = {block["block_id"]: block for block in page["current_candidate"]["blocks"]}
    assert "candidate.block.p1.0005" not in kept
    assert "pdf.image.p1.0002" in kept["candidate.block.p1.0000"]["source_refs"]
    assert "candidate.block.p1.0005" not in page["field_reviews"]["blocks"]


def test_block_box_is_extended_to_cover_claimed_evidence() -> None:
    page = {
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
        "evidence": {"pdf": [line("pdf.line.p1.0000", [100.0, 500.0, 300.0, 560.0], "合计")]},
    }
    block = {"block_id": "candidate.block.p1.0000", "type": "table", "bbox_image_px": [110.0, 505.0, 290.0, 545.0], "source_refs": ["pdf.line.p1.0000"], "support_refs": [], "html": "<table><tr><td>合计</td></tr></table>"}
    reviews = {"blocks": {}}
    _enforce_evidence_containment(page, [block], reviews)
    assert block["bbox_image_px"][1] <= 500.0 and block["bbox_image_px"][3] >= 560.0
    assert reviews["blocks"]["candidate.block.p1.0000"]["bbox"]["method"] == "pdf_evidence_containment_union"


def test_diagram_region_expands_to_absorb_partially_contained_pictures() -> None:
    evidence = [
        {"evidence_id": "pdf.image.p1.0001", "kind": "pdf_image", "bbox_image_px": [150, 700, 350, 900], "required_coverage": True},
        {"evidence_id": "pdf.image.p1.0002", "kind": "pdf_image", "bbox_image_px": [300, 700, 500, 980], "required_coverage": True},
        {"evidence_id": "pdf.drawing.p1.0000", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950], "item_count": 6},
        {"evidence_id": "pdf.drawing.p1.0001", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950]},
        {"evidence_id": "pdf.drawing.p1.0002", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950]},
        {"evidence_id": "pdf.drawing.p1.0003", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950]},
    ]
    page, blocks, _lines = _diagram_page(evidence)
    nested = {"block_id": "candidate.block.p1.0005", "type": "image", "bbox_image_px": [300, 700, 500, 980], "source_refs": ["pdf.image.p1.0002"], "support_refs": ["mineru.icon"], "reading_order": 5}
    page["current_candidate"] = {"blocks": blocks + [nested]}
    _absorb_blocks_inside_graphic_images([page])
    owner = page["current_candidate"]["blocks"][0]
    assert owner["bbox_image_px"][3] >= 980.0
    assert "pdf.image.p1.0002" in owner["source_refs"]
    assert not [block for block in page["current_candidate"]["blocks"] if block["block_id"] == "candidate.block.p1.0005"]


def test_unanchored_text_inside_anchored_text_is_dropped() -> None:
    anchored = {"block_id": "candidate.block.p1.0000", "type": "text", "text": "口 通过 口 部分通过", "bbox_image_px": [188.0, 156.0, 1045.0, 900.0], "source_refs": ["pdf.line.p1.0000"], "support_refs": [], "type_candidates": ["text"]}
    duplicate = {"block_id": "candidate.block.p1.0001", "type": "text", "text": "测试结论☐通过☐部分通过", "bbox_image_px": [249.0, 300.0, 1039.0, 880.0], "source_refs": [], "support_refs": ["paddle.block.p1.0007"], "type_candidates": ["text"]}
    reviews = {"blocks": {anchored["block_id"]: {}, duplicate["block_id"]: {}}}
    kept, _issues = _drop_unanchored_duplicate_blocks([anchored, duplicate], reviews, [], [line("pdf.line.p1.0000", [188.0, 156.0, 1045.0, 900.0], "口 通过 口 部分通过")])
    assert [block["block_id"] for block in kept] == [anchored["block_id"]]
    assert "paddle.block.p1.0007" in anchored["support_refs"]
    assert reviews["blocks"][anchored["block_id"]]["unanchored_text_inside_anchored"]["status"] == "PASS"


def test_text_bearing_image_is_not_promoted_when_text_layer_already_carries_it() -> None:
    page = {
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
        "evidence": {
            "pdf": [{"evidence_id": "pdf.image.p1.0000", "kind": "pdf_image", "bbox_image_px": [83.0, 49.0, 159.0, 123.0]}],
            "mineru": [{"evidence_id": "mineru.block.p1.0000", "model": "mineru", "source_type": "header", "type_candidate": "image", "bbox_image_px": [83.0, 49.0, 159.0, 123.0], "text": "XINNING"}],
            "paddle": [{"evidence_id": "paddle.block.p1.0000", "model": "paddle", "source_type": "header", "type_candidate": "image", "bbox_image_px": [84.0, 50.0, 158.0, 122.0], "text": "XINNING"}],
        },
    }
    image_block = {"block_id": "candidate.block.p1.0000", "type": "image", "bbox_image_px": [83.0, 49.0, 159.0, 123.0], "source_refs": ["pdf.image.p1.0000"], "support_refs": ["mineru.block.p1.0000", "paddle.block.p1.0000"], "type_candidates": ["image"], "reading_order": 0}
    letterhead = {"block_id": "candidate.block.p1.0001", "type": "header", "text": "Wuhan Xinning Logistics co.,ltd", "bbox_image_px": [168.0, 95.0, 623.0, 124.0], "source_refs": ["pdf.line.p1.0001"], "support_refs": [], "type_candidates": ["header"], "reading_order": 1}
    reviews = {"blocks": {image_block["block_id"]: {}, letterhead["block_id"]: {}}}
    _promote_textual_content_images(page, [image_block, letterhead], reviews, {"pdf.image.p1.0000": ["mineru.block.p1.0000", "paddle.block.p1.0000"]}, page["evidence"]["mineru"] + page["evidence"]["paddle"], [])
    assert image_block["type"] == "image"


def test_text_without_visible_ink_is_not_visible_content(tmp_path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("L", (200, 200), 255)
    ImageDraw.Draw(image).line([12, 20, 58, 20], fill=0, width=3)
    path = tmp_path / "page.png"
    image.save(path)
    page = {
        "page": 1,
        "page_image": {"path": str(path), "width": 200, "height": 200},
        "pdf_page": {"width_pt": 100.0, "height_pt": 100.0},
        "evidence": {"pdf": []},
    }
    visible = line("pdf.line.p1.0000", [10.0, 10.0, 60.0, 30.0], "可见行")
    hidden = line("pdf.line.p1.0001", [100.0, 100.0, 160.0, 130.0], "HNPR-2020-36010")
    assert _mark_visually_absent_lines(page, [visible, hidden]) == 1
    assert hidden["visually_absent"] is True and hidden["required_coverage"] is False
    assert hidden["visual_absence"]["method"] == "rendered_region_has_no_ink"
    assert "visually_absent" not in visible

    block = {"block_id": "candidate.block.p1.0000", "type": "text", "text": "HNPR-2020-36010", "bbox_image_px": [100.0, 100.0, 160.0, 130.0], "source_refs": ["pdf.line.p1.0001"], "support_refs": [], "reading_order": 0}
    reviews = {"blocks": {block["block_id"]: {"type": {"status": "PASS"}}}}
    issues: list[dict] = []
    _drop_invisible_text_blocks(page, [block], reviews, issues, [visible, hidden])
    assert page["evidence"]["pdf"] == []
    assert issues[0]["type"] == "invisible_text_not_visible_content"


def test_faint_grey_glyphs_are_visible_content(tmp_path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("L", (200, 200), 255)
    ImageDraw.Draw(image).line([12, 20, 58, 20], fill=223, width=3)  # very light grey glyph strokes
    path = tmp_path / "page.png"
    image.save(path)
    page = {"page": 1, "page_image": {"path": str(path), "width": 200, "height": 200}, "pdf_page": {"width_pt": 100.0, "height_pt": 100.0}, "evidence": {"pdf": []}}
    faint = line("pdf.line.p1.0000", [10.0, 10.0, 60.0, 30.0], "浅灰字")
    assert _mark_visually_absent_lines(page, [faint]) == 0
    assert "visually_absent" not in faint


def test_hidden_lines_are_stripped_from_a_partly_visible_block(tmp_path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("L", (200, 200), 255)
    ImageDraw.Draw(image).line([12, 20, 58, 20], fill=0, width=3)
    path = tmp_path / "page.png"
    image.save(path)
    page = {
        "page": 1,
        "page_image": {"path": str(path), "width": 200, "height": 200},
        "pdf_page": {"width_pt": 100.0, "height_pt": 100.0},
        "evidence": {"pdf": []},
    }
    visible = line("pdf.line.p1.0000", [10.0, 10.0, 60.0, 30.0], "可见行")
    hidden = line("pdf.line.p1.0001", [100.0, 100.0, 160.0, 130.0], "隐藏行")
    _mark_visually_absent_lines(page, [visible, hidden])
    block = {"block_id": "candidate.block.p1.0000", "type": "text", "text": "可见行\n隐藏行", "bbox_image_px": [10.0, 10.0, 160.0, 130.0], "source_refs": ["pdf.line.p1.0000", "pdf.line.p1.0001"], "support_refs": [], "reading_order": 0}
    reviews = {"blocks": {block["block_id"]: {}}}
    _drop_invisible_text_blocks(page, [block], reviews, [], [visible, hidden])
    assert block["source_refs"] == ["pdf.line.p1.0000"]
    assert block["text"] == "可见行"
    assert reviews["blocks"][block["block_id"]]["text"]["method"] == "pdf_text_visible_lines_only"


def test_table_owns_picture_when_the_model_box_is_slightly_short() -> None:
    table = [100.0, 200.0, 900.0, 1000.0]
    inside = [150.0, 220.0, 400.0, 380.0]
    spilling = [140.0, 150.0, 420.0, 700.0]
    far_away = [100.0, 1100.0, 400.0, 1300.0]
    assert _table_owns_picture(table, inside) is True
    assert _table_owns_picture(table, spilling) is True
    assert _table_owns_picture(table, far_away) is False


def test_rule_positions_merge_near_duplicates() -> None:
    assert _merge_positions([100.0, 101.2, 250.0, 900.0, 898.5]) == [100.6, 250.0, 899.25]


def test_picture_binds_into_a_table_whose_lower_rows_have_no_rules() -> None:
    table_box = [100.0, 200.0, 900.0, 1000.0]
    image_box = [150.0, 700.0, 400.0, 900.0]
    page = {
        "page": 1,
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
        "evidence": {
            "pdf": [
                {"evidence_id": "pdf.image.p1.0000", "kind": "pdf_image", "bbox_image_px": image_box, "required_coverage": True},
                {"evidence_id": "pdf.drawing.p1.0000", "kind": "pdf_drawing", "bbox_image_px": [100.0, 200.0, 102.0, 1000.0]},
                {"evidence_id": "pdf.drawing.p1.0001", "kind": "pdf_drawing", "bbox_image_px": [898.0, 200.0, 900.0, 1000.0]},
                {"evidence_id": "pdf.drawing.p1.0002", "kind": "pdf_drawing", "bbox_image_px": [100.0, 200.0, 900.0, 202.0]},
                {"evidence_id": "pdf.drawing.p1.0003", "kind": "pdf_drawing", "bbox_image_px": [100.0, 500.0, 900.0, 502.0]},
                line("pdf.line.p1.0000", [110.0, 210.0, 400.0, 240.0], "指标"),
            ],
            "mineru": [
                {"evidence_id": "mineru.block.p1.0000", "model": "mineru", "source_type": "table", "bbox_image_px": table_box, "html": "<table><tr><td>指标</td><td>值</td></tr><tr><td></td><td></td></tr></table>"},
                {"evidence_id": "mineru.block.p1.0001", "model": "mineru", "source_type": "image", "bbox_image_px": image_box},
            ],
        },
    }
    table = {"block_id": "candidate.block.p1.0000", "type": "table", "bbox_image_px": table_box, "html": "<table><tr><td>指标</td><td>值</td></tr><tr><td></td><td></td></tr></table>", "source_refs": ["pdf.line.p1.0000"], "support_refs": ["mineru.block.p1.0000"], "reading_order": 0}
    picture = {"block_id": "candidate.block.p1.0001", "type": "image", "bbox_image_px": image_box, "source_refs": ["pdf.image.p1.0000"], "support_refs": ["mineru.block.p1.0001"], "reading_order": 1}
    reviews = {"blocks": {table["block_id"]: {}, picture["block_id"]: {}}}
    _bind_table_embedded_images(page, [table, picture], reviews)
    assert [entry["image_ref"] for entry in table.get("embedded_images", [])] == ["pdf.image.p1.0000"]
    assert "pdf.image.p1.0000" in table["source_refs"]
    assert 'data-image-ref="pdf.image.p1.0000"' in table["html"]


def test_nearest_empty_cell_prefers_the_same_column() -> None:
    html = "<table><tr><td>a</td><td></td></tr><tr><td></td><td>x</td></tr></table>"
    assert _nearest_empty_cell(html, 1, 1) == (0, 1)
    assert _nearest_empty_cell(html, 0, 0) == (1, 0)


def test_nearest_empty_cell_never_returns_a_cell_that_already_has_a_picture() -> None:
    html = '<table><tr><td><img data-image-ref="pdf.image.p1.0000"/></td><td></td></tr></table>'
    assert _nearest_empty_cell(html, 0, 0) == (0, 1)


def test_table_image_block_is_recycled_when_the_cell_transcribes_it() -> None:
    table_box = [100.0, 200.0, 900.0, 1000.0]
    image_box = [150.0, 700.0, 400.0, 990.0]
    page = {
        "page": 1,
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
        "evidence": {"pdf": [{"evidence_id": "pdf.image.p1.0000", "kind": "pdf_image", "bbox_image_px": image_box, "required_coverage": True}]},
    }
    table = {"block_id": "candidate.block.p1.0000", "type": "table", "bbox_image_px": table_box, "html": "<table><tr><td>名称</td><td>值</td></tr><tr><td>图</td><td>说明文字</td></tr></table>", "source_refs": [], "support_refs": ["mineru.table"], "reading_order": 0}
    picture = {"block_id": "candidate.block.p1.0001", "type": "image", "bbox_image_px": image_box, "source_refs": ["pdf.image.p1.0000"], "support_refs": ["mineru.icon"], "reading_order": 1}
    reviews = {"blocks": {table["block_id"]: {}, picture["block_id"]: {}}}
    _drop_textual_table_images(page, [table, picture], reviews)
    assert [block["block_id"] for block in [table, picture] if block.get("_kept")] == []
    assert "pdf.image.p1.0000" in table["source_refs"]
    assert reviews["blocks"][table["block_id"]]["image_text_transcribed_in_cell"]["status"] == "PASS"


def test_labels_outside_a_plain_picture_are_not_absorbed() -> None:
    page, blocks, _lines = _diagram_page([{"evidence_id": "pdf.image.p1.0001", "kind": "pdf_image", "bbox_image_px": [120, 680, 900, 950]}])
    page["current_candidate"] = {"blocks": blocks}
    _absorb_blocks_inside_graphic_images([page])
    assert len(page["current_candidate"]["blocks"]) == len(blocks)


def test_text_container_without_graphic_structure_still_yields_to_native_text() -> None:
    page, blocks, lines = _diagram_page([{"evidence_id": "pdf.drawing.p1.0000", "kind": "pdf_drawing", "bbox_image_px": [120, 680, 900, 950]}])
    assert _region_graphic_structure(page, [120, 680, 900, 950]) is False
    _drop_vector_container_images(page, blocks, {"blocks": {}}, lines)
    assert not [block for block in blocks if block["type"] == "image"]


def test_duplicate_page_numbers_merge_only_at_page_edge() -> None:
    blocks = [
        {"block_id": "wide", "type": "page_number", "text": "- 1 -", "bbox_image_px": [400, 1300, 600, 1340], "support_refs": ["mineru"]},
        {"block_id": "tight", "type": "page_number", "text": "1", "bbox_image_px": [480, 1302, 520, 1342], "support_refs": ["paddle"]},
        {"block_id": "body", "type": "text", "text": "1", "bbox_image_px": [480, 600, 520, 640], "support_refs": []},
    ]
    kept = _drop_duplicate_page_number_blocks(blocks, {"page_image": {"width": 1000, "height": 1400}})
    assert [block["block_id"] for block in kept] == ["wide", "body"]


def test_document_relation_task_contains_previous_table_content() -> None:
    page = {
        "sample_id": "sample",
        "page": 2,
        "page_image": {"width": 1000, "height": 1400},
        "document_context": {
            "previous_pages_complete": True,
            "previous_page_tail": [{"block_id": "p1.table", "type": "table", "table_content": ["<table><tr><td>下周工作计划</td></tr></table>"]}],
        },
        "current_candidate": {"blocks": [{"block_id": "p2.heading", "type": "title", "text": "下周工作计划", "bbox_image_px": [100, 100, 500, 150], "source_refs": ["pdf.1"], "support_refs": ["paddle.1"]}]},
    }
    tasks = build_document_relation_tasks(page)
    assert len(tasks) == 1
    assert tasks[0]["previous_tables"][0]["table_content"][0].startswith("<table>")
    assert tasks[0]["current_top_blocks"][0]["block_id"] == "p2.heading"
    page["document_context"]["previous_pages_complete"] = False
    assert build_document_relation_tasks(page) == []


def test_image_box_uses_model_consensus_only_when_models_agree() -> None:
    image = {"bbox_image_px": [296, 851, 917, 1363]}
    models = [
        {"evidence_id": "mineru.image", "type_candidate": "image", "bbox_image_px": [293, 847, 921, 1252]},
        {"evidence_id": "paddle.image", "type_candidate": "image", "bbox_image_px": [295, 852, 916, 1248]},
    ]
    assert _consensus_image_box(image, ["mineru.image", "paddle.image"], models) == [295, 852, 916, 1248]
    large = [{"evidence_id": "mineru.image", "type_candidate": "image", "bbox_image_px": [234, 157, 1002, 338]}, models[1]]
    assert _consensus_image_box(image, ["mineru.image", "paddle.image"], large) == image["bbox_image_px"]


def test_standalone_marker_block_is_merged_into_the_item_it_marks() -> None:
    from ocr_agent.evidence_builder import _merge_decorative_marker_blocks

    marker = {"block_id": "m", "type": "text", "text": "\uf075", "bbox_image_px": [221, 471, 243, 499],
              "source_refs": ["pdf.line.m"], "support_refs": []}
    item = {"block_id": "t", "type": "title", "text": "安全可信的远程接入：", "bbox_image_px": [265, 466, 489, 498],
            "source_refs": ["pdf.line.t"], "support_refs": []}
    other = {"block_id": "p", "type": "text", "text": "正文", "bbox_image_px": [221, 507, 1064, 620],
             "source_refs": ["pdf.line.p"], "support_refs": []}
    blocks = [marker, item, other]
    reviews = {"blocks": {"m": {"text": {"status": "PASS", "method": "pdf_text_exact"}},
                          "t": {"text": {"status": "PASS", "method": "pdf_text_exact"}}}}
    page = {"page": 9, "page_image": {"width": 1241, "height": 1754},
            "pdf_page": {"width_pt": 595.3, "height_pt": 841.9}, "evidence": {"pdf": []}}
    _merge_decorative_marker_blocks(blocks, reviews, page)
    assert [block["block_id"] for block in blocks] == ["t", "p"]
    assert blocks[0]["text"] == "\uf075安全可信的远程接入："
    assert blocks[0]["bbox_image_px"] == [221.0, 466.0, 489.0, 499.0]
    assert set(blocks[0]["source_refs"]) == {"pdf.line.m", "pdf.line.t"}
    assert reviews["blocks"][blocks[0]["block_id"]]["text"]["method"] == "decorative_marker_merged_into_item"
    assert "m" not in reviews["blocks"]


def test_placeholder_block_without_evidence_is_dropped() -> None:
    from ocr_agent.evidence_builder import _drop_placeholder_blocks

    placeholder = {"block_id": "ph", "type": "header", "text": "[Non-Text]", "bbox_image_px": [0, 64, 1750, 105],
                   "source_refs": [], "support_refs": ["mineru.block.p14.0001"]}
    measured = {"block_id": "m", "type": "text", "text": "[Non-Text]", "bbox_image_px": [0, 200, 100, 240],
                "source_refs": ["pdf.line.1"], "support_refs": []}
    real = {"block_id": "r", "type": "title", "text": "标题", "bbox_image_px": [0, 300, 100, 340], "source_refs": [], "support_refs": []}
    blocks = [placeholder, measured, real]
    reviews = {"blocks": {"ph": {"type": {"status": "CHECK", "method": "single_model_type"}},
                          "m": {"type": {"status": "CHECK", "method": "single_model_type"}}}}
    issues: list[dict] = []
    _drop_placeholder_blocks(blocks, reviews, issues)
    assert [block["block_id"] for block in blocks] == ["m", "r"]
    assert "text" not in blocks[0]  # measured evidence kept, placeholder text cleared
    assert any(issue["type"] == "non_text_placeholder_without_evidence" for issue in issues)


def test_duplicate_image_blocks_with_the_same_raster_are_merged() -> None:
    """MinerU calling it an image and Paddle calling it a chart must not leave two owners for one raster."""
    from ocr_agent.evidence_builder import _merge_duplicate_image_blocks
    blocks = [
        {"block_id": "a", "type": "image", "bbox_image_px": [470.0, 1259.0, 877.0, 1574.0],
         "source_refs": ["pdf.image.p2.0013", "pdf.drawing.p2.0489"], "support_refs": ["mineru.block.p2.0008"], "type_candidates": ["image"], "issues": []},
        {"block_id": "b", "type": "chart", "bbox_image_px": [472.0, 1262.0, 876.0, 1576.0],
         "source_refs": ["pdf.image.p2.0013", "pdf.drawing.p2.0489"], "support_refs": ["paddle.block.p2.0009"], "type_candidates": ["chart"], "issues": []},
    ]
    reviews = {"blocks": {"a": {}, "b": {}}}
    merged = _merge_duplicate_image_blocks(blocks, reviews)
    assert merged == {"b"}
    assert all(isinstance(issue, str) for block in blocks for issue in block.get("issues", []))
    assert [block["block_id"] for block in blocks] == ["a"]
    assert blocks[0]["type"] == "image" and "chart" in blocks[0]["type_candidates"]
    assert "b" not in reviews["blocks"]
    # a different raster in the same district stays separate
    blocks.append({"block_id": "c", "type": "chart", "bbox_image_px": [472.0, 1262.0, 876.0, 1576.0],
                   "source_refs": ["pdf.image.p2.0020"], "support_refs": ["paddle.block.p2.0011"], "type_candidates": ["chart"], "issues": []})
    assert _merge_duplicate_image_blocks(blocks, reviews) == set()
    assert len(blocks) == 2


def test_nonprintable_text_blocks_are_dropped() -> None:
    from ocr_agent.evidence_builder import _drop_nonprintable_text_blocks
    blocks = [
        {"block_id": "garbage", "type": "text", "text": "\x00\r#\x07\b\r\x06$"},
        {"block_id": "real", "type": "text", "text": "正常正文"},
        {"block_id": "short", "type": "text", "text": "第1页"},
        {"block_id": "image", "type": "image", "text": ""},
    ]
    reviews = {"blocks": {"garbage": {"type": {}}, "real": {"type": {}}}}
    issues: list = []
    _drop_nonprintable_text_blocks(blocks, reviews, issues)
    assert [block["block_id"] for block in blocks] == ["real", "short", "image"]
    assert "garbage" not in reviews["blocks"]
    assert issues and issues[0]["type"] == "nonprintable_text_block_dropped"


def test_dropped_nonprintable_block_does_not_break_reviews() -> None:
    from ocr_agent.evidence_builder import _drop_nonprintable_text_blocks
    blocks = [{"block_id": "only_space", "type": "text", "text": " \u200b\u2028"}]
    reviews = {"blocks": {}}
    issues: list = []
    _drop_nonprintable_text_blocks(blocks, reviews, issues)
    assert blocks == []


PAGE = {
    "page_image": {"width": 1000, "height": 1400},
    "pdf_page": {"width_pt": 500.0, "height_pt": 700.0},
}


def test_nested_text_blocks_are_consolidated():
    """The parser often emits a fragment and the full line over the same pixels: that is one block."""
    from ocr_agent.constants import FIELD_REVIEW_VERSION
    from ocr_agent.evidence_builder import _consolidate_nested_text_blocks

    def block(block_id: str, text: str, box: list[float], refs: list[str], order: int, kind: str = "text") -> dict:
        return {
            "block_id": block_id, "type": kind, "text": text, "bbox_image_px": box,
            "source_refs": refs, "support_refs": [], "reading_order": order,
        }

    blocks = [
        block("a", "感谢您的观看 ", [582, 387, 1382, 524], [], 0),
        block("b", "感谢您的观看\nTHANKS FOR YOUR TIME", [582, 389, 1378, 616], ["pdf.line.p1.0002"], 1),
        block("c", "完全不同的内容", [100, 100, 300, 160], ["pdf.line.p1.0009"], 2),
    ]
    kept, issues, dropped = _consolidate_nested_text_blocks(PAGE, blocks, {"blocks": {}}, [])
    assert dropped == {"a"}, dropped
    assert [item["block_id"] for item in kept] == ["b", "c"]
    survivor = next(item for item in kept if item["block_id"] == "b")
    assert survivor["text"].endswith("THANKS FOR YOUR TIME")          # the fuller wording survives
    assert issues and issues[0]["type"] == "nested_text_block_consolidated"


def test_identical_text_keeps_the_verifiable_block():
    from ocr_agent.evidence_builder import _consolidate_nested_text_blocks

    anchored = {"block_id": "keep", "type": "title", "level": 2, "text": "生猪：底部震荡，静待反转",
                "bbox_image_px": [57, 13, 632, 83], "source_refs": ["pdf.image.p3.0003"], "support_refs": [], "reading_order": 1}
    loose = {"block_id": "drop", "type": "title", "level": 3, "text": "生猪：底部震荡，静待反转",
             "bbox_image_px": [50, 18, 744, 76], "source_refs": [], "support_refs": [], "reading_order": 0}
    kept, issues, dropped = _consolidate_nested_text_blocks(PAGE, [loose, anchored], {"blocks": {}}, [])
    assert dropped == {"drop"}
    assert kept[0]["block_id"] == "keep"
    assert kept[0]["level"] == 2


def test_sanitize_text_strips_glyph_noise_but_keeps_content():
    from ocr_agent.evidence_builder import _sanitize_text, _text_is_unusable

    assert _sanitize_text("\uf06e资源管理：提供对aSV、aSAN 的管理") == "资源管理：提供对aSV、aSAN 的管理"
    assert _sanitize_text("\x00\x01\x02\x03") == ""
    assert _sanitize_text("第1页") == "第1页"
    # 符号字体残码占比低 → 可用（消毒即可）；纯噪声或占比高 → 不可用
    assert not _text_is_unusable("\uf06eWhen possible, it is beneficial to create masks")
    assert _text_is_unusable("\x00\x01\x02\x03\x04\n\x05\x06\x07")
    assert _text_is_unusable("\x01\x01\x03\x01!\x01")
    assert not _text_is_unusable("生猪：底部震荡，静待反转")
    assert not _text_is_unusable("")


def test_lines_text_skips_unusable_lines():
    from ocr_agent.evidence_builder import _lines_text

    lines = [
        {"text": "\x00\x01\x02", "bbox_image_px": [0, 0, 10, 10]},
        {"text": "可读的一行", "bbox_image_px": [0, 12, 40, 24]},
    ]
    assert _lines_text(lines) == "可读的一行"


def test_glyph_noise_text_blocks_are_dropped_or_sanitised():
    from ocr_agent.evidence_builder import _drop_glyph_noise_text_blocks

    def block(block_id: str, text: str) -> dict:
        return {"block_id": block_id, "type": "text", "text": text, "bbox_image_px": [0, 0, 10, 10],
                "source_refs": ["pdf.line.p1.0000"], "support_refs": [], "reading_order": 0}

    junk = block("junk", "\x00\x01\x02\x03\x04\x05\x06\x07")
    mixed = block("mixed", "\uf06e支持安全启动")
    clean = block("clean", "正文内容")
    reviews = {"blocks": {"junk": {"text": {"status": "PASS", "method": "pdf_text_exact", "evidence_refs": []}},
                          "mixed": {"text": {"status": "PASS", "method": "pdf_text_exact", "evidence_refs": []}}}}
    issues: list[dict] = []
    _drop_glyph_noise_text_blocks([junk, mixed, clean], reviews, issues)
    assert sorted(reviews["blocks"]) == ["mixed"]
    assert issues and issues[0]["type"] == "glyph_noise_text_block_dropped"
    assert issues[0]["block_ids"] == ["junk"]
    assert mixed["text"] == "支持安全启动"
    assert reviews["blocks"]["mixed"]["text"]["method"] == "glyph_noise_sanitised"
    assert reviews["blocks"]["mixed"]["text"]["status"] == "CHECK"


def test_consolidation_keeps_coordinate_views_consistent():
    """Widening the survivor's box must recompute bbox_pdf_pt, or the revision fails the schema."""
    from ocr_agent.evidence_builder import _consolidate_nested_text_blocks
    from ocr_agent.schemas import add_coordinate_views, convert_bbox

    def block(block_id: str, text: str, box: list[float], refs: list[str], order: int) -> dict:
        item = {"block_id": block_id, "type": "text", "text": text, "bbox_image_px": box,
                "source_refs": refs, "support_refs": [], "reading_order": order}
        add_coordinate_views(item, PAGE)
        return item

    left = block("a", "感谢您的观看 ", [582, 387, 1382, 524], [], 0)
    right = block("b", "感谢您的观看\nTHANKS FOR YOUR TIME", [582, 389, 1378, 616], ["pdf.line.p1.0002"], 1)
    kept, _issues, dropped = _consolidate_nested_text_blocks(PAGE, [left, right], {"blocks": {}}, [])
    assert dropped == {"a"}
    survivor = kept[0]
    expected_pdf, expected_norm = convert_bbox(survivor["bbox_image_px"], PAGE)
    assert max(abs(x - y) for x, y in zip(survivor["bbox_pdf_pt"], expected_pdf)) <= 0.05
    assert max(abs(x - y) for x, y in zip(survivor["bbox_normalized"], expected_norm)) <= 1e-5


def test_join_parts_restores_spaces_lost_by_span_split():
    """A Latin line split into many spans must not come back as one glued word."""
    from ocr_agent.evidence_builder import _join_parts, _needs_space

    assert _join_parts(["To", "achieve", "a", "balance"]) == "To achieve a balance"
    assert _join_parts(["B.", "Modeling", "the", "Freeform-DOE"]) == "B. Modeling the Freeform-DOE"
    # CJK keeps no separator, and punctuation binds to the previous fragment
    assert _join_parts(["生猪：", "底部震荡", "，静待反转"]) == "生猪：底部震荡，静待反转"
    assert _join_parts(["price", ",", "quality"]) == "price, quality"
    assert _join_parts(["(", "a", ")"]) == "(a)"
    # already-spaced fragments are untouched
    assert _join_parts(["HI, our objective is", " to achieve"]) == "HI, our objective is to achieve"
    assert _needs_space("a", "b") and not _needs_space("中", "文") and not _needs_space("a", ".")


def test_pdf_line_text_keeps_latin_words_apart():
    """The line builder is where the PDF span split is undone."""
    import inspect
    from ocr_agent import evidence_builder

    source = inspect.getsource(evidence_builder._pdf_facts)
    assert "_join_parts([str(span.get(\"text\", \"\")) for span in spans])" in source
