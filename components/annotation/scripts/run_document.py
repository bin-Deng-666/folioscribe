#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ocr_agent.contract_gt import export_contract_gt
from ocr_agent.cross_page import column_boundaries, finalize_cross_page_flow
from ocr_agent.task_agent import TaskOCRAgent
from ocr_agent.task_provider import TaskProvider
from ocr_agent.document_level import drop_template_graphics
from ocr_agent.inventory_rules import refresh_layout
from ocr_agent.title_consistency import apply_numbering_depth_convention, apply_parallel_group_uniformity, apply_title_consistency_decision, apply_title_level_coherence, build_cover_title_stack_tasks, build_title_consistency_tasks, mark_ambiguous_multiline_root_groups
from ocr_agent.review_tasks import promote_continued_question_candidates
from ocr_agent.provider import ModelConfig, OpenAICompatibleProvider
from ocr_agent.jsonio import read_json, write_json_atomic
from ocr_agent.schemas import validate_document_flow
from ocr_agent.markdown import blocks_to_markdown
from ocr_agent.overlay import render_overlay
from ocr_agent.validators import validate_revision

RUN_PROTOCOL_VERSION = "ocr-agent-document.v4.15"
FINGERPRINT_FILES = (
    ROOT / "scripts" / "run_document.py",
    ROOT / "src" / "ocr_agent" / "constants.py",
    ROOT / "src" / "ocr_agent" / "cross_page.py",
    ROOT / "src" / "ocr_agent" / "decision_applier.py",
    ROOT / "src" / "ocr_agent" / "decision_policies.py",
    ROOT / "src" / "ocr_agent" / "evidence_builder.py",
    ROOT / "src" / "ocr_agent" / "review_tasks.py",
    ROOT / "src" / "ocr_agent" / "schemas.py",
    ROOT / "src" / "ocr_agent" / "inventory_rules.py",
    ROOT / "src" / "ocr_agent" / "reading_order.py",
    ROOT / "src" / "ocr_agent" / "task_agent.py",
    ROOT / "src" / "ocr_agent" / "task_provider.py",
    ROOT / "src" / "ocr_agent" / "title_consistency.py",
    ROOT / "src" / "ocr_agent" / "validators.py",
)


def _implementation_sha256() -> str:
    digest = hashlib.sha256()
    for path in FINGERPRINT_FILES:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_fingerprint(page: dict, config: ModelConfig) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(page, ensure_ascii=False, sort_keys=True).encode())
    settings = {
        "protocol": RUN_PROTOCOL_VERSION,
        "implementation_sha256": _implementation_sha256(),
        "model": config.model,
        "base_url": config.base_url,
        "vision_enabled": config.vision_enabled,
        "reasoning_effort": config.reasoning_effort,
        "max_output_tokens": config.max_output_tokens,
        "temperature": config.temperature,
        "system_prompt": "semantic-type-v11",
        "page_task_prompt": "semantic-task-v13",
    }
    digest.update(json.dumps(settings, ensure_ascii=False, sort_keys=True).encode())
    return digest.hexdigest()


