from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .constants import (
    ALLOWED_OPERATIONS,
    ALLOWED_TYPES,
    CAPTION_TYPES,
    FIELD_REVIEW_VERSION,
    FIELD_STATUSES,
    MAX_TITLE_LEVEL,
    REVISION_VERSION,
    SCHEMA_VERSION,
    TEXT_TYPES,
)
from .errors import SchemaError


def require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be an object")
    return value


def require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaError(f"{name} must be a list")
    return value


def validate_bbox(value: Any, *, width: float, height: float, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise SchemaError(f"{name} must contain four numbers")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise SchemaError(f"{name} must contain finite numbers")
    x1, y1, x2, y2 = [float(v) for v in value]
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise SchemaError(f"{name} is outside the page or degenerate: {value}")
    return [x1, y1, x2, y2]


def convert_bbox(image_bbox: list[float], page: dict[str, Any]) -> tuple[list[float], list[float]]:
    iw, ih = page["page_image"]["width"], page["page_image"]["height"]
    pw, ph = page["pdf_page"]["width_pt"], page["pdf_page"]["height_pt"]
    x1, y1, x2, y2 = image_bbox
    pdf = [min(pw, max(0.0, round(x1 / iw * pw, 4))), min(ph, max(0.0, round(y1 / ih * ph, 4))), min(pw, max(0.0, round(x2 / iw * pw, 4))), min(ph, max(0.0, round(y2 / ih * ph, 4)))]
    normalized = [min(1.0, max(0.0, round(x1 / iw, 8))), min(1.0, max(0.0, round(y1 / ih, 8))), min(1.0, max(0.0, round(x2 / iw, 8))), min(1.0, max(0.0, round(y2 / ih, 8)))]
    return pdf, normalized


def add_coordinate_views(block: dict[str, Any], page: dict[str, Any]) -> None:
    pdf, normalized = convert_bbox(block["bbox_image_px"], page)
    block["bbox_pdf_pt"] = pdf
    block["bbox_normalized"] = normalized


CROSS_PAGE_KEYS = {
    "cross_page_text": {
        "continues_block_id",
        "continued_by_block_id",
        "status",
        "method",
        "confidence_basis",
        "evidence_refs",
    },
    "cross_page_table": {
        "document_table_identity",
        "part_index",
        "part_count",
        "continues_block_id",
        "continued_by_block_id",
        "truncated_at_page_end",
        "truncated_at_page_start",
        "repeated_header",
        "has_closing_rule",
        "status",
        "method",
        "evidence_refs",
    },
}
CROSS_PAGE_BOOL_KEYS = ("truncated_at_page_end", "truncated_at_page_start", "repeated_header", "has_closing_rule")
CROSS_PAGE_LINK_KEYS = ("continues_block_id", "continued_by_block_id")


def _validate_cross_page_field(block_id: str, block_type: str, name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise SchemaError(f"{block_id}.{name} must be an object")
    unsupported = set(value) - CROSS_PAGE_KEYS[name]
    if unsupported:
        raise SchemaError(f"{block_id}.{name} has unsupported keys: {sorted(unsupported)}")
    for key in CROSS_PAGE_LINK_KEYS:
        target = value.get(key)
        if target is not None and (not isinstance(target, str) or not target):
            raise SchemaError(f"{block_id}.{name}.{key} must be null or a non-empty string")
    status = value.get("status")
    if status is not None and status not in FIELD_STATUSES:
        raise SchemaError(f"{block_id}.{name}.status must be one of {sorted(FIELD_STATUSES)}")
    method = value.get("method")
    if method is not None and (not isinstance(method, str) or not method):
        raise SchemaError(f"{block_id}.{name}.method must be a non-empty string")
    refs = value.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise SchemaError(f"{block_id}.{name}.evidence_refs must be a list of non-empty strings")
    if name == "cross_page_text":
        if block_type not in TEXT_TYPES and block_type != "unknown":
            raise SchemaError(f"{block_id}: cross_page_text is only valid on text-bearing blocks")
        basis = value.get("confidence_basis")
        if basis is not None and (
            not isinstance(basis, dict)
            or any(not isinstance(key, str) or not isinstance(flag, bool) for key, flag in basis.items())
        ):
            raise SchemaError(f"{block_id}.cross_page_text.confidence_basis must map strings to booleans")
        return
    if block_type != "table":
        raise SchemaError(f"{block_id}: cross_page_table is only valid on table blocks")
    for key in ("part_index", "part_count"):
        part = value.get(key)
        if part is not None and (isinstance(part, bool) or not isinstance(part, int) or part < 1):
            raise SchemaError(f"{block_id}.cross_page_table.{key} must be a positive integer")
    index, count = value.get("part_index"), value.get("part_count")
    if isinstance(index, int) and isinstance(count, int) and index > count:
        raise SchemaError(f"{block_id}.cross_page_table.part_index exceeds part_count")
    for key in CROSS_PAGE_BOOL_KEYS:
        flag = value.get(key)
        if flag is not None and not isinstance(flag, bool):
            raise SchemaError(f"{block_id}.cross_page_table.{key} must be a boolean or null")
    identity = value.get("document_table_identity")
    if identity is not None and (not isinstance(identity, str) or not identity):
        raise SchemaError(f"{block_id}.cross_page_table.document_table_identity must be a non-empty string")


def validate_document_flow(data: Any) -> dict[str, Any]:
    item = require_object(data, "document_flow")
    for edge in require_list(item.get("text_edges", []), "document_flow.text_edges"):
        edge_item = require_object(edge, "document_flow.text_edges[]")
        for side in ("from", "to"):
            endpoint = require_object(edge_item.get(side), f"document_flow.text_edges[].{side}")
            if not isinstance(endpoint.get("block_id"), str) or not endpoint.get("block_id"):
                raise SchemaError(f"document_flow.text_edges[].{side}.block_id must be a non-empty string")
            if isinstance(endpoint.get("page"), bool) or not isinstance(endpoint.get("page"), int):
                raise SchemaError(f"document_flow.text_edges[].{side}.page must be an integer")
    for group in require_list(item.get("table_groups", []), "document_flow.table_groups"):
        group_item = require_object(group, "document_flow.table_groups[]")
        if not isinstance(group_item.get("document_table_identity"), str) or not group_item.get("document_table_identity"):
            raise SchemaError("document_flow.table_groups[].document_table_identity must be a non-empty string")
        if isinstance(group_item.get("part_count"), bool) or not isinstance(group_item.get("part_count"), int) or group_item["part_count"] < 1:
            raise SchemaError("document_flow.table_groups[].part_count must be a positive integer")
    return item


def validate_block(
    block: Any,
    *,
    page: dict[str, Any],
    allow_unknown: bool = False,
    max_title_level: int = MAX_TITLE_LEVEL,
) -> dict[str, Any]:
    item = require_object(block, "block")
    block_id = item.get("block_id")
    if not isinstance(block_id, str) or not block_id:
        raise SchemaError("block_id must be a non-empty string")
    allowed = ALLOWED_TYPES | ({"unknown"} if allow_unknown else set())
    block_type = item.get("type")
    if block_type not in allowed:
        raise SchemaError(f"{block_id}: unsupported type {block_type!r}")
    iw, ih = page["page_image"]["width"], page["page_image"]["height"]
    pw, ph = page["pdf_page"]["width_pt"], page["pdf_page"]["height_pt"]
    image_bbox = validate_bbox(item.get("bbox_image_px"), width=iw, height=ih, name=f"{block_id}.bbox_image_px")
    pdf_bbox = validate_bbox(item.get("bbox_pdf_pt"), width=pw, height=ph, name=f"{block_id}.bbox_pdf_pt")
    norm_bbox = validate_bbox(item.get("bbox_normalized"), width=1.0, height=1.0, name=f"{block_id}.bbox_normalized")
    expected_pdf, expected_norm = convert_bbox(image_bbox, page)
    if max(abs(a - b) for a, b in zip(pdf_bbox, expected_pdf)) > 0.05:
        raise SchemaError(f"{block_id}: bbox_pdf_pt does not match bbox_image_px")
    if max(abs(a - b) for a, b in zip(norm_bbox, expected_norm)) > 1e-5:
        raise SchemaError(f"{block_id}: bbox_normalized does not match bbox_image_px")
    if block_type in TEXT_TYPES or block_type == "unknown":
        if not isinstance(item.get("text"), str):
            raise SchemaError(f"{block_id}: type {block_type} requires text")
    if block_type == "table":
        html = item.get("html")
        if not isinstance(html, str) or "<table" not in html.lower() or "</table>" not in html.lower():
            raise SchemaError(f"{block_id}: table requires HTML table")
    if block_type == "title":
        level = item.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= max_title_level:
            raise SchemaError(f"{block_id}: title requires level from 1 to {max_title_level}")
    elif "level" in item:
        raise SchemaError(f"{block_id}: only title may contain level")
    if block_type in CAPTION_TYPES:
        if not isinstance(item.get("parent_id"), str) or not item["parent_id"]:
            raise SchemaError(f"{block_id}: caption requires parent_id")
    elif "parent_id" in item:
        raise SchemaError(f"{block_id}: only caption may contain parent_id")
    for name in CROSS_PAGE_KEYS:
        if name in item:
            _validate_cross_page_field(block_id, block_type, name, item[name])
    order = item.get("reading_order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise SchemaError(f"{block_id}: reading_order must be a non-negative integer")
    for field in ("source_refs", "support_refs"):
        refs = require_list(item.get(field, []), f"{block_id}.{field}")
        if any(not isinstance(ref, str) or not ref for ref in refs):
            raise SchemaError(f"{block_id}.{field} must contain non-empty strings")
    return item


def validate_page_evidence(data: Any) -> dict[str, Any]:
    page = require_object(data, "page evidence")
    if page.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError(f"schema_version must be {SCHEMA_VERSION!r}")
    if not isinstance(page.get("sample_id"), str) or not page["sample_id"]:
        raise SchemaError("sample_id must be a non-empty string")
    if not isinstance(page.get("page"), int) or page["page"] < 1:
        raise SchemaError("page must be a positive integer")
    image = require_object(page.get("page_image"), "page_image")
    pdf_page = require_object(page.get("pdf_page"), "pdf_page")
    if not all(isinstance(image.get(k), int) and image[k] > 0 for k in ("width", "height")):
        raise SchemaError("page_image width and height must be positive integers")
    if not all(isinstance(pdf_page.get(k), (int, float)) and pdf_page[k] > 0 for k in ("width_pt", "height_pt")):
        raise SchemaError("pdf_page dimensions must be positive")
    evidence = require_object(page.get("evidence"), "evidence")
    evidence_ids: set[str] = set()
    for source, records in evidence.items():
        for index, raw in enumerate(require_list(records, f"evidence.{source}")):
            item = require_object(raw, f"evidence.{source}[{index}]")
            evidence_id = item.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id:
                raise SchemaError(f"evidence.{source}[{index}] has no evidence_id")
            if evidence_id in evidence_ids:
                raise SchemaError(f"duplicate evidence_id: {evidence_id}")
            evidence_ids.add(evidence_id)
            if "bbox_image_px" in item:
                validate_bbox(item["bbox_image_px"], width=image["width"], height=image["height"], name=f"{evidence_id}.bbox_image_px")
    candidate = require_object(page.get("current_candidate"), "current_candidate")
    blocks = require_list(candidate.get("blocks"), "current_candidate.blocks")
    block_ids: set[str] = set()
    for raw in blocks:
        block = validate_block(raw, page=page, allow_unknown=True)
        if block["block_id"] in block_ids:
            raise SchemaError(f"duplicate block_id: {block['block_id']}")
        block_ids.add(block["block_id"])
        unknown = (set(block.get("source_refs", [])) | set(block.get("support_refs", []))) - evidence_ids
        if unknown:
            raise SchemaError(f"{block['block_id']} references unknown evidence: {sorted(unknown)}")
    reviews = require_object(page.get("field_reviews", {}), "field_reviews")
    if reviews.get("schema_version") not in {None, FIELD_REVIEW_VERSION}:
        raise SchemaError("unsupported field review version")
    for block_id, fields in require_object(reviews.get("blocks", {}), "field_reviews.blocks").items():
        for field, review in require_object(fields, f"field_reviews.blocks.{block_id}").items():
            status = require_object(review, f"field_reviews.blocks.{block_id}.{field}").get("status")
            if status not in FIELD_STATUSES:
                raise SchemaError(f"unsupported field status: {status!r}")
    return page


def evidence_index(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["evidence_id"]: item for records in page["evidence"].values() for item in records}


def validate_agent_response(data: Any) -> dict[str, Any]:
    response = require_object(data, "agent response")
    status = response.get("status")
    if status not in {"needs_tools", "propose_changes", "complete", "review"}:
        raise SchemaError(f"unsupported agent status: {status!r}")
    calls = require_list(response.get("tool_calls", []), "tool_calls")
    operations = require_list(response.get("operations", []), "operations")
    for index, call in enumerate(calls):
        obj = require_object(call, f"tool_calls[{index}]")
        if not isinstance(obj.get("tool"), str) or not isinstance(obj.get("arguments", {}), dict):
            raise SchemaError(f"tool_calls[{index}] is invalid")
    for index, operation in enumerate(operations):
        obj = require_object(operation, f"operations[{index}]")
        if obj.get("op") not in ALLOWED_OPERATIONS:
            raise SchemaError(f"operations[{index}] has unsupported op {obj.get('op')!r}")
        refs = require_list(obj.get("evidence_refs", []), f"operations[{index}].evidence_refs")
        if obj["op"] != "mark_review" and not refs:
            raise SchemaError(f"operations[{index}] requires evidence_refs")
    if status == "needs_tools" and not calls:
        raise SchemaError("needs_tools response must contain tool_calls")
    if status == "propose_changes" and not operations:
        raise SchemaError("propose_changes response must contain operations")
    return response


def new_revision(page: dict[str, Any], revision: int) -> dict[str, Any]:
    return {
        "schema_version": REVISION_VERSION,
        "sample_id": page["sample_id"],
        "page": page["page"],
        "revision": revision,
        "blocks": deepcopy(page["current_candidate"]["blocks"]),
        "field_reviews": deepcopy(page.get("field_reviews", {})),
        "review_items": [],
        "operation_log": [],
    }
