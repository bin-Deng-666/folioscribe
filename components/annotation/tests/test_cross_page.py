from __future__ import annotations

import pytest

from helpers import make_page
from ocr_agent.cross_page import (
    annotate_cross_page_tables,
    apply_cross_page_text_decision,
    build_cross_page_text_task,
    column_boundaries,
    cross_page_text_candidate,
    finalize_cross_page_flow,
    has_closing_rule,
    is_open_tail,
    mark_cross_page_text_failure,
    starts_new_section,
)
from ocr_agent.errors import SchemaError
from ocr_agent.schemas import add_coordinate_views, validate_block


def _block(page: dict, block_id: str, text: str, box: list[float], order: int, *, kind: str = "text", ref: str | None = None) -> dict:
    block = {
        "block_id": block_id,
        "type": kind,
        "bbox_image_px": box,
        "reading_order": order,
        "source_refs": [ref] if ref else [],
        "support_refs": [],
    }
    if kind == "table":
        block["html"] = "<table><tr><td>名称</td><td>数量</td></tr></table>"
    else:
        block["text"] = text
    add_coordinate_views(block, page)
    return block


def _ready_page(blocks: list[dict]) -> dict:
    page = make_page()
    page["current_candidate"]["blocks"] = blocks
    page["field_reviews"]["blocks"] = {block["block_id"]: {} for block in blocks}
    page["document_context"] = {"previous_pages_complete": True}
    return page


def _tail(text_tail: str, box: list[float] | None = None) -> dict:
    return {
        "block_id": "candidate.block.p0.0009",
        "type": "text",
        "bbox_image_px": box or [100.0, 1300.0, 600.0, 1350.0],
        "text_tail": text_tail,
        "source_refs": ["pdf.line.p0.0009"],
    }


# --------------------------------------------------------------------------- gate


def test_open_tail_and_new_section_detection() -> None:
    assert is_open_tail("该方案在实施之后")
    assert not is_open_tail("该方案在实施之后。")
    assert not is_open_tail("The sentence is complete.")
    assert not is_open_tail("参数取值：")
    assert not is_open_tail("")
    assert starts_new_section("1. 点击确定")
    assert starts_new_section("（一）总体要求")
    assert starts_new_section("附件3. 统计表")
    assert starts_new_section("- 列表项")
    assert starts_new_section("第3章 测试环境")
    assert not starts_new_section("并且因此提高了整体效率")


def test_cross_page_text_gate_requires_edge_geometry_and_open_tail() -> None:
    head = _block(make_page(), "candidate.block.p1.0000", "并且因此提高了整体效率", [100.0, 80.0, 620.0, 130.0], 0, ref="pdf.line.p1.0002")
    page = _ready_page([head])
    assert cross_page_text_candidate(page, [_tail("该方案在实施之后")]) is not None

    # closed tail: sentence already finished -> no candidate, model not asked
    assert cross_page_text_candidate(page, [_tail("该方案在实施之后。")]) is None

    # head starts a new numbered item -> no candidate
    numbered = _block(make_page(), "candidate.block.p1.0001", "1. 点击确定按钮", [100.0, 80.0, 620.0, 130.0], 0)
    assert cross_page_text_candidate(_ready_page([numbered]), [_tail("该方案在实施之后")]) is None

    # head in a different column -> no candidate
    other_column = _block(make_page(), "candidate.block.p1.0002", "并且因此提高了整体效率", [700.0, 80.0, 990.0, 130.0], 0)
    assert cross_page_text_candidate(_ready_page([other_column]), [_tail("该方案在实施之后")]) is None

    # previous page tail not at the page bottom -> no candidate
    assert cross_page_text_candidate(page, [_tail("该方案在实施之后", [100.0, 900.0, 600.0, 950.0])]) is None

    # a table or heading ends the page -> not a text continuation
    table_tail = dict(_tail("该方案在实施之后"), type="table")
    assert cross_page_text_candidate(page, [table_tail]) is None
    title_tail = dict(_tail("该方案在实施之后"), type="title")
    assert cross_page_text_candidate(page, [title_tail]) is None


