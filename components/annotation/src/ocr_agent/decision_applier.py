from __future__ import annotations

from copy import deepcopy
from typing import Any

from .constants import ALLOWED_TYPES, CAPTION_TYPES, MEDIA_TYPES
from .errors import OperationError
from .schemas import evidence_index, new_revision
from .validators import validate_revision


def _usable_caption_parent(block_type: str, requested: str | None, blocks: dict[str, dict], item: dict) -> str | None:
    """A caption may only hang off the family its type claims.

    The task lists candidate parents for the block's *current* type, but the model decides the type in
    the same answer, so the pair has to be re-checked here: an ``image_caption`` pointing at a table is
    not a weaker claim, it is an impossible one. When the requested parent does not match, fall back to
    the nearest allowed parent; when none exists the caller demotes the block to text.
    """
    expected = {"table"} if block_type == "table_caption" else {"image", "chart"}
    allowed = [parent for parent in item.get("allowed_parent_ids", []) if parent in blocks]
    matching = [parent for parent in allowed if blocks[parent].get("type") in expected]
    if requested in matching:
        return requested
    return matching[0] if matching else None


def _merge_is_plausible(loser: dict[str, Any], winner: dict[str, Any], item: dict[str, Any]) -> bool:
    """Veto a merge that would eat furniture, a captioned figure, or a pair the geometry contradicts."""
    for block in (loser, winner):
        if str(block.get("type")) in {"header", "footer", "page_number", "table"}:
            return False
        if str(block.get("parent_id") or "").strip():
            return False
    # form/field lines stay alone: a line that ends with a colon+blank or a run of underscores is a
    # form field (合同编号：____), and the annotators keep every field on its own
    for block in (loser, winner):
        text = str(block.get("text") or "").strip()
        if "___" in text or text.endswith(("：", ":")):
            return False
        if "\n" in text and any("___" in line or line.rstrip().endswith(("：", ":")) for line in text.split("\n")):
            return False
    context = item.get("relation_context") or {}
    gap = context.get("gap_px")
    line_height = context.get("line_height_px")
    if gap is not None and line_height:
        if gap > float(line_height) * 1.5:
            return False
    return True

def _decoration_is_plausible(page: dict[str, Any], blocks: list[dict[str, Any]], block: dict[str, Any]) -> bool:
    """A veto over "this image is decoration": keep anything that looks like a real figure.

    A figure that is a significant part of the page, that a caption points at, or that carries text is
    something the annotator keeps; only small or over-covered graphics are dropped.
    """
    if str(block.get("type")) in {"header", "footer", "page_number"}:
        return False
    if any(str(other.get("parent_id") or "") == str(block.get("block_id")) for other in blocks):
        return False  # a caption belongs to it
    if str(block.get("text") or "").strip():
        return False
    # A block backed by a real PDF raster is a picture the source page truly contains; the parsers
    # agreed on it (required_coverage), so "decoration" needs stronger grounds than smallness.
    if any(str(ref).startswith("pdf.image.") for ref in (block.get("source_refs") or []) + (block.get("support_refs") or [])):
        return False
    image = page.get("page_image") or {}
    width, height = float(image.get("width") or 0), float(image.get("height") or 0)
    box = [float(value) for value in (block.get("bbox_image_px") or [0, 0, 0, 0])]
    area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
    page_area = max(1.0, width * height)

    def covered_by(other: dict) -> float:
        other_box = [float(value) for value in (other.get("bbox_image_px") or [0, 0, 0, 0])]
        x1, y1 = max(box[0], other_box[0]), max(box[1], other_box[1])
        x2, y2 = min(box[2], other_box[2]), min(box[3], other_box[3])
        return (max(0.0, x2 - x1) * max(0.0, y2 - y1)) / max(1.0, area)

    # "Small" alone is not evidence of decoration - the annotators keep small real figures. A graphic is
    # only decoration when a table/text block already covers it, or another figure contains it, or it is
    # a tiny speck next to text.
    covered = any(
        other is not block
        and str(other.get("type")) in {"table", "text", "title", "formula"}
        and covered_by(other) >= 0.35
        for other in blocks
    )
    inside_figure = any(
        other is not block
        and str(other.get("type")) in {"image", "chart", "seal"}
        and covered_by(other) >= 0.6
        for other in blocks
    )
    return covered or inside_figure or area / page_area < 0.01