def _bounded_heading_context(candidates: list[dict], page: int, *, radius: int = 6, limit: int = 60) -> list[dict]:
    """Keep heading lookahead bounded for long documents while retaining local and sequence context."""
    local = [item for item in candidates if abs(int(item.get("page", 0)) - page) <= radius]
    sequence = [item for item in candidates if re.match(r"^\s*#*\s*(?:第.+章|chapter\s+\d+|步骤\s*\d+|step\s+\d+)", str(item.get("text", "")), re.IGNORECASE)]
    selected: list[dict] = []
    seen_keys: set[tuple[int, str]] = set()
    for item in candidates[:6] + local + sequence[-12:]:
        key = (int(item.get("page", 0)), str(item.get("block_id", "")))
        if key not in seen_keys:
            seen_keys.add(key)
            selected.append(item)
    return selected[:limit]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document-root", "--pilot-root", dest="document_root", type=Path, default=Path(os.environ.get("OCR_AGENT_DOCUMENT_ROOT", "data/annotation")))
    parser.add_argument("--pages", type=str, default="1,2,3,7,12", help="comma-separated pages; use --all-pages for the whole document")
    parser.add_argument("--all-pages", action="store_true", help="run every page listed by the manifest")
    parser.add_argument("--retry-check", action="store_true", help="rerun cached CHECK pages; cached PASS pages are reused")
    args = parser.parse_args()
    per_sample_manifest = args.document_root / "manifests" / f"{os.environ.get('OCR_AGENT_SAMPLE_ID', '')}.json"
    manifest_path = per_sample_manifest if per_sample_manifest.is_file() else args.document_root / "document_manifest.json"
    if not manifest_path.is_file():
        manifest_path = args.document_root / "pilot_manifest.json"
    manifest = read_json(manifest_path)
    config = ModelConfig.from_env()
    provider = TaskProvider(OpenAICompatibleProvider(config))
    agent = TaskOCRAgent(provider)
    summary = []
    total_started = time.monotonic()
    confirmed_titles = []
    candidate_titles = []
    previous_page = 0
    previous_page_tail = []
    previous_pages_complete = True
    evidence_pages = [read_json(path) for path in manifest["evidence_files"]]
    document_heading_candidates = []
    first_document_page = min((int(page["page"]) for page in evidence_pages), default=1)
    for evidence_page in evidence_pages:
        for block in evidence_page.get("current_candidate", {}).get("blocks", []):
            title_possible = block.get("type") == "title" or "title" in block.get("type_candidates", [])
            structural = bool(block.get("document_heading_sequence_evidence") or block.get("geometric_heading_peer_evidence"))
            if not structural and not (int(evidence_page["page"]) == first_document_page and title_possible):
                continue
            document_heading_candidates.append({
                "page": evidence_page["page"],
                "block_id": block["block_id"],
                "text": str(block.get("text", ""))[:240],
                "current_type": block.get("type"),
                "current_level": block.get("level"),
                "context_kind": "structural_candidate" if structural else "root_candidate",
            })
    selected_pages = set(manifest.get("pages", [])) if args.all_pages else {int(value) for value in args.pages.split(",") if value.strip()}
    for page_index, raw_path in enumerate(manifest["evidence_files"]):
        page = read_json(raw_path)
        if page["page"] not in selected_pages:
            continue
        refresh_layout(page)
        page_started = time.monotonic()
        previous_page_complete = page["page"] == previous_page + 1 and not any(row.get("status") == "CHECK" and row.get("error_type") for row in summary)
        previous_pages_complete = previous_pages_complete and previous_page_complete
        promote_continued_question_candidates(page, previous_page_tail)
        own_candidates = page.get("document_context", {}).get("candidate_titles", [])
        page["document_context"] = {
            "confirmed_titles": confirmed_titles[-100:],
            "candidate_titles": candidate_titles[-100:] + own_candidates,
            "previous_pages_complete": previous_pages_complete,
            "last_processed_page": previous_page,
            "previous_page_tail": previous_page_tail,
            "next_page_head": _page_head(evidence_pages[page_index + 1]) if page_index + 1 < len(evidence_pages) else [],
            "document_heading_candidates": _bounded_heading_context(document_heading_candidates, page["page"]),
        }
        previous_page = page["page"]
        run_dir = args.document_root / "runs" / page["sample_id"] / f"page_{page['page']:04d}"
        input_sha256 = _run_fingerprint(page, config)
        result_path = run_dir / "result.json"
        if result_path.is_file():
            existing = read_json(result_path)
            if existing.get("input_sha256") == input_sha256 and not (args.retry_check and existing.get("status") == "CHECK"):
                (run_dir / "result.md").write_text(blocks_to_markdown(existing.get("revision", {}).get("blocks", [])), encoding="utf-8")
                row = {
                    "page": page["page"],
                    "status": existing.get("status", "CHECK"),
                    "rounds": existing.get("task_count", 0),
                    "errors": existing.get("validation", {}).get("error_count"),
                    "resumed": True,
                }
                row["timing"] = {
                    "wall_seconds": round(time.monotonic() - page_started, 3),
                    "model_seconds": 0.0,
                    "cached_model_seconds": _trace_seconds(run_dir),
                }
                summary.append(row)
                if "first_pass_title_context" not in existing:
                    existing["first_pass_title_context"] = _title_context_snapshot(existing)
                if "first_pass_page_tail" not in existing:
                    existing["first_pass_page_tail"] = _page_tail(existing, page)
                write_json_atomic(result_path, existing)
                _update_title_context(existing, confirmed_titles, candidate_titles)
                previous_page_tail = deepcopy(existing["first_pass_page_tail"])
                print(row)
                continue
            shutil.rmtree(run_dir)
        try:
            result = agent.run_page(page, run_dir)
            result["input_sha256"] = input_sha256
            result["first_pass_title_context"] = _title_context_snapshot(result)
            result["first_pass_page_tail"] = _page_tail(result, page)
            write_json_atomic(result_path, result)
            render_overlay(page, result["revision"]["blocks"], run_dir / "final_overlay.png")
            row = {"page": page["page"], "status": result["status"], "tasks": result["task_count"], "errors": result["validation"].get("error_count"), "unresolved_fields": result["validation"].get("unresolved_field_count"), "timing": {"wall_seconds": round(time.monotonic() - page_started, 3), "model_seconds": _trace_seconds(run_dir)}}
            _update_title_context(result, confirmed_titles, candidate_titles)
            previous_page_tail = deepcopy(result["first_pass_page_tail"])
        except Exception as exc:
            previous_pages_complete = False
            row = {"page": page["page"], "status": "CHECK", "error_type": type(exc).__name__, "error": str(exc), "timing": {"wall_seconds": round(time.monotonic() - page_started, 3), "model_seconds": _trace_seconds(run_dir)}}
        summary.append(row)
        print(row)
    if args.all_pages and len(summary) == len(manifest.get("pages", [])):
        _run_title_consistency_review(args.document_root, manifest, evidence_pages, provider, summary)
    document_flow: dict = {}
    if args.all_pages and len(summary) == len(manifest.get("pages", [])):
        document_flow = _run_cross_page_finalize(args.document_root, manifest, evidence_pages, summary)
        _run_template_graphic_pass(args.document_root, manifest, evidence_pages, summary)
    wall_seconds = round(time.monotonic() - total_started, 3)
    executed = [row for row in summary if not row.get("resumed")]
    timing = {
        "wall_seconds": wall_seconds,
        "model_seconds": round(sum(row.get("timing", {}).get("model_seconds", 0.0) for row in executed), 3),
        "executed_pages": len(executed),
        "resumed_pages": sum(bool(row.get("resumed")) for row in summary),
        "average_executed_page_seconds": round(sum(row.get("timing", {}).get("wall_seconds", 0.0) for row in executed) / len(executed), 3) if executed else 0.0,
    }
    document_status = "PASS" if summary and all(row.get("status") == "PASS" for row in summary) and len(summary) == len(manifest.get("pages", [])) else "CHECK"
    result_pages = []
    markdown_parts = []
    for row in summary:
        page_dir = args.document_root / "runs" / manifest["sample_id"] / f"page_{row['page']:04d}"
        result_path = page_dir / "result.json"
        if result_path.is_file():
            result = read_json(result_path)
            result_pages.append({"page": row["page"], "status": result.get("status"), "result": str(result_path)})
        elif row.get("error_type"):
            # a page that raised before writing its result must stay visible in the document result,
            # otherwise it disappears from the delivery while the counts still claim it was processed
            result_pages.append({
                "page": row["page"],
                "status": "CHECK",
                "error_type": row["error_type"],
                "error": row.get("error"),
                "result": None,
            })
            markdown_path = page_dir / "result.md"
            if markdown_path.is_file():
                markdown_parts.append(f"<!-- page {row['page']} -->\n" + markdown_path.read_text(encoding="utf-8").strip())
    document_result = {
        "schema_version": "ocr-agent-document-result.v1",
        "sample_id": manifest["sample_id"],
        "status": document_status,
        "page_count": len(manifest.get("pages", [])),
        "processed_page_count": len(summary),
        "pages": result_pages,
        "document_flow": document_flow,
        "timing": timing,
    }
    validate_document_flow(document_result.get("document_flow", {}))
    write_json_atomic(args.document_root / "pilot_run_summary.json", {"pages": summary, "confirmed_titles": confirmed_titles, "candidate_titles": candidate_titles, "timing": timing})
    # The document result is persisted before the Contract GT export: the export reads the document
    # flow back from disk, so exporting first would project a stale (or absent) flow on a fresh run.
    write_json_atomic(args.document_root / "document_result.json", document_result)
    document_dir = args.document_root / "documents"
    document_dir.mkdir(exist_ok=True)
    write_json_atomic(document_dir / f"{manifest['sample_id']}.json", document_result)
    # Contract GT projection (A1/A2/A3 + bundle). Export is additive: a failure here must not lose
    # the OCR-Agent run itself, so it is recorded on the document result instead of raised.
    try:
        contract_report = export_contract_gt(args.document_root, str(manifest["sample_id"]))
        document_result["contract_gt"] = {
            "output": contract_report["output"],
            "counts": contract_report["counts"],
            "registered_valid": contract_report["validation"].get("validated"),
        }
    except Exception as exc:  # pragma: no cover - export is best effort
        document_result["contract_gt"] = {"error": f"{type(exc).__name__}: {exc}"}
    write_json_atomic(args.document_root / "document_result.json", document_result)
    write_json_atomic(document_dir / f"{manifest['sample_id']}.json", document_result)
    dataset_manifest_path = args.document_root / "dataset_manifest.json"
    dataset_manifest = read_json(dataset_manifest_path) if dataset_manifest_path.is_file() else {"schema_version": "ocr-agent-dataset-manifest.v1", "dataset": args.document_root.name}
    dataset_manifest.update({"status": document_status, "processed_page_count": len(summary), "pass_pages": sum(row.get("status") == "PASS" for row in summary), "check_pages": sum(row.get("status") == "CHECK" for row in summary), "document_result": str(args.document_root / "document_result.json"), "timing": timing})
    write_json_atomic(dataset_manifest_path, dataset_manifest)
    (args.document_root / "document.md").write_text("\n\n".join(markdown_parts) + ("\n" if markdown_parts else ""), encoding="utf-8")
    print({"document_status": document_status, "document_timing": timing})