def test_cross_page_text_decision_writes_links_and_status() -> None:
    head = _block(make_page(), "candidate.block.p1.0000", "并且因此提高了整体效率", [100.0, 80.0, 620.0, 130.0], 0)
    page = _ready_page([head])
    task = build_cross_page_text_task(page, [_tail("该方案在实施之后")])
    assert task is not None

    apply_cross_page_text_decision(page, task, {"task_id": task["task_id"], "relation": "continuous", "tail_block_id": task["tail"]["block_id"], "head_block_id": head["block_id"], "reason": "sentence continues"})
    field = head["cross_page_text"]
    assert field["continues_block_id"] == task["tail"]["block_id"]
    assert field["continued_by_block_id"] is None
    assert field["status"] == "PASS"
    assert field["method"] == "model_cross_page_text_flow"
    assert page["field_reviews"]["blocks"][head["block_id"]]["cross_page_text"]["status"] == "PASS"

    apply_cross_page_text_decision(page, task, {"task_id": task["task_id"], "relation": "separate", "tail_block_id": task["tail"]["block_id"], "head_block_id": head["block_id"], "reason": "new paragraph"})
    assert head["cross_page_text"]["continues_block_id"] is None
    assert head["cross_page_text"]["status"] == "PASS"

    apply_cross_page_text_decision(page, task, {"task_id": task["task_id"], "relation": "uncertain", "tail_block_id": task["tail"]["block_id"], "head_block_id": head["block_id"], "reason": "ambiguous"})
    assert head["cross_page_text"]["status"] == "CHECK"
    assert head["cross_page_text"]["method"] == "cross_page_text_flow_unresolved"

    mark_cross_page_text_failure(page, task, "provider timeout")
    assert head["cross_page_text"]["status"] == "CHECK"
    assert head["cross_page_text"]["method"] == "cross_page_text_flow_failure"


# --------------------------------------------------------------------------- tables


def _ruled_table_page() -> tuple[dict, dict]:
    table = _block(make_page(), "candidate.block.p1.0000", "", [100.0, 50.0, 900.0, 600.0], 0, kind="table")
    page = _ready_page([table])
    page["evidence"]["pdf"] = [
        {"evidence_id": "pdf.drawing.p1.0000", "kind": "pdf_drawing", "bbox_image_px": [100.0, 50.0, 103.0, 600.0]},
        {"evidence_id": "pdf.drawing.p1.0001", "kind": "pdf_drawing", "bbox_image_px": [900.0, 50.0, 903.0, 600.0]},
        {"evidence_id": "pdf.line.p1.0000", "kind": "pdf_line", "bbox_image_px": [110.0, 60.0, 300.0, 90.0], "text": "名称"},
    ]
    previous = {
        "block_id": "candidate.block.p0.0003",
        "type": "table",
        "bbox_image_px": [100.0, 900.0, 900.0, 1390.0],
        "column_boundaries": [101.5, 901.5],
        "table_content": ["<table><tr><td>名称</td><td>数量</td></tr></table>"],
    }
    return page, previous


def test_table_continuation_rule_matches_column_structure() -> None:
    page, previous = _ruled_table_page()
    table = page["current_candidate"]["blocks"][0]
    records = annotate_cross_page_tables(page, [previous], None)
    field = table["cross_page_table"]
    assert field["continues_block_id"] == previous["block_id"]
    assert field["truncated_at_page_start"] is True
    assert field["repeated_header"] is True
    assert field["status"] == "PASS"
    assert records and records[0]["continues_block_id"] == previous["block_id"]


def test_table_continuation_requires_matching_columns_and_top_position() -> None:
    page, previous = _ruled_table_page()
    page["current_candidate"]["blocks"][0]["bbox_image_px"] = [100.0, 700.0, 900.0, 1300.0]
    annotate_cross_page_tables(page, [previous], None)
    assert "cross_page_table" not in page["current_candidate"]["blocks"][0] or page["current_candidate"]["blocks"][0]["cross_page_table"].get("continues_block_id") is None

    page, previous = _ruled_table_page()
    mismatched = dict(previous, column_boundaries=[101.5, 500.0, 901.5])
    annotate_cross_page_tables(page, [mismatched], None)
    assert page["current_candidate"]["blocks"][0].get("cross_page_table", {}).get("continues_block_id") is None


