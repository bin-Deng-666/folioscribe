from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time
from typing import Any

from .cross_page import (
    annotate_cross_page_tables,
    apply_cross_page_text_decision,
    build_cross_page_text_task,
    mark_cross_page_text_failure,
)
from .decision_applier import apply_semantic_decision
from .errors import OCRAgentError, OperationError
from .jsonio import write_json_atomic
from .markdown import blocks_to_markdown
from .review_tasks import build_document_relation_tasks, build_semantic_tasks, build_table_tasks
from .schemas import new_revision, validate_page_evidence
from .validators import validate_revision


class TaskOCRAgent:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def run_page(self, raw_page: dict[str, Any], run_dir: str | Path) -> dict[str, Any]:
        page = deepcopy(raw_page)
        validate_page_evidence(page)
        root = Path(run_dir)
        root.mkdir(parents=True, exist_ok=True)

        operation_log = []
        review_items = []
        document_relations = []
        trace_count = 0
        task_count = 0
        for task in build_semantic_tasks(page):
            task_count += 1
            write_json_atomic(root / "tasks" / f"{task['task_id']}.json", task)
            task_started = time.monotonic()
            try:
                response, trace = self.provider.complete_semantic(task)
                trace_count += 1
                write_json_atomic(root / "traces" / f"{task['task_id']}.json", trace)
                write_json_atomic(root / "decisions" / f"{task['task_id']}.json", response)
                try:
                    revision, _ = apply_semantic_decision(page, task, response, task_count)
                except OperationError as first_error:
                    retry_task = deepcopy(task)
                    retry_task["retry_instruction"] = (
                        f"The previous decision was rejected: {first_error}. "
                        "Return every task item exactly once and use only its allowed values and evidence IDs."
                    )
                    response, retry_trace = self.provider.complete_semantic(retry_task)
                    trace_count += 1
                    write_json_atomic(root / "traces" / f"{task['task_id']}.retry.json", retry_trace)
                    write_json_atomic(root / "decisions" / f"{task['task_id']}.retry.json", response)
                    revision, _ = apply_semantic_decision(page, task, response, task_count)
                operation_log.extend(revision["operation_log"])
                review_items.extend(revision["review_items"])
                page["current_candidate"] = {"blocks": deepcopy(revision["blocks"])}
                page["field_reviews"] = deepcopy(revision["field_reviews"])
            except OCRAgentError as exc:
                _stabilize_failed_semantic_task(page, task)
                review_items.append({"field": "semantic", "issue": str(exc), "target": None, "task_id": task["task_id"], "evidence_refs": []})
                error_record = {"task_id": task["task_id"], "status": "CHECK", "error": str(exc), "elapsed_seconds": round(time.monotonic() - task_started, 3)}
                if isinstance(getattr(exc, "trace", None), dict):
                    error_record["provider_trace"] = exc.trace
                write_json_atomic(root / "decisions" / f"{task['task_id']}.error.json", error_record)

        _stabilize_final_ontology(page)

        for task in build_document_relation_tasks(page):
            task_count += 1
            write_json_atomic(root / "tasks" / f"{task['task_id']}.json", task)
            try:
                response = _deterministic_word_table_relation(task)
                if response is None:
                    response, trace = self.provider.complete_relation(task)
                    trace_count += 1
                    write_json_atomic(root / "traces" / f"{task['task_id']}.json", trace)
                else:
                    write_json_atomic(root / "traces" / f"{task['task_id']}.json", {"provider": "deterministic_word_table_relation", "task_id": task["task_id"], "elapsed_seconds": 0.0, "response": response})
                if response.get("relation") == "continuation":
                    previous = next((item for item in task.get("previous_tables", []) if item.get("block_id") == response.get("previous_block_id")), {})
                    table_identity = previous.get("document_table_identity") or f"{page['sample_id']}.table.{response.get('previous_block_id') or task['task_id']}"
                    response["table_identity"] = table_identity
                    current_ids = set(response.get("current_block_ids", []))
                    for block in page.get("current_candidate", {}).get("blocks", []):
                        if block.get("block_id") in current_ids and block.get("type") == "table":
                            block["document_table_identity"] = table_identity
                write_json_atomic(root / "decisions" / f"{task['task_id']}.json", response)
                document_relations.append(response)
                if response.get("relation") == "uncertain":
                    review_items.append({"field": "document_relation", "issue": response.get("reason", "cross-page relation unresolved"), "target": None, "task_id": task["task_id"], "evidence_refs": []})
            except (OCRAgentError, ValueError, KeyError) as exc:
                review_items.append({"field": "document_relation", "issue": str(exc), "target": None, "task_id": task["task_id"], "evidence_refs": []})
                write_json_atomic(root / "decisions" / f"{task['task_id']}.error.json", {"task_id": task["task_id"], "status": "CHECK", "error": str(exc)})

        cross_page_relations: list[dict[str, Any]] = []
        previous_page_tail = page.get("document_context", {}).get("previous_page_tail", []) or []
        text_task = build_cross_page_text_task(page, previous_page_tail)
        if text_task is not None:
            task_count += 1
            write_json_atomic(root / "tasks" / f"{text_task['task_id']}.json", text_task)
            try:
                response, trace = self.provider.complete_cross_page_text(text_task)
                trace_count += 1
                write_json_atomic(root / "traces" / f"{text_task['task_id']}.json", trace)
                write_json_atomic(root / "decisions" / f"{text_task['task_id']}.json", response)
                record = apply_cross_page_text_decision(page, text_task, response)
                cross_page_relations.append(record)
                if record.get("status") == "CHECK":
                    review_items.append({
                        "field": "cross_page_text",
                        "target": record.get("head_block_id"),
                        "issue": "cross-page text flow unresolved",
                        "task_id": text_task["task_id"],
                        "evidence_refs": record.get("evidence_refs", []),
                    })
            except OCRAgentError as exc:
                record = mark_cross_page_text_failure(page, text_task, str(exc))
                cross_page_relations.append(record)
                review_items.append({
                    "field": "cross_page_text",
                    "target": record.get("head_block_id"),
                    "issue": str(exc),
                    "task_id": text_task["task_id"],
                    "evidence_refs": [],
                })
                write_json_atomic(root / "decisions" / f"{text_task['task_id']}.error.json", {"task_id": text_task["task_id"], "status": "CHECK", "error": str(exc)})

        relation_responses = [item for item in document_relations if "document_relation" in str(item.get("task_id", ""))]
        for record in annotate_cross_page_tables(page, previous_page_tail, relation_responses[-1] if relation_responses else None):
            if record.get("status") == "CHECK":
                review_items.append({
                    "field": "cross_page_table",
                    "target": record.get("block_id"),
                    "issue": str(record.get("method", "cross-page table conflict")),
                    "task_id": None,
                    "evidence_refs": [],
                })

        for task in build_table_tasks(page):
            task_count += 1
            write_json_atomic(root / "tasks" / f"{task['task_id']}.json", task)
            review_items.append({
                "field": "html",
                "issue": "table HTML requires verified PDF grid or table reconstruction tool",
                "target": task["block_id"],
                "task_id": task["task_id"],
                "evidence_refs": [candidate["evidence_id"] for candidate in task["candidates"]],
            })

        revision = new_revision(page, task_count)
        revision["operation_log"] = operation_log
        revision["review_items"] = review_items
        revision["document_relations"] = document_relations
        revision["cross_page_relations"] = cross_page_relations
        return self._finish(page, revision, root, task_count=task_count, trace_count=trace_count)

    @staticmethod
    def _finish(page: dict[str, Any], revision: dict[str, Any], root: Path, *, task_count: int, trace_count: int) -> dict[str, Any]:
        report = validate_revision(page, revision)
        result = {
            "status": report["status"],
            "sample_id": page["sample_id"],
            "page": page["page"],
            "task_count": task_count,
            "trace_count": trace_count,
            "revision": revision,
            "validation": report,
        }
        write_json_atomic(root / "validation.json", report)
        write_json_atomic(root / "result.json", result)
        (root / "result.md").write_text(blocks_to_markdown(revision["blocks"]), encoding="utf-8")
        return result