def _run_template_graphic_pass(document_root: Path, manifest: dict, evidence_pages: list[dict], summary: list[dict]) -> set:
    """A graphic repeated at the same box across half the document is template art, not content."""
    pages = {int(page["page"]): page for page in evidence_pages}
    results = {}
    for page_number in manifest.get("pages", []):
        path = document_root / "runs" / manifest["sample_id"] / f"page_{int(page_number):04d}" / "result.json"
        if not path.is_file():
            return set()
        results[int(page_number)] = read_json(path)
    touched = drop_template_graphics(len(manifest.get("pages", [])), results)
    for page_number in touched:
        result = results[page_number]
        result["validation"] = validate_revision(pages[page_number], result["revision"])
        result["status"] = result["validation"]["status"]
        run_dir = document_root / "runs" / manifest["sample_id"] / f"page_{page_number:04d}"
        write_json_atomic(run_dir / "result.json", result)
        write_json_atomic(run_dir / "validation.json", result["validation"])
        render_overlay(pages[page_number], result["revision"]["blocks"], run_dir / "final_overlay.png")
    for row in summary:
        result = results.get(int(row["page"]))
        if result and int(row["page"]) in touched:
            row["status"] = result["status"]
            row["errors"] = result["validation"].get("error_count")
    return touched