def test_table_continuation_blocked_when_previous_part_not_at_page_edge() -> None:
    page, previous = _ruled_table_page()
    closed = dict(previous)
    closed["cross_page_table"] = {"truncated_at_page_end": False, "status": "PASS"}
    annotate_cross_page_tables(page, [closed], None)
    field = page["current_candidate"]["blocks"][0].get("cross_page_table", {})
    assert field.get("continues_block_id") is None

    unknown = dict(previous)
    unknown["cross_page_table"] = {"truncated_at_page_end": None, "status": "PASS"}
    page, previous = _ruled_table_page()
    annotate_cross_page_tables(page, [unknown], None)
    assert page["current_candidate"]["blocks"][0]["cross_page_table"]["continues_block_id"] == previous["block_id"]


def test_table_model_only_continuation_is_check_without_link() -> None:
    page, previous = _ruled_table_page()
    page["current_candidate"]["blocks"][0]["bbox_image_px"] = [300.0, 700.0, 900.0, 1300.0]
    annotate_cross_page_tables(page, [previous], {"relation": "continuation", "task_id": "t"})
    field = page["current_candidate"]["blocks"][0]["cross_page_table"]
    assert field["status"] == "CHECK"
    assert field["method"] == "cross_page_table_rule_model_unconfirmed"
    assert field["continues_block_id"] is None


def test_table_rule_and_model_conflict_is_check_without_linking() -> None:
    page, previous = _ruled_table_page()
    annotate_cross_page_tables(page, [previous], {"relation": "new_section", "task_id": "t"})
    field = page["current_candidate"]["blocks"][0]["cross_page_table"]
    assert field["status"] == "CHECK"
    assert field["method"] == "cross_page_table_rule_model_conflict"
    assert field["continues_block_id"] is None
    assert field["truncated_at_page_start"] is None


def test_table_page_end_truncation_follows_page_edge_position() -> None:
    table = _block(make_page(), "candidate.block.p1.0000", "", [100.0, 700.0, 900.0, 1345.0], 0, kind="table")
    page = _ready_page([table])
    bottom_text = _block(page, "candidate.block.p1.0001", "页脚上方正文", [120.0, 1300.0, 800.0, 1350.0], 1)
    page["current_candidate"]["blocks"].append(bottom_text)
    page["field_reviews"]["blocks"][bottom_text["block_id"]] = {}
    page["evidence"]["pdf"] = [
        {"evidence_id": "pdf.drawing.p1.0000", "kind": "pdf_drawing", "bbox_image_px": [100.0, 700.0, 103.0, 1345.0]},
        {"evidence_id": "pdf.drawing.p1.0001", "kind": "pdf_drawing", "bbox_image_px": [900.0, 700.0, 903.0, 1345.0]},
    ]
    annotate_cross_page_tables(page, [], None)
    field = table["cross_page_table"]
    assert field["truncated_at_page_end"] is True
    assert field["has_closing_rule"] is False

    # a bottom rule at the cut edge must not veto truncation: the table is still at the page edge
    page["evidence"]["pdf"].append({"evidence_id": "pdf.drawing.p1.0002", "kind": "pdf_drawing", "bbox_image_px": [100.0, 1343.0, 900.0, 1347.0]})
    assert has_closing_rule(page["evidence"]["pdf"], [100.0, 700.0, 900.0, 1345.0]) is True
    annotate_cross_page_tables(page, [], None)
    assert table["cross_page_table"]["truncated_at_page_end"] is True
    assert table["cross_page_table"]["has_closing_rule"] is True

    # a table that ends well above the page content bottom is not truncated
    upper = _block(make_page(), "candidate.block.p1.0002", "", [100.0, 200.0, 900.0, 600.0], 0, kind="table")
    upper_page = _ready_page([upper, _block(make_page(), "candidate.block.p1.0003", "页尾正文", [120.0, 1300.0, 800.0, 1350.0], 1)])
    upper_page["field_reviews"]["blocks"][upper["block_id"]] = {}
    upper_page["evidence"]["pdf"] = [
        {"evidence_id": "pdf.drawing.p1.0000", "kind": "pdf_drawing", "bbox_image_px": [100.0, 200.0, 103.0, 600.0]},
        {"evidence_id": "pdf.drawing.p1.0001", "kind": "pdf_drawing", "bbox_image_px": [900.0, 200.0, 903.0, 600.0]},
    ]
    annotate_cross_page_tables(upper_page, [], None)
    assert upper["cross_page_table"]["truncated_at_page_end"] is False


