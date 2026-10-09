import pytest

from helpers import make_page
from ocr_agent.decision_applier import apply_semantic_decision, validate_semantic_response
from ocr_agent.errors import OperationError
from ocr_agent.review_tasks import build_semantic_tasks


def test_semantic_type_change_removes_stale_fields() -> None:
    page = make_page()
    block = page["current_candidate"]["blocks"][0]
    block["type"] = "unknown"
    block.pop("level")
    block["html"] = "<table><tr><td>x</td></tr></table>"
    block["issues"] = ["type_conflict", "title_level_review", "unrelated_issue"]
    task = {
        "task_id": "task",
        "heading_context_complete": True,
        "items": [{
            "block_id": block["block_id"],
            "allowed_types": ["text"],
            "allowed_levels": [],
            "allowed_parent_ids": [],
            "evidence": [{"evidence_id": "mineru.block.p1.0001"}],
        }],
    }
    response = {
        "task_id": "task",
        "decisions": [{
            "block_id": block["block_id"],
            "type": "text",
            "evidence_refs": ["mineru.block.p1.0001"],
        }],
        "unresolved": [],
    }
    revision, _ = apply_semantic_decision(page, task, response, 1)
    assert revision["blocks"][0]["type"] == "text"
    assert "html" not in revision["blocks"][0]
    assert "level" not in revision["blocks"][0]
    assert revision["blocks"][0]["issues"] == ["unrelated_issue"]


def test_non_title_decision_ignores_inapplicable_unresolved_level() -> None:
    page = make_page()
    block = page["current_candidate"]["blocks"][0]
    task = {"task_id": "task", "heading_context_complete": True, "items": [{
        "block_id": block["block_id"], "allowed_types": ["text", "title"], "allowed_levels": [1, 2], "allowed_parent_ids": [],
        "evidence": [{"evidence_id": "mineru.block.p1.0001"}],
    }]}
    response = {"task_id": "task", "decisions": [{"block_id": block["block_id"], "type": "text", "evidence_refs": ["mineru.block.p1.0001"]}], "unresolved": [{"block_id": block["block_id"], "field": "level", "reason": "not applicable"}]}
    revision, _ = apply_semantic_decision(page, task, response, 1)
    assert revision["review_items"] == []
    assert revision["blocks"][0]["type"] == "text"
    assert "level" not in revision["blocks"][0]


def test_single_model_title_candidate_can_be_downgraded_to_text() -> None:
    page = make_page()
    block = page["current_candidate"]["blocks"][0]
    block["type_candidates"] = ["title"]
    page["field_reviews"]["blocks"][block["block_id"]]["type"] = {"status": "CHECK", "method": "single_model_type", "evidence_refs": block["support_refs"]}
    item = build_semantic_tasks(page)[0]["items"][0]
    assert item["allowed_types"] == ["text", "title"]
    assert item["needs"] == ["type", "level"]


def test_invalid_title_level_does_not_reject_other_task_items() -> None:
    page = make_page()
    first = page["current_candidate"]["blocks"][0]
    second = dict(first, block_id="candidate.block.p1.0002", reading_order=1)
    page["current_candidate"]["blocks"].append(second)
    page["field_reviews"]["blocks"][second["block_id"]] = dict(page["field_reviews"]["blocks"][first["block_id"]])
    task = {"task_id": "task", "heading_context_complete": True, "items": [
        {"block_id": first["block_id"], "allowed_types": ["text"], "allowed_levels": [], "allowed_parent_ids": [], "evidence": [{"evidence_id": "mineru.block.p1.0001"}]},
        {"block_id": second["block_id"], "allowed_types": ["title"], "allowed_levels": [1, 2], "allowed_parent_ids": [], "evidence": [{"evidence_id": "mineru.block.p1.0001"}]},
    ]}
    response = {"task_id": "task", "decisions": [
        {"block_id": first["block_id"], "type": "text", "evidence_refs": ["mineru.block.p1.0001"]},
        {"block_id": second["block_id"], "type": "title", "evidence_refs": ["mineru.block.p1.0001"]},
    ], "unresolved": []}
    revision, _ = apply_semantic_decision(page, task, response, 1)
    assert revision["blocks"][0]["type"] == "text"
    assert revision["review_items"][0]["target"] == second["block_id"]


def test_title_level_passes_only_with_continuous_document_context() -> None:
    page = make_page()
    page["field_reviews"]["blocks"]["candidate.block.p1.0001"]["level"]["status"] = "CHECK"
    page["document_context"]["previous_pages_complete"] = True
    task = build_semantic_tasks(page)[0]
    item = task["items"][0]
    response = {
        "task_id": task["task_id"],
        "decisions": [{
            "block_id": item["block_id"],
            "type": "title",
            "level": 1,
            "evidence_refs": [item["evidence"][0]["evidence_id"]],
        }],
        "unresolved": [],
    }
    revision, report = apply_semantic_decision(page, task, response, 1)
    assert revision["field_reviews"]["blocks"][item["block_id"]]["level"]["status"] == "PASS"
    assert report["status"] == "PASS"

    page["document_context"]["previous_pages_complete"] = False
    task = build_semantic_tasks(page)[0]
    response["task_id"] = task["task_id"]
    revision, report = apply_semantic_decision(page, task, response, 1)
    assert revision["field_reviews"]["blocks"][item["block_id"]]["level"]["status"] == "CHECK"
    assert report["status"] == "CHECK"
    assert report["error_count"] == 0