def _run_cross_page_finalize(document_root: Path, manifest: dict, evidence_pages: list[dict], summary: list[dict]) -> dict:
    """Back-fill reverse cross-page links and table group metadata, then re-validate touched pages."""
    pages = {int(page["page"]): page for page in evidence_pages}
    results = {}
    for page_number in manifest.get("pages", []):
        path = document_root / "runs" / manifest["sample_id"] / f"page_{int(page_number):04d}" / "result.json"
        if not path.is_file():
            return {}
        results[int(page_number)] = read_json(path)
    before = {number: json.dumps(result.get("revision", {}).get("blocks", []), sort_keys=True) for number, result in results.items()}
    flow = finalize_cross_page_flow(pages, results)
    touched = [number for number, result in results.items() if json.dumps(result.get("revision", {}).get("blocks", []), sort_keys=True) != before[number]]
    for page_number in touched:
        result = results[page_number]
        result["validation"] = validate_revision(pages[page_number], result["revision"])
        result["status"] = result["validation"]["status"]
        run_dir = document_root / "runs" / manifest["sample_id"] / f"page_{page_number:04d}"
        write_json_atomic(run_dir / "result.json", result)
        write_json_atomic(run_dir / "validation.json", result["validation"])
        render_overlay(pages[page_number], result["revision"]["blocks"], run_dir / "final_overlay.png")
    for row in summary:
        result = results.get(int(row["page"]))
        if result and int(row["page"]) in touched:
            row["status"] = result["status"]
            row["errors"] = result["validation"].get("error_count")
            row["unresolved_fields"] = result["validation"].get("unresolved_field_count")
    return flow