def test_column_boundaries_fall_back_to_text_left_edges() -> None:
    page, _ = _ruled_table_page()
    lines = [
        {"bbox_image_px": [110.0, 60.0, 300.0, 90.0], "text": "名称"},
        {"bbox_image_px": [500.0, 60.0, 700.0, 90.0], "text": "数量"},
        {"bbox_image_px": [112.0, 120.0, 300.0, 150.0], "text": "甲"},
        {"bbox_image_px": [502.0, 120.0, 700.0, 150.0], "text": "1"},
    ]
    boundaries = column_boundaries([], lines, [100.0, 50.0, 900.0, 200.0], 1000.0)
    assert len(boundaries) == 2
    assert boundaries[0] == pytest.approx(111.0, abs=3.0)
    assert boundaries[1] == pytest.approx(501.0, abs=3.0)


# --------------------------------------------------------------------------- finalize


def test_finalize_backfills_reverse_links_and_table_groups() -> None:
    page1, page2 = make_page(), make_page()
    page2["page"] = 2
    tail = _block(page1, "candidate.block.p1.0009", "该方案在实施之后", [100.0, 1300.0, 600.0, 1350.0], 9)
    head = _block(page2, "candidate.block.p2.0000", "并且因此提高了整体效率", [100.0, 80.0, 620.0, 130.0], 0)
    table1 = _block(page1, "candidate.block.p1.0010", "", [100.0, 400.0, 900.0, 900.0], 10, kind="table")
    table2 = _block(page2, "candidate.block.p2.0001", "", [100.0, 50.0, 900.0, 600.0], 1, kind="table")
    head["cross_page_text"] = {"continues_block_id": tail["block_id"], "continued_by_block_id": None, "status": "PASS", "method": "model_cross_page_text_flow", "confidence_basis": {"tail_open": True}, "evidence_refs": []}
    table2["cross_page_table"] = {"continues_block_id": table1["block_id"], "truncated_at_page_start": True, "status": "PASS", "method": "pdf_ruled_grid_cross_page_continuation", "evidence_refs": []}
    results = {
        1: {"revision": {"blocks": [tail, table1], "field_reviews": {"blocks": {}}, "review_items": []}},
        2: {"revision": {"blocks": [head, table2], "field_reviews": {"blocks": {}}, "review_items": []}},
    }
    flow = finalize_cross_page_flow({1: page1, 2: page2}, results)
    assert tail["cross_page_text"]["continued_by_block_id"] == head["block_id"]
    assert flow["text_edges"][0]["from"]["block_id"] == tail["block_id"]
    assert flow["text_edges"][0]["to"]["block_id"] == head["block_id"]
    assert table1["cross_page_table"]["part_index"] == 1
    assert table1["cross_page_table"]["part_count"] == 2
    assert table1["cross_page_table"]["truncated_at_page_end"] is True
    assert table2["cross_page_table"]["part_index"] == 2
    assert table2["cross_page_table"]["continued_by_block_id"] is None
    assert flow["table_groups"][0]["part_count"] == 2