def apply_semantic_decision(page: dict[str, Any], task: dict[str, Any], response: dict[str, Any], revision_number: int) -> tuple[dict[str, Any], dict[str, Any]]:
    if response.get("task_id") != task["task_id"]:
        raise OperationError("semantic decision task_id mismatch")
    decisions = response.get("decisions")
    unresolved = response.get("unresolved", [])
    if not isinstance(decisions, list) or not isinstance(unresolved, list):
        raise OperationError("semantic response requires decisions and unresolved lists")
    task_items = {item["block_id"]: item for item in task["items"]}
    revision = new_revision(page, revision_number)
    blocks = {block["block_id"]: block for block in revision["blocks"]}
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            raise OperationError("semantic decision must be an object")
        block_id = decision.get("block_id")
        if block_id not in task_items or block_id in seen:
            raise OperationError(f"unknown or duplicate semantic block_id: {block_id}")
        seen.add(block_id)
        item, block = task_items[block_id], blocks[block_id]
        refs = decision.get("evidence_refs")
        allowed_refs = {entry["evidence_id"] for entry in item["evidence"]}
        if not isinstance(refs, list) or not refs or set(refs) - allowed_refs:
            raise OperationError(f"{block_id}: invalid evidence_refs")
        block_type = decision.get("type", block.get("type"))
        if block_type not in item["allowed_types"] or block_type not in ALLOWED_TYPES:
            raise OperationError(f"{block_id}: type is not allowed: {block_type!r}")
        fields = revision["field_reviews"]["blocks"].setdefault(block_id, {})
        if block_type == "table" and not (isinstance(block.get("html"), str) and "<table" in block["html"].lower()):
            evidence = evidence_index(page)
            html_value = next(
                (
                    str(evidence[ref]["html"])
                    for ref in block.get("support_refs", [])
                    if isinstance(evidence.get(ref, {}).get("html"), str) and "<table" in evidence[ref]["html"].lower()
                ),
                None,
            )
            if html_value is None:
                block_type = block.get("type") or "text"
                revision["review_items"].append({"field": "type", "target": block_id, "issue": "table decision without table HTML", "evidence_refs": refs})
            else:
                block["html"] = html_value
                fields["html"] = {"status": "CHECK", "method": "semantic_table_html_from_model_evidence", "evidence_refs": list(block.get("support_refs", []))}
        if block_type == "title" and decision.get("level") not in item["allowed_levels"]:
            revision["review_items"].append({"field": "level", "target": block_id, "issue": "semantic title decision omitted a valid level", "evidence_refs": refs})
            continue
        block["type"] = block_type
        resolved_issues = {"type_conflict", "title_level_review", "marginal_role_review", "document_heading_sequence_review"}
        block["issues"] = [issue for issue in block.get("issues", []) if (issue.get("kind") if isinstance(issue, dict) else issue) not in resolved_issues]
        fields["type"] = {"status": "PASS", "method": "semantic_task", "evidence_refs": refs}
        if block_type != "table":
            block.pop("html", None)
            fields.pop("html", None)
        if block_type in MEDIA_TYPES:
            block.pop("text", None)
            fields.pop("text", None)
        if block_type == "title":
            level = decision["level"]
            block["level"] = level
            if task.get("heading_context_complete"):
                fields["level"] = {"status": "PASS", "method": "document_heading_sequence", "evidence_refs": refs}
            else:
                fields["level"] = {"status": "CHECK", "method": "incomplete_previous_page_context", "evidence_refs": refs}
                revision["review_items"].append({"field": "level", "target": block_id, "issue": "previous pages were not processed continuously", "evidence_refs": refs})
        else:
            block.pop("level", None)
            fields.pop("level", None)
        if block_type in CAPTION_TYPES:
            parent_id = _usable_caption_parent(block_type, decision.get("parent_id"), blocks, item)
            if parent_id is None:
                # the model typed a caption but no parent of the matching family exists: a caption
                # without its figure is ordinary text, so demote instead of emitting an invalid pair
                block["type"] = "text"
                block.pop("parent_id", None)
                fields.pop("parent_id", None)
                block.setdefault("issues", []).append("caption_without_matching_parent")
                revision["review_items"].append({
                    "field": "type",
                    "target": block_id,
                    "issue": "caption decision had no parent of the matching family; demoted to text",
                    "evidence_refs": refs,
                })
            else:
                block["parent_id"] = parent_id
                fields["parent_id"] = {"status": "PASS", "method": "caption_neighbour_binding", "evidence_refs": refs}
        else:
            block.pop("parent_id", None)
            fields.pop("parent_id", None)
    for unresolved_item in unresolved:
        if not isinstance(unresolved_item, dict):
            raise OperationError("invalid unresolved semantic item")
        if unresolved_item.get("block_id") not in task_items:
            continue
        block_id = unresolved_item["block_id"]
        seen.add(block_id)
        field = unresolved_item.get("field", "type")
        block = blocks[block_id]
        if field == "level" and block.get("type") != "title":
            continue
        if field == "parent_id" and block.get("type") not in CAPTION_TYPES:
            continue
        revision["review_items"].append({"field": field, "target": block_id, "issue": str(unresolved_item.get("reason", "semantic decision unresolved")), "evidence_refs": []})
    # Relation verdicts: only the model's judgement removes or merges a block, and every merge keeps the
    # evidence of the block that disappears.
    # Captions confirmed by the model are bound to their figure/table (the type and parent_id arrive in
    # the decision itself, so nothing extra is needed here beyond the evidence log).
    for decision in decisions:
        if isinstance(decision, dict) and decision.get("parent_id"):
            target_block = blocks.get(decision.get("block_id") or "")
            if target_block is not None:
                revision["operation_log"].append({
                    "kind": "caption_binding",
                    "block_id": decision.get("block_id"),
                    "parent_id": decision.get("parent_id"),
                    "type": decision.get("type"),
                })
    dropped: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        block_id = decision.get("block_id")
        relation = decision.get("relation")
        if not isinstance(relation, str) or block_id not in blocks:
            continue
        target = None
        if relation.startswith("continuation_of_previous:"):
            target = relation.split(":", 1)[1]
        elif relation.startswith("duplicate_of:"):
            target = relation.split(":", 1)[1]
        elif relation.startswith("caption_of:"):
            # the model confirmed the geometry: bind type and parent here
            anchor_id = relation.split(":", 1)[1]
            block = blocks.get(block_id)
            item = task_items.get(block_id) or {}
            allowed_parents = set(item.get("allowed_parent_ids") or [])
            if block is not None and anchor_id in blocks and (not allowed_parents or anchor_id in allowed_parents):
                context = item.get("relation_context") or {}
                block["type"] = context.get("caption_type") or "image_caption"
                block["parent_id"] = anchor_id
                revision["operation_log"].append({"kind": "caption_binding", "block_id": block_id, "parent_id": anchor_id, "type": block["type"]})
            continue
        if relation == "decoration" and len(blocks) > 1:
            # The checker keeps a veto: a verdict never removes a figure that is substantial, that owns
            # a caption, or that is page furniture. Dropping those was the model's one systematic
            # mistake (37 real figures and 8 running heads disappeared in the last round).
            candidate = blocks[block_id]
            if _decoration_is_plausible(page, revision["blocks"], candidate):
                dropped.add(block_id)
                revision["operation_log"].append({"kind": "inventory_relation", "block_id": block_id, "relation": "decoration"})
            else:
                revision["operation_log"].append({"kind": "inventory_relation", "block_id": block_id, "relation": "decoration_rejected"})
            continue
        if target and target in blocks and target != block_id:
            # Same veto as for removal: furniture is never merged away, a figure that owns a caption is
            # never swallowed, and the merge has to be geometry-consistent with what the model was shown.
            loser = blocks[block_id]
            winner = blocks[target]
            if not _merge_is_plausible(loser, winner, task_items.get(block_id) or {}):
                revision["operation_log"].append({"kind": "inventory_relation", "block_id": block_id, "relation": f"{relation}_rejected"})
                continue
            survivor, loser = blocks[target], blocks[block_id]
            survivor["source_refs"] = list(dict.fromkeys(list(survivor.get("source_refs") or []) + list(loser.get("source_refs") or [])))
            survivor["support_refs"] = list(dict.fromkeys(list(survivor.get("support_refs") or []) + list(loser.get("support_refs") or [])))
            left, right = str(survivor.get("text") or ""), str(loser.get("text") or "")
            if right and right not in left:
                survivor["text"] = f"{left}\n{right}".strip() if left else right
            survivor["bbox_image_px"] = [
                min(survivor["bbox_image_px"][0], loser["bbox_image_px"][0]),
                min(survivor["bbox_image_px"][1], loser["bbox_image_px"][1]),
                max(survivor["bbox_image_px"][2], loser["bbox_image_px"][2]),
                max(survivor["bbox_image_px"][3], loser["bbox_image_px"][3]),
            ]
            dropped.add(block_id)
            revision["operation_log"].append({"kind": "inventory_relation", "block_id": block_id, "relation": relation, "merged_into": target})
    if dropped:
        revision["blocks"] = [block for block in revision["blocks"] if block["block_id"] not in dropped]
        for index, block in enumerate(revision["blocks"]):
            block["reading_order"] = index
    missing = set(task_items) - seen
    if missing:
        raise OperationError(f"semantic response omitted blocks: {sorted(missing)}")
    revision["operation_log"].append({"task_id": task["task_id"], "kind": "semantic_decisions", "decisions": deepcopy(decisions), "unresolved": deepcopy(unresolved)})
    return revision, validate_revision(page, revision)