def _run_title_consistency_review(document_root: Path, manifest: dict, evidence_pages: list[dict], provider: TaskProvider, summary: list[dict]) -> None:
    pages = {int(page["page"]): page for page in evidence_pages}
    results = {}
    for page_number in manifest.get("pages", []):
        path = document_root / "runs" / manifest["sample_id"] / f"page_{int(page_number):04d}" / "result.json"
        if not path.is_file():
            return
        results[int(page_number)] = read_json(path)
    review_root = document_root / "document_reviews" / "title_consistency" / str(manifest["sample_id"])
    reviewed = False
    artifacts_cleared = False
    cover_tasks = build_cover_title_stack_tasks(str(manifest["sample_id"]), pages, results)
    for task in cover_tasks:
        write_json_atomic(review_root / "tasks" / f"{task['task_id']}.json", task)
        try:
            response, trace = provider.complete_semantic(task)
            write_json_atomic(review_root / "traces" / f"{task['task_id']}.json", trace)
            write_json_atomic(review_root / "decisions" / f"{task['task_id']}.json", response)
            results = apply_title_consistency_decision(pages, results, task, response)
            reviewed = True
        except Exception as exc:
            write_json_atomic(review_root / "decisions" / f"{task['task_id']}.error.json", {"task_id": task["task_id"], "status": "CHECK", "error": str(exc)})
            for item in task["items"]:
                result = results[int(item["page"])]
                fields = result["revision"]["field_reviews"]["blocks"][item["block_id"]]
                fields.setdefault("type", {})["status"] = "CHECK"
                fields["type"]["method"] = "document_title_consistency_failure"
                result["revision"].setdefault("review_items", []).append({"field": "type", "target": item["block_id"], "issue": str(exc), "task_id": task["task_id"], "evidence_refs": []})
                result["validation"] = validate_revision(pages[int(item["page"])], result["revision"])
                result["status"] = result["validation"]["status"]
    for round_index in range(1, 3):
        tasks = build_title_consistency_tasks(str(manifest["sample_id"]), pages, results)
        if not tasks:
            break
        if not artifacts_cleared:
            for directory in ("tasks", "traces", "decisions"):
                for stale in (review_root / directory).glob("*.json"):
                    stale.unlink()
            artifacts_cleared = True
        reviewed = True
        for task in tasks:
            task["task_id"] = f"{task['task_id']}.round{round_index}"
            write_json_atomic(review_root / "tasks" / f"{task['task_id']}.json", task)
            try:
                response, trace = provider.complete_semantic(task)
                write_json_atomic(review_root / "traces" / f"{task['task_id']}.json", trace)
                write_json_atomic(review_root / "decisions" / f"{task['task_id']}.json", response)
                results = apply_title_consistency_decision(pages, results, task, response)
            except Exception as exc:
                write_json_atomic(review_root / "decisions" / f"{task['task_id']}.error.json", {"task_id": task["task_id"], "status": "CHECK", "error": str(exc)})
                for item in task["items"]:
                    result = results[int(item["page"])]
                    block_id = item["block_id"]
                    fields = result["revision"]["field_reviews"]["blocks"][block_id]
                    fields.setdefault("type", {})["status"] = "CHECK"
                    fields["type"]["method"] = "document_title_consistency_failure"
                    result["revision"].setdefault("review_items", []).append({"field": "type", "target": block_id, "issue": str(exc), "task_id": task["task_id"], "evidence_refs": []})
                    result["validation"] = validate_revision(pages[int(item["page"])], result["revision"])
                    result["status"] = result["validation"]["status"]
    ambiguous_root_blocks = 0
    if not cover_tasks:
        results, ambiguous_root_blocks = mark_ambiguous_multiline_root_groups(pages, results)
    results, convention_blocks = apply_numbering_depth_convention(pages, results)
    if convention_blocks:
        reviewed = True
    results, parallel_group_blocks = apply_parallel_group_uniformity(pages, results)
    if parallel_group_blocks:
        reviewed = True
    results, level_coherence_blocks = apply_title_level_coherence(pages, results)
    if level_coherence_blocks:
        reviewed = True
    if not reviewed and not ambiguous_root_blocks:
        return
    for page_number, result in results.items():
        run_dir = document_root / "runs" / manifest["sample_id"] / f"page_{page_number:04d}"
        write_json_atomic(run_dir / "result.json", result)
        write_json_atomic(run_dir / "validation.json", result["validation"])
        (run_dir / "result.md").write_text(blocks_to_markdown(result["revision"]["blocks"]), encoding="utf-8")
        render_overlay(pages[page_number], result["revision"]["blocks"], run_dir / "final_overlay.png")
    for row in summary:
        result = results.get(int(row["page"]))
        if result:
            row["status"] = result["status"]
            row["errors"] = result["validation"].get("error_count")
            row["unresolved_fields"] = result["validation"].get("unresolved_field_count")