def test_page_end_truncation_is_not_triggered_by_a_lower_table() -> None:
    """A table below the target pushes the content bottom down: the target is not cut by the page edge."""
    page, _ = _ruled_table_page()
    lower = _block(page, "candidate.block.p1.0001", "", [100.0, 900.0, 900.0, 1380.0], 1, kind="table")
    page["current_candidate"]["blocks"].append(lower)
    page["evidence"]["pdf"] += [
        {"evidence_id": "pdf.drawing.p1.0002", "kind": "pdf_drawing", "bbox_image_px": [100.0, 900.0, 103.0, 1380.0]},
        {"evidence_id": "pdf.drawing.p1.0003", "kind": "pdf_drawing", "bbox_image_px": [900.0, 900.0, 903.0, 1380.0]},
    ]
    annotate_cross_page_tables(page, [], None)
    upper, blocked_lower = page["current_candidate"]["blocks"]
    assert upper["cross_page_table"]["truncated_at_page_end"] is False
    assert blocked_lower["cross_page_table"]["truncated_at_page_end"] is True


def test_repeated_page_header_tables_are_marked_as_furniture() -> None:
    from ocr_agent.evidence_builder import _confirm_repeated_page_header_tables

    pages = []
    for number in range(1, 5):
        page = make_page()
        page["page"] = number
        header = {
            "block_id": f"candidate.block.p{number}.0000",
            "type": "table",
            "html": f"<table><tr><td>WEIGANG</td><td>文件编号</td></tr><tr><td>第{number}页</td><td>共4页</td></tr></table>",
            "bbox_image_px": [130.0, 84.0, 1107.0, 184.0],
            "reading_order": 0,
            "source_refs": [],
            "support_refs": [],
        }
        data = {
            "block_id": f"candidate.block.p{number}.0001",
            "type": "table",
            "html": f"<table><tr><td>字段{number}</td><td>说明</td></tr></table>",
            "bbox_image_px": [199.0, 600.0, 1043.0, 900.0],
            "reading_order": 1,
            "source_refs": [],
            "support_refs": [],
        }
        page["current_candidate"]["blocks"] = [header, data]
        pages.append(page)
    _confirm_repeated_page_header_tables(pages)
    assert all(page["current_candidate"]["blocks"][0].get("repeated_page_header") for page in pages)
    assert not any(page["current_candidate"]["blocks"][1].get("repeated_page_header") for page in pages)
    assert all(page["current_candidate"]["blocks"][0]["cross_page_role_evidence"]["candidate_role"] == "repeated_page_table" for page in pages)


def test_repeated_page_header_tables_are_skipped_by_cross_page_annotation() -> None:
    page, previous = _ruled_table_page()
    page["current_candidate"]["blocks"][0]["repeated_page_header"] = True
    annotate_cross_page_tables(page, [previous], {"relation": "continuation", "task_id": "t"})
    assert "cross_page_table" not in page["current_candidate"]["blocks"][0]


def test_finalize_flags_non_adjacent_links_as_check() -> None:
    page1, page3 = make_page(), make_page()
    page3["page"] = 3
    tail = _block(page1, "candidate.block.p1.0009", "尾部", [100.0, 1300.0, 600.0, 1350.0], 0)
    head = _block(page3, "candidate.block.p3.0000", "头部", [100.0, 80.0, 620.0, 130.0], 0)
    head["cross_page_text"] = {"continues_block_id": tail["block_id"], "continued_by_block_id": None, "status": "PASS", "method": "model_cross_page_text_flow", "confidence_basis": {}, "evidence_refs": []}
    results = {
        1: {"revision": {"blocks": [tail], "field_reviews": {"blocks": {}}, "review_items": []}},
        3: {"revision": {"blocks": [head], "field_reviews": {"blocks": {}}, "review_items": []}},
    }
    flow = finalize_cross_page_flow({1: page1, 3: page3}, results)
    assert head["cross_page_text"]["status"] == "CHECK"
    assert head["cross_page_text"]["method"] == "cross_page_text_flow_link_conflict"
    assert flow["conflicts"] == [head["block_id"]]
    assert results[3]["revision"]["review_items"]


# --------------------------------------------------------------------------- schema