def _deterministic_word_table_relation(task: dict[str, Any]) -> dict[str, Any] | None:
    previous = [item for item in task.get("previous_tables", []) if item.get("word_table_ref") and isinstance(item.get("word_row_range"), list)]
    current = [item for item in task.get("current_top_blocks", []) if item.get("type") == "table" and item.get("word_table_ref") and isinstance(item.get("word_row_range"), list)]
    matches = []
    for left in previous:
        for right in current:
            left_range = left["word_row_range"]
            right_range = right["word_row_range"]
            if left.get("word_table_ref") == right.get("word_table_ref") and len(left_range) == len(right_range) == 2 and left_range[1] + 1 == right_range[0]:
                matches.append((left, right))
    if len(matches) != 1:
        return None
    left, right = matches[0]
    return {
        "task_id": task["task_id"],
        "relation": "continuation",
        "previous_block_id": left["block_id"],
        "current_block_ids": [right["block_id"]],
        "reason": f"same {left['word_table_ref']} with contiguous bound Word row ranges {left['word_row_range']} -> {right['word_row_range']}",
        "method": "word_table_contiguous_row_ranges",
    }


def _stabilize_final_ontology(page: dict[str, Any]) -> None:
    """Keep uncertain text-bearing candidates valid without claiming semantic certainty."""
    reviews = page.get("field_reviews", {}).get("blocks", {})
    for block in page.get("current_candidate", {}).get("blocks", []):
        if block.get("type") != "unknown":
            continue
        block["type"] = "text"
        block.pop("level", None)
        block.pop("parent_id", None)
        block.pop("html", None)
        fields = reviews.setdefault(block["block_id"], {})
        fields["type"] = {
            "status": "CHECK",
            "method": "final_ontology_text_fallback",
            "evidence_refs": list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", []))),
        }
        fields.pop("level", None)
        fields.pop("parent_id", None)
        fields.pop("html", None)