def _trace_seconds(run_dir: Path) -> float:
    total = 0.0
    for path in list((run_dir / "traces").glob("*.json")) + list((run_dir / "decisions").glob("*.error.json")):
        try:
            total += float(read_json(path).get("elapsed_seconds", 0.0))
        except (OSError, TypeError, ValueError):
            continue
    return round(total, 3)


def _page_head(page: dict) -> list[dict]:
    blocks = sorted(page.get("current_candidate", {}).get("blocks", []), key=lambda block: block.get("reading_order", 0))
    return [
        {
            "block_id": block.get("block_id"),
            "type": block.get("type"),
            "type_candidates": block.get("type_candidates", []),
            "level": block.get("level"),
            "text": str(block.get("text", ""))[:320],
            "reading_order": block.get("reading_order"),
        }
        for block in blocks[:6]
    ]


def _page_tail(result: dict, page: dict | None = None) -> list[dict]:
    evidence = {}
    drawings: list[dict] = []
    lines: list[dict] = []
    page_width = 0.0
    if page:
        evidence = {item.get("evidence_id"): item for values in page.get("evidence", {}).values() for item in values if isinstance(item, dict)}
        drawings = [item for item in page.get("evidence", {}).get("pdf", []) if item.get("kind") == "pdf_drawing"]
        lines = [item for item in page.get("evidence", {}).get("pdf", []) if item.get("kind") == "pdf_line"]
        page_width = float(page.get("page_image", {}).get("width", 0.0))
    blocks = sorted(result.get("revision", {}).get("blocks", []), key=lambda block: block.get("reading_order", 0))
    tail = []
    for block in blocks[-8:]:
        item = {
            "block_id": block.get("block_id"),
            "type": block.get("type"),
            "type_candidates": block.get("type_candidates", []),
            "level": block.get("level"),
            "text": str(block.get("text", ""))[:240],
            "text_tail": str(block.get("text", ""))[-240:],
            "reading_order": block.get("reading_order"),
            "bbox_image_px": block.get("bbox_image_px"),
            "source_refs": block.get("source_refs", []),
        }
        if block.get("repeated_page_header"):
            item["repeated_page_header"] = True
        if block.get("cross_page_text"):
            item["cross_page_text"] = block["cross_page_text"]
        if block.get("cross_page_table"):
            item["cross_page_table"] = block["cross_page_table"]
        if block.get("document_table_identity"):
            item["document_table_identity"] = block["document_table_identity"]
        if block.get("type") == "table":
            if block.get("word_table_ref"):
                item["word_table_ref"] = block["word_table_ref"]
                item["word_row_range"] = block.get("word_row_range")
                # the Word table's own row count decides page state exactly, so the tail must carry it
                item["word_table_shape"] = block.get("word_table_shape")
            table_sources = [evidence[ref] for ref in block.get("support_refs", []) if ref in evidence and evidence[ref].get("type_candidate") == "table"]
            item["table_content"] = [str(source.get("html", ""))[:4000] for source in table_sources if source.get("html")]
            box = block.get("bbox_image_px")
            if isinstance(box, list) and len(box) == 4 and page_width > 0:
                item["column_boundaries"] = column_boundaries(drawings, lines, [float(value) for value in box], page_width)
        tail.append(item)
    return tail