def test_schema_validates_cross_page_fields() -> None:
    page = make_page()
    block = page["current_candidate"]["blocks"][0]
    block["cross_page_text"] = {
        "continues_block_id": "candidate.block.p0.0001",
        "continued_by_block_id": None,
        "status": "PASS",
        "method": "model_cross_page_text_flow",
        "confidence_basis": {"tail_open": True},
        "evidence_refs": ["pdf.line.p1.0001"],
    }
    validate_block(block, page=page)

    block["cross_page_table"] = {"part_index": 1}
    with pytest.raises(SchemaError):
        validate_block(block, page=page)
    block.pop("cross_page_table")

    block["cross_page_text"] = {"unsupported": 1}
    with pytest.raises(SchemaError):
        validate_block(block, page=page)
    block["cross_page_text"] = {"status": "MAYBE"}
    with pytest.raises(SchemaError):
        validate_block(block, page=page)
    block["cross_page_text"] = {"continues_block_id": 7}
    with pytest.raises(SchemaError):
        validate_block(block, page=page)

    table = dict(block)
    table["block_id"] = "candidate.block.p1.0002"
    table["type"] = "table"
    table.pop("level", None)
    table["html"] = "<table><tr><td>a</td></tr></table>"
    table["cross_page_text"] = None
    table.pop("cross_page_text")
    table["cross_page_table"] = {
        "document_table_identity": "t.1",
        "part_index": 1,
        "part_count": 2,
        "truncated_at_page_end": True,
        "continued_by_block_id": "candidate.block.p2.0000",
        "status": "PASS",
        "method": "pdf_ruled_grid_page_end_state",
        "evidence_refs": [],
    }
    add_coordinate_views(table, page)
    validate_block(table, page=page)

    table["cross_page_table"] = {"part_index": 3, "part_count": 2}
    with pytest.raises(SchemaError):
        validate_block(table, page=page)
    table["cross_page_table"] = {"truncated_at_page_end": "yes"}
    with pytest.raises(SchemaError):
        validate_block(table, page=page)