def test_semantic_response_rejects_block_ids_used_as_evidence_refs() -> None:
    page = make_page()
    page["field_reviews"]["blocks"]["candidate.block.p1.0001"]["level"]["status"] = "CHECK"
    task = build_semantic_tasks(page)[0]
    item = task["items"][0]
    response = {
        "task_id": task["task_id"],
        "decisions": [{"block_id": item["block_id"], "type": "title", "level": 1, "evidence_refs": [item["block_id"]]}],
        "unresolved": [],
    }
    with pytest.raises(OperationError, match="invalid evidence_refs"):
        validate_semantic_response(task, response)


def test_semantic_item_contains_target_centered_context() -> None:
    page = make_page()
    original = page["current_candidate"]["blocks"][0]
    second = dict(original, block_id="candidate.block.p1.0002", reading_order=1, text="Body")
    page["current_candidate"]["blocks"].append(second)
    page["field_reviews"]["blocks"][second["block_id"]] = page["field_reviews"]["blocks"][original["block_id"]]
    page["field_reviews"]["blocks"][original["block_id"]]["level"]["status"] = "CHECK"
    page["document_context"]["next_page_head"] = [{"block_id": "next", "type": "text", "text": "Expanded details", "reading_order": 0}]
    item = build_semantic_tasks(page)[0]["items"][0]
    assert item["page_position"] >= 0
    context = item["semantic_context"]
    assert context["next_page_head"][0]["text"] == "Expanded details"
    assert context["current_page_reading_context"]["target"]["block_id"] == original["block_id"]
    assert context["current_page_reading_context"]["after"][0]["block_id"] == second["block_id"]
    assert context["source_votes"] == item["evidence"]


def test_table_decision_without_html_is_repaired_or_reviewed() -> None:
    page = {
        "sample_id": "sample",
        "page": 1,
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
        "field_reviews": {"blocks": {}},
        "evidence": {
            "pdf": [{"evidence_id": "pdf.line.p1.0000", "kind": "pdf_line", "bbox_image_px": [10.0, 10.0, 200.0, 40.0], "text": "内容"}],
            "mineru": [{"evidence_id": "mineru.block.p1.0000", "model": "mineru", "source_type": "table", "bbox_image_px": [10.0, 10.0, 200.0, 40.0], "html": "<table><tr><td>内容</td></tr></table>"}],
        },
        "current_candidate": {"blocks": [{"block_id": "candidate.block.p1.0000", "type": "unknown", "text": "内容", "bbox_image_px": [10.0, 10.0, 200.0, 40.0], "reading_order": 0, "source_refs": ["pdf.line.p1.0000"], "support_refs": ["mineru.block.p1.0000"], "type_candidates": ["table", "text"], "issues": ["table_html_missing"]}]},
    }
    task = {
        "task_version": "ocr-agent-semantic-task.v1",
        "task_id": "sample.p1.semantic.1",
        "sample_id": "sample",
        "heading_context_complete": True,
        "items": [{"page": 1, "block_id": "candidate.block.p1.0000", "current_type": "unknown", "allowed_types": ["table", "text"], "allowed_levels": [], "needs": ["type"], "evidence": [{"evidence_id": "pdf.line.p1.0000"}, {"evidence_id": "mineru.block.p1.0000"}]}],
    }
    response = {"task_id": task["task_id"], "decisions": [{"block_id": "candidate.block.p1.0000", "type": "table", "evidence_refs": ["pdf.line.p1.0000"]}], "unresolved": []}
    revision, _trace = apply_semantic_decision(page, task, response, 1)
    block = revision["blocks"][0]
    assert block["type"] == "table"
    assert "<table" in block["html"].lower()
    assert revision["field_reviews"]["blocks"]["candidate.block.p1.0000"]["html"]["method"] == "semantic_table_html_from_model_evidence"


def test_table_decision_without_any_html_keeps_the_previous_type() -> None:
    page = {
        "sample_id": "sample",
        "page": 1,
        "page_image": {"width": 1000, "height": 1400},
        "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
        "field_reviews": {"blocks": {}},
        "evidence": {
            "pdf": [{"evidence_id": "pdf.line.p1.0000", "kind": "pdf_line", "bbox_image_px": [10.0, 10.0, 200.0, 40.0], "text": "内容"}],
            "mineru": [{"evidence_id": "mineru.block.p1.0000", "model": "mineru", "source_type": "table", "bbox_image_px": [10.0, 10.0, 200.0, 40.0]}],
        },
        "current_candidate": {"blocks": [{"block_id": "candidate.block.p1.0000", "type": "unknown", "text": "内容", "bbox_image_px": [10.0, 10.0, 200.0, 40.0], "reading_order": 0, "source_refs": ["pdf.line.p1.0000"], "support_refs": ["mineru.block.p1.0000"], "type_candidates": ["table", "text"], "issues": ["table_html_missing"]}]},
    }
    task = {
        "task_version": "ocr-agent-semantic-task.v1",
        "task_id": "sample.p1.semantic.1",
        "sample_id": "sample",
        "heading_context_complete": True,
        "items": [{"page": 1, "block_id": "candidate.block.p1.0000", "current_type": "unknown", "allowed_types": ["table", "text"], "allowed_levels": [], "needs": ["type"], "evidence": [{"evidence_id": "pdf.line.p1.0000"}]}],
    }
    response = {"task_id": task["task_id"], "decisions": [{"block_id": "candidate.block.p1.0000", "type": "table", "evidence_refs": ["pdf.line.p1.0000"]}], "unresolved": []}
    revision, _trace = apply_semantic_decision(page, task, response, 1)
    block = revision["blocks"][0]
    assert block["type"] != "table"
    assert any(item.get("issue") == "table decision without table HTML" for item in revision["review_items"])