def _title_context_snapshot(result: dict) -> dict[str, list[dict]]:
    confirmed: list[dict] = []
    candidates: list[dict] = []
    reviews = result.get("revision", {}).get("field_reviews", {}).get("blocks", {})
    for block in result.get("revision", {}).get("blocks", []):
        fields = reviews.get(block.get("block_id"), {})
        type_verified = fields.get("type", {}).get("status") == "PASS"
        if type_verified and block.get("type") != "title":
            continue
        if block.get("type") != "title" and "title" not in block.get("type_candidates", []):
            continue
        item = {"block_id": block.get("block_id"), "page": result.get("page"), "text": block.get("text", ""), "level": block.get("level"), "status": "provisional_model_decision"}
        if block.get("type") == "title" and type_verified and fields.get("level", {}).get("status") == "PASS":
            confirmed.append(item)
        else:
            candidates.append(item)
    return {"confirmed": confirmed, "candidates": candidates}


def _update_title_context(result: dict, confirmed: list[dict], candidates: list[dict]) -> None:
    snapshot = result.get("first_pass_title_context")
    if isinstance(snapshot, dict):
        confirmed.extend(deepcopy(snapshot.get("confirmed", [])))
        candidates.extend(deepcopy(snapshot.get("candidates", [])))
        return
    reviews = result.get("revision", {}).get("field_reviews", {}).get("blocks", {})
    for block in result.get("revision", {}).get("blocks", []):
        fields = reviews.get(block.get("block_id"), {})
        type_verified = fields.get("type", {}).get("status") == "PASS"
        if type_verified and block.get("type") != "title":
            continue
        if block.get("type") != "title" and "title" not in block.get("type_candidates", []):
            continue
        item = {"block_id": block.get("block_id"), "page": result.get("page"), "text": block.get("text", ""), "level": block.get("level"), "status": "provisional_model_decision"}
        if block.get("type") == "title" and type_verified and fields.get("level", {}).get("status") == "PASS":
            confirmed.append(item)
        else:
            candidates.append(item)


if __name__ == "__main__":
    main()