def test_fingerprint_files_cover_result_affecting_modules() -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("run_document_module", Path(__file__).resolve().parents[1] / "scripts" / "run_document.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {path.name for path in module.FINGERPRINT_FILES}
    assert {"cross_page.py", "evidence_builder.py", "schemas.py", "constants.py", "task_agent.py"} <= names


def test_model_continuation_needs_a_verifiable_boundary_quote() -> None:
    """A model continuation is adopted only when its quoted boundary text is really in both fragments."""
    from ocr_agent.cross_page import verified_quote_continuation

    previous_tables = [{"block_id": "prev", "text": "1、组织生产人员宣导质量意识，培训发现整机划"}, {"block_id": "other", "text": "无关内容"}]
    table = {"block_id": "cur", "text": "伤，通知组长", "html": ""}
    assert verified_quote_continuation(previous_tables, table, {
        "relation": "continuation", "previous_block_id": "prev",
        "boundary_before": "培训发现整机划", "boundary_after": "伤，通知组长",
    }) == "prev"
    # a quote that is not in the fragments, a wrong fragment and a too-short quote are all rejected
    assert verified_quote_continuation(previous_tables, table, {
        "relation": "continuation", "previous_block_id": "prev",
        "boundary_before": "并不存在于上一段", "boundary_after": "伤，通知组长",
    }) is None
    assert verified_quote_continuation(previous_tables, table, {
        "relation": "continuation", "previous_block_id": "missing",
        "boundary_before": "培训发现整机划", "boundary_after": "伤，通知组长",
    }) is None
    assert verified_quote_continuation(previous_tables, table, {
        "relation": "continuation", "previous_block_id": "prev",
        "boundary_before": "1", "boundary_after": "伤",
    }) is None


def test_table_page_end_truncation_needs_to_reach_the_page_bottom() -> None:
    """A table that ends in the middle of a page is not cut by the page edge."""
    from ocr_agent.cross_page import annotate_cross_page_tables

    drawings = [{"evidence_id": "d1", "kind": "pdf_drawing", "bbox_image_px": [100, 260, 500, 262]},
                {"evidence_id": "d2", "kind": "pdf_drawing", "bbox_image_px": [100, 610, 500, 612]}]
    page = {"page": 2, "page_image": {"width": 1000, "height": 1754}, "evidence": {"pdf": drawings},
            "current_candidate": {"blocks": [
                {"block_id": "title", "type": "title", "text": "文档修订记录", "bbox_image_px": [160, 199, 388, 248], "reading_order": 0, "source_refs": []},
                {"block_id": "table", "type": "table", "text": "版本号", "bbox_image_px": [160, 260, 1077, 612], "reading_order": 1, "source_refs": []},
            ]}}
    annotate_cross_page_tables(page, [], None)
    table = next(block for block in page["current_candidate"]["blocks"] if block["block_id"] == "table")
    field = table.get("cross_page_table") or {}
    assert field.get("truncated_at_page_end") is False or field.get("truncated_at_page_end") is None
    assert field.get("method") != "pdf_ruled_grid_page_end_state" or field.get("truncated_at_page_end") is False


def test_table_reaching_the_page_bottom_is_truncated() -> None:
    from ocr_agent.cross_page import annotate_cross_page_tables

    drawings = [{"evidence_id": "d1", "kind": "pdf_drawing", "bbox_image_px": [100, 200, 500, 202]},
                {"evidence_id": "d2", "kind": "pdf_drawing", "bbox_image_px": [100, 1690, 500, 1692]}]
    page = {"page": 1, "page_image": {"width": 1000, "height": 1754}, "evidence": {"pdf": drawings},
            "current_candidate": {"blocks": [
                {"block_id": "table", "type": "table", "text": "甲", "bbox_image_px": [100, 200, 500, 1692], "reading_order": 0, "source_refs": []},
            ]}}
    annotate_cross_page_tables(page, [], None)
    table = page["current_candidate"]["blocks"][0]
    assert (table.get("cross_page_table") or {}).get("truncated_at_page_end") is True


def test_word_row_coverage_decides_the_page_state_exactly() -> None:
    """A complete Word table is not truncated, whatever the PDF geometry says."""
    from ocr_agent.cross_page import word_row_coverage_state, word_contiguous_previous

    complete = {"word_table_ref": "word.table.0000", "word_table_shape": [4, 6], "word_row_range": [0, 3]}
    assert word_row_coverage_state(complete) == (False, False)
    first_half = {"word_table_ref": "word.table.0000", "word_table_shape": [8, 6], "word_row_range": [0, 3]}
    assert word_row_coverage_state(first_half) == (True, False)
    second_half = {"word_table_ref": "word.table.0000", "word_table_shape": [8, 6], "word_row_range": [4, 7]}
    assert word_row_coverage_state(second_half) == (False, True)
    middle = {"word_table_ref": "word.table.0000", "word_table_shape": [12, 6], "word_row_range": [4, 7]}
    assert word_row_coverage_state(middle) == (True, True)
    assert word_row_coverage_state({"word_table_ref": "word.table.0000"}) is None
    previous = [{**first_half, "block_id": "prev"}]
    assert word_contiguous_previous(previous, {**second_half, "block_id": "cur"}) == "prev"
    assert word_contiguous_previous(previous, {**second_half, "block_id": "cur", "word_table_ref": "word.table.0001"}) is None
    assert word_contiguous_previous(previous, {**first_half, "block_id": "cur", "word_row_range": [6, 9]}) is None


def test_word_bound_table_uses_row_coverage_for_page_state() -> None:
    from ocr_agent.cross_page import annotate_cross_page_tables

    page = {"page": 4, "page_image": {"width": 1241, "height": 1754}, "evidence": {"pdf": []},
            "current_candidate": {"blocks": [
                {"block_id": "t", "type": "table", "text": "制度编号 DC09014", "reading_order": 0, "source_refs": [],
                 "bbox_image_px": [160, 565, 1077, 753],
                 "word_table_ref": "word.table.0000", "word_table_shape": [4, 6], "word_row_range": [0, 3]},
            ]}}
    annotate_cross_page_tables(page, [], None)
    field = page["current_candidate"]["blocks"][0]["cross_page_table"]
    assert field["truncated_at_page_end"] is False
    assert field["method"] == "word_table_row_coverage_page_state"


def test_unlinked_page_end_truncation_is_reconciled_to_undetermined() -> None:
    """Geometry alone cannot assert a truncation: without a next-page table it stays undetermined."""
    from copy import deepcopy

    from ocr_agent.cross_page import finalize_cross_page_flow, word_row_coverage_state

    def page(number: int, blocks: list[dict]) -> dict:
        return {"page": number, "page_image": {"width": 1000, "height": 1000}, "evidence": {"pdf": []},
                "current_candidate": {"blocks": deepcopy(blocks)}}

    flagged = {"block_id": "t1", "type": "table", "text": "甲", "reading_order": 0, "source_refs": [],
               "bbox_image_px": [100, 100, 500, 830],
               "cross_page_table": {"truncated_at_page_end": True, "status": "PASS", "method": "pdf_ruled_grid_page_end_state", "evidence_refs": []}}
    pages = {1: page(1, [flagged]),
             2: page(2, [{"block_id": "h", "type": "title", "text": "下一节", "reading_order": 0, "source_refs": [], "bbox_image_px": [100, 100, 500, 140]}])}
    results = {
        1: {"revision": {"blocks": deepcopy(pages[1]["current_candidate"]["blocks"]), "field_reviews": {"blocks": {"t1": {}}}, "review_items": []}},
        2: {"revision": {"blocks": deepcopy(pages[2]["current_candidate"]["blocks"]), "field_reviews": {"blocks": {"h": {}}}, "review_items": []}},
    }
    finalize_cross_page_flow(pages, results)
    field = results[1]["revision"]["blocks"][0]["cross_page_table"]
    assert field["truncated_at_page_end"] is None
    assert field["method"] == "page_end_state_unlinked"

    # ... but a next page that does start with a table keeps the truncation claim
    pages2 = {1: page(1, [deepcopy(flagged)]),
              2: page(2, [{"block_id": "t2", "type": "table", "text": "乙", "reading_order": 0, "source_refs": [], "bbox_image_px": [100, 120, 500, 400]}])}
    results2 = {
        1: {"revision": {"blocks": deepcopy(pages2[1]["current_candidate"]["blocks"]), "field_reviews": {"blocks": {"t1": {}}}, "review_items": []}},
        2: {"revision": {"blocks": deepcopy(pages2[2]["current_candidate"]["blocks"]), "field_reviews": {"blocks": {"t2": {}}}, "review_items": []}},
    }
    finalize_cross_page_flow(pages2, results2)
    assert results2[1]["revision"]["blocks"][0]["cross_page_table"]["truncated_at_page_end"] is True
    # the Word row evidence is exact and survives the reconciliation untouched
    word_table = {**deepcopy(flagged), "word_table_ref": "word.table.0000", "word_table_shape": [9, 3], "word_row_range": [0, 3]}
    pages3 = {1: page(1, [word_table]), 2: page(2, [{"block_id": "h", "type": "title", "text": "下一节", "reading_order": 0, "source_refs": [], "bbox_image_px": [100, 100, 500, 140]}])}
    results3 = {1: {"revision": {"blocks": deepcopy(pages3[1]["current_candidate"]["blocks"]), "field_reviews": {"blocks": {"t1": {}}}, "review_items": []}},
                2: {"revision": {"blocks": deepcopy(pages3[2]["current_candidate"]["blocks"]), "field_reviews": {"blocks": {"h": {}}}, "review_items": []}}}
    finalize_cross_page_flow(pages3, results3)
    assert word_row_coverage_state(results3[1]["revision"]["blocks"][0]) == (True, False)
    assert results3[1]["revision"]["blocks"][0]["cross_page_table"]["truncated_at_page_end"] is True