def _stabilize_failed_semantic_task(page: dict[str, Any], task: dict[str, Any]) -> None:
    blocks = {block["block_id"]: block for block in page.get("current_candidate", {}).get("blocks", [])}
    reviews = page.get("field_reviews", {}).get("blocks", {})
    for item in task.get("items", []):
        block = blocks.get(item.get("block_id"))
        if not block or block.get("type") != "unknown" or "text" not in item.get("allowed_types", []):
            continue
        # a caption candidate whose figure anchor is in the task keeps its caption role even when
        # the model call failed: the geometry already bound it, and "text" would erase the binding
        context = item.get("relation_context") or {}
        if context.get("caption_of") and item.get("allowed_types") and "image_caption" in item["allowed_types"]:
            block["type"] = "image_caption"
            block["parent_id"] = context["caption_of"]
            fields = reviews.setdefault(block["block_id"], {})
            fields["type"] = {"status": "CHECK", "method": "failed_task_caption_geometry", "evidence_refs": [e.get("evidence_id") for e in item.get("evidence", [])[:3]]}
            fields["parent_id"] = {"status": "CHECK", "method": "failed_task_caption_geometry", "evidence_refs": []}
            continue
        block["type"] = "text"
        block.pop("level", None)
        block.pop("parent_id", None)
        fields = reviews.setdefault(block["block_id"], {})
        fields["type"] = {
            "status": "CHECK",
            "method": "semantic_failure_text_fallback",
            "evidence_refs": [entry["evidence_id"] for entry in item.get("evidence", [])],
        }


