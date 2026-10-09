from copy import deepcopy

from helpers import make_page
from ocr_agent.schemas import validate_page_evidence
from ocr_agent.validators import validate_revision


def initial_revision(page):
    return {
        "blocks": deepcopy(page["current_candidate"]["blocks"]),
        "field_reviews": deepcopy(page["field_reviews"]),
        "review_items": [],
    }


def test_valid_page_and_revision_pass() -> None:
    page = make_page()
    validate_page_evidence(page)
    report = validate_revision(page, initial_revision(page))
    assert report["status"] == "PASS"
    assert report["claimed_required_evidence"] == 1


def test_coordinate_mismatch_fails() -> None:
    page = make_page()
    page["current_candidate"]["blocks"][0]["bbox_pdf_pt"][0] += 20
    report = validate_revision(page, initial_revision(page))
    assert report["status"] == "CHECK"
    assert "invalid_block" in {error["code"] for error in report["errors"]}


def test_title_requires_level() -> None:
    page = make_page()
    del page["current_candidate"]["blocks"][0]["level"]
    report = validate_revision(page, initial_revision(page))
    assert report["status"] == "CHECK"


def test_duplicate_required_evidence_fails() -> None:
    page = make_page()
    duplicate = deepcopy(page["current_candidate"]["blocks"][0])
    duplicate["block_id"] = "candidate.block.p1.0002"
    duplicate["reading_order"] = 1
    revision = initial_revision(page)
    revision["blocks"].append(duplicate)
    revision["field_reviews"]["blocks"][duplicate["block_id"]] = deepcopy(revision["field_reviews"]["blocks"]["candidate.block.p1.0001"])
    report = validate_revision(page, revision)
    assert report["status"] == "CHECK"
    assert "duplicate_evidence_claim" in {error["code"] for error in report["errors"]}


def test_strongly_overlapping_plain_text_blocks_are_rejected() -> None:
    page = make_page()
    original = page["current_candidate"]["blocks"][0]
    original["type"] = "text"
    original.pop("level")
    duplicate = deepcopy(original)
    duplicate["block_id"] = "candidate.block.p1.0002"
    duplicate["reading_order"] = 1
    duplicate["source_refs"] = []
    revision = initial_revision(page)
    revision["blocks"] = [original, duplicate]
    revision["field_reviews"]["blocks"][duplicate["block_id"]] = deepcopy(revision["field_reviews"]["blocks"]["candidate.block.p1.0001"])
    report = validate_revision(page, revision)
    assert "overlapping_text_blocks" in {error["code"] for error in report["errors"]}


def test_same_text_cross_type_overlap_is_rejected() -> None:
    page = make_page()
    original = page["current_candidate"]["blocks"][0]
    duplicate = deepcopy(original)
    duplicate["block_id"] = "candidate.block.p1.0002"
    duplicate["type"] = "footer"
    duplicate.pop("level")
    duplicate["reading_order"] = 1
    duplicate["source_refs"] = []
    revision = initial_revision(page)
    revision["blocks"].append(duplicate)
    revision["field_reviews"]["blocks"][duplicate["block_id"]] = deepcopy(revision["field_reviews"]["blocks"][original["block_id"]])
    report = validate_revision(page, revision)
    assert "duplicate_overlapping_textual_blocks" in {error["code"] for error in report["errors"]}


def test_unverified_field_results_in_review() -> None:
    page = make_page()
    revision = initial_revision(page)
    revision["field_reviews"]["blocks"]["candidate.block.p1.0001"]["level"]["status"] = "CHECK"
    report = validate_revision(page, revision)
    assert report["status"] == "CHECK"
    assert report["unresolved_field_count"] == 1


def test_resolved_review_item_does_not_hold_the_page_back() -> None:
    """A review item whose field a later pass settled must not keep the page in CHECK."""
    page = make_page()
    revision = initial_revision(page)
    block = revision["blocks"][0]
    revision["field_reviews"]["blocks"][block["block_id"]] = {
        "type": {"status": "PASS", "method": "cross_model_type_agreement", "evidence_refs": []},
        "bbox": {"status": "PASS", "method": "pdf_text_exact", "evidence_refs": []},
        "text": {"status": "PASS", "method": "pdf_text_exact", "evidence_refs": []},
    }
    revision["review_items"] = [{
        "field": "type", "target": block["block_id"], "issue": "request failed: The read operation timed out",
        "task_id": "t", "evidence_refs": [],
    }]
    with_item = validate_revision(page, revision)
    without_item = deepcopy(revision)
    without_item["review_items"] = []
    baseline = validate_revision(page, without_item)
    assert with_item["review_count"] == 0 and with_item["resolved_review_count"] == 1
    assert with_item["status"] == baseline["status"]

    # ... an item whose field is still unresolved keeps blocking the page
    revision["field_reviews"]["blocks"][block["block_id"]]["type"]["status"] = "CHECK"
    blocked = validate_revision(page, revision)
    assert blocked["review_count"] == 1 and blocked["status"] == "CHECK"


def test_glyph_noise_text_is_rejected() -> None:
    """A block whose text is broken-font code soup must not pass the text invariant."""
    page = make_page()
    page["current_candidate"]["blocks"][0]["text"] = "\x00\x02\x03\x04\x05\x06\x02\x07\x08\x05"
    report = validate_revision(page, initial_revision(page))
    assert "glyph_noise_text" in {error["code"] for error in report["errors"]}


def test_stray_symbol_glyph_is_tolerated() -> None:
    """A single Wingdings bullet inside real text is sanitised upstream, not an error."""
    page = make_page()
    page["current_candidate"]["blocks"][0]["text"] = "生猪：底部震荡，静待反转"
    report = validate_revision(page, initial_revision(page))
    assert "glyph_noise_text" not in {error["code"] for error in report["errors"]}
