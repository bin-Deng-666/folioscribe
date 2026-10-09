from helpers import make_page
from ocr_agent.schemas import new_revision
from ocr_agent.task_agent import TaskOCRAgent, _deterministic_word_table_relation, _stabilize_final_ontology


class FailingProvider:
    def complete_semantic(self, task):
        from ocr_agent.errors import ProviderTransientError
        raise ProviderTransientError("timeout")


class SemanticProvider:
    def complete_semantic(self, task):
        decisions = []
        for item in task["items"]:
            decisions.append({
                "block_id": item["block_id"],
                "type": "title",
                "level": 1,
                "evidence_refs": [item["evidence"][0]["evidence_id"]],
            })
        response = {"task_id": task["task_id"], "decisions": decisions, "unresolved": []}
        return response, {"provider": "fake", "response": response}


def test_contiguous_bound_word_fragments_determine_table_continuation() -> None:
    task = {
        "task_id": "relation.1",
        "previous_tables": [{"block_id": "p1", "word_table_ref": "word.table.1", "word_row_range": [0, 4]}],
        "current_top_blocks": [{"block_id": "p2", "type": "table", "word_table_ref": "word.table.1", "word_row_range": [5, 8]}],
    }
    response = _deterministic_word_table_relation(task)
    assert response["relation"] == "continuation"
    assert response["method"] == "word_table_contiguous_row_ranges"
    task["current_top_blocks"][0]["word_row_range"] = [6, 8]
    assert _deterministic_word_table_relation(task) is None


def test_task_agent_applies_compact_semantic_sheet(tmp_path) -> None:
    page = make_page()
    page["document_context"]["previous_pages_complete"] = True
    page["field_reviews"]["blocks"]["candidate.block.p1.0001"]["level"]["status"] = "CHECK"
    result = TaskOCRAgent(SemanticProvider()).run_page(page, tmp_path)
    assert result["status"] == "PASS"
    assert result["task_count"] == 1
    assert result["revision"]["field_reviews"]["blocks"]["candidate.block.p1.0001"]["level"]["status"] == "PASS"
    assert list((tmp_path / "tasks").glob("*.json"))
    assert list((tmp_path / "decisions").glob("*.json"))
    assert (tmp_path / "result.md").read_text(encoding="utf-8").startswith("# Heading")


def test_task_agent_preserves_validator_fail_status(tmp_path) -> None:
    page = make_page()
    revision = new_revision(page, 0)
    revision["blocks"][0]["bbox_normalized"] = [0.1, 0.1, 0.2, 0.2]
    result = TaskOCRAgent._finish(page, revision, tmp_path, task_count=0, trace_count=0)
    assert result["status"] == "CHECK"
    assert result["validation"]["error_count"] > 0


def test_final_ontology_fallback_keeps_uncertain_grounded_text_valid() -> None:
    page = make_page()
    block = page["current_candidate"]["blocks"][0]
    block["type"] = "unknown"
    block.pop("level")
    _stabilize_final_ontology(page)
    assert block["type"] == "text"
    review = page["field_reviews"]["blocks"][block["block_id"]]["type"]
    assert review["status"] == "CHECK"
    assert review["method"] == "final_ontology_text_fallback"
    assert new_revision(page, 1)["blocks"][0]["type"] == "text"


def test_failed_semantic_task_keeps_revision_valid_and_check(tmp_path) -> None:
    page = make_page()
    block = page["current_candidate"]["blocks"][0]
    block["type"] = "unknown"
    block.pop("level")
    block["type_candidates"] = ["text", "title"]
    page["field_reviews"]["blocks"][block["block_id"]]["type"]["status"] = "CHECK"
    page["field_reviews"]["blocks"][block["block_id"]].pop("level")
    result = TaskOCRAgent(FailingProvider()).run_page(page, tmp_path)
    assert result["status"] == "CHECK"
    assert result["revision"]["blocks"][0]["type"] == "text"
    assert result["validation"]["error_count"] == 0
    assert result["validation"]["unresolved_field_count"] == 1
    assert result["revision"]["field_reviews"]["blocks"][block["block_id"]]["type"]["method"] == "semantic_failure_text_fallback"