def validate_semantic_response(task: dict[str, Any], response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise OperationError("semantic response must be an object")
    if response.get("task_id") != task["task_id"]:
        raise OperationError("semantic response task_id mismatch")
    decisions = response.get("decisions")
    unresolved = response.get("unresolved")
    if not isinstance(decisions, list) or not isinstance(unresolved, list):
        raise OperationError("semantic response requires decisions and unresolved")
    items = {item["block_id"]: item for item in task.get("items", [])}
    seen: set[str] = set()
    for decision in decisions:
        block_id = decision.get("block_id") if isinstance(decision, dict) else None
        if block_id not in items or block_id in seen:
            raise OperationError(f"semantic response has unknown or duplicate block_id: {block_id}")
        item = items[block_id]
        refs = decision.get("evidence_refs")
        allowed_refs = {entry.get("evidence_id") for entry in item.get("evidence", [])}
        if not isinstance(refs, list) or not refs or set(refs) - allowed_refs:
            raise OperationError(f"{block_id}: semantic response has invalid evidence_refs")
        block_type = decision.get("type")
        if block_type not in item.get("allowed_types", []):
            raise OperationError(f"{block_id}: semantic response has invalid type")
        if block_type == "title" and decision.get("level") not in item.get("allowed_levels", []):
            raise OperationError(f"{block_id}: semantic response has invalid title level")
        if block_type != "title" and decision.get("level") not in {None}:
            raise OperationError(f"{block_id}: non-title semantic response must not contain level")
        if block_type in CAPTION_TYPES and decision.get("parent_id") not in item.get("allowed_parent_ids", []):
            raise OperationError(f"{block_id}: semantic response has invalid parent_id")
        if block_type not in CAPTION_TYPES and decision.get("parent_id") not in {None}:
            raise OperationError(f"{block_id}: non-caption semantic response must not contain parent_id")
        seen.add(block_id)
    for entry in unresolved:
        block_id = entry.get("block_id") if isinstance(entry, dict) else None
        if block_id not in items or block_id in seen:
            raise OperationError(f"semantic unresolved has unknown or duplicate block_id: {block_id}")
        if entry.get("field") not in {"type", "level", "parent_id"}:
            raise OperationError(f"{block_id}: semantic unresolved has invalid field")
        seen.add(block_id)
    if seen != set(items):
        raise OperationError(f"semantic response omitted blocks: {sorted(set(items) - seen)}")
    return response
