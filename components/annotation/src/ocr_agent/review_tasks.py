from __future__ import annotations

import re
from typing import Any

from .constants import ALLOWED_TYPES, CAPTION_TYPES, MAX_TITLE_LEVEL
from .decision_policies import TYPE_RULES
from .schemas import evidence_index

MAX_SEMANTIC_ITEMS = 4
LOCAL_CONTEXT_CHAR_BUDGET = 2200
LOCAL_CONTEXT_BLOCK_LIMIT = 6


def _context_block(block: dict[str, Any], *, relation: str) -> dict[str, Any]:
    text = str(block.get("text", "")).strip()
    return {
        "relation": relation,
        "block_id": block.get("block_id"),
        "type": block.get("type"),
        "level": block.get("level"),
        "text": text[:500],
        "reading_order": block.get("reading_order"),
        "bbox_image_px": block.get("bbox_image_px"),
    }


def _local_reading_context(blocks: list[dict[str, Any]], target_index: int) -> dict[str, Any]:
    target = _context_block(blocks[target_index], relation="target")
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    used = len(target["text"])
    for distance in range(1, len(blocks)):
        for position, destination, relation in (
            (target_index - distance, before, "before"),
            (target_index + distance, after, "after"),
        ):
            if position < 0 or position >= len(blocks) or len(destination) >= LOCAL_CONTEXT_BLOCK_LIMIT:
                continue
            item = _context_block(blocks[position], relation=relation)
            cost = len(item["text"]) + 80
            if used + cost > LOCAL_CONTEXT_CHAR_BUDGET and destination:
                continue
            destination.append(item)
            used += cost
        if len(before) >= LOCAL_CONTEXT_BLOCK_LIMIT and len(after) >= LOCAL_CONTEXT_BLOCK_LIMIT:
            break
    before.reverse()
    return {"before": before, "target": target, "after": after}


def _provisional_heading_path(confirmed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path: list[dict[str, Any]] = []
    for raw in confirmed:
        level = raw.get("level")
        if not isinstance(level, int) or not 1 <= level <= MAX_TITLE_LEVEL:
            continue
        if re.sub(r"\s+", "", str(raw.get("text", ""))).casefold() in {"目录", "contents"}:
            continue
        path = [item for item in path if int(item["level"]) < level]
        path.append({"page": raw.get("page"), "block_id": raw.get("block_id"), "text": str(raw.get("text", ""))[:240], "level": level, "status": "provisional_model_decision"})
    return path[-8:]


def _document_root_candidates(context: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [item for item in context.get("document_heading_candidates", []) if item.get("context_kind") == "root_candidate"]
    confirmed = context.get("confirmed_titles", [])
    if not candidates and confirmed:
        confirmed_pages = [int(item.get("page", 0)) for item in confirmed if int(item.get("page", 0)) > 0]
        if confirmed_pages:
            first_page = min(confirmed_pages)
            candidates = [item for item in confirmed if int(item.get("page", 0)) == first_page]
    pages = [int(item.get("page", 0)) for item in candidates if int(item.get("page", 0)) > 0]
    if not pages:
        return []
    first_page = min(pages)
    return [
        {
            "page": item.get("page"),
            "block_id": item.get("block_id"),
            "text": str(item.get("text", ""))[:240],
            "current_type": item.get("current_type"),
            "current_level": item.get("current_level"),
            "status": "provisional_root_candidate",
        }
        for item in candidates
        if int(item.get("page", 0)) == first_page
    ][:8]


def _nearby_heading_candidates(context: dict[str, Any], page_number: int, target_id: str) -> list[dict[str, Any]]:
    structural = [
        item for item in context.get("document_heading_candidates", [])
        if item.get("context_kind") == "structural_candidate"
    ]
    candidates = [
        item for item in context.get("confirmed_titles", []) + structural
        if item.get("block_id") != target_id and abs(int(item.get("page", 0)) - page_number) <= 3
    ]
    deduplicated = {str(item.get("block_id")): item for item in candidates}
    candidates = list(deduplicated.values())
    candidates.sort(key=lambda item: (abs(int(item.get("page", 0)) - page_number), int(item.get("page", 0))))
    return [
        {
            "page": item.get("page"),
            "block_id": item.get("block_id"),
            "text": str(item.get("text", ""))[:240],
            "current_type": item.get("current_type"),
            "current_level": item.get("current_level"),
            "status": "provisional_candidate",
        }
        for item in candidates[:12]
    ]


def _cross_page_excerpt(items: list[dict[str, Any]], relation: str) -> list[dict[str, Any]]:
    result = []
    for item in items[:4] if relation == "next_page" else items[-4:]:
        result.append({
            "relation": relation,
            "block_id": item.get("block_id"),
            "type": item.get("type"),
            "level": item.get("level"),
            "text": str(item.get("text", ""))[:320],
            "reading_order": item.get("reading_order"),
        })
    return result


def _semantic_context(page: dict[str, Any], blocks: list[dict[str, Any]], block_index: int, source_summary: list[dict[str, Any]]) -> dict[str, Any]:
    context = page.get("document_context", {})
    block = blocks[block_index]
    return {
        "current_page_reading_context": _local_reading_context(blocks, block_index),
        "provisional_heading_path": _provisional_heading_path(context.get("confirmed_titles", [])),
        "document_root_candidates": _document_root_candidates(context),
        "nearby_heading_candidates": _nearby_heading_candidates(context, int(page["page"]), str(block["block_id"])),
        "previous_page_tail": _cross_page_excerpt(context.get("previous_page_tail", []), "previous_page"),
        "next_page_head": _cross_page_excerpt(context.get("next_page_head", []), "next_page"),
        "source_votes": source_summary,
        "context_note": "Source labels and earlier heading decisions are provisional evidence and may be corrected.",
    }


def _inventory_suspicion(blocks: list[dict[str, Any]], index: int, page: dict[str, Any]) -> dict[str, Any] | None:
    """A question worth asking the model about this block's place among its neighbours.

    The rules no longer edit the result: they only decide *which blocks are worth a question* - is this
    text the continuation of the block above it, is this image really a figure, or is the block merely
    decoration. The model answers, and the applier carries out the verdict.

    The neighbour is found geometrically (the block directly above in the same column), because the
    candidate list is in build order, not reading order - looking at ``blocks[index - 1]`` asked almost
    nothing, which is why the first version never fired.
    """
    from .inventory_rules import _area, _box, _looks_like_table_row, _overlap

    block = blocks[index]
    box = _box(block)
    image = page.get("page_image") or {}
    width, height = float(image.get("width") or 0), float(image.get("height") or 0)
    page_area = max(1.0, width * height)
    block_type = str(block.get("type"))
    others = [other for other in blocks if other is not block]

    if block_type in {"image", "chart", "seal"}:
        covered_by = next(
            (
                other
                for other in others
                if other.get("type") in {"table", "text", "title"}
                and _overlap(box, _box(other)) / max(1.0, _area(box)) >= 0.35
            ),
            None,
        )
        overlapping = next(
            (
                other
                for other in others
                if other.get("type") in {"image", "chart", "seal"} and _overlap(box, _box(other)) / max(1.0, _area(box)) >= 0.5
            ),
            None,
        )
        small = _area(box) < 0.05 * page_area
        if covered_by or overlapping or small:
            return {
                "question": "Is this really a separate figure, or decoration / a fragment already covered by the block it sits on?",
                "allowed_relations": ["standalone", "decoration"],
                "context": {
                    "covered_by": str(covered_by.get("block_id")) if covered_by else None,
                    "covered_by_type": covered_by.get("type") if covered_by else None,
                    "overlapping_figure": str(overlapping.get("block_id")) if overlapping else None,
                    "area_ratio": round(_area(box) / page_area, 4),
                    "has_text": bool(str(block.get("text") or "").strip()),
                },
            }

    if block_type in {"text", "unknown", "title"} and str(block.get("text") or "").strip():
        # A line that sits against a figure/table and spans its width is that figure's caption. The
        # models only tag captions when the wording looks like one, so captions arrived as plain text
        # (19 of the type corrections were exactly this) - position and width are the reliable signal.
        anchor = None
        for other in others:
            if str(other.get("type")) not in {"image", "chart", "table", "formula"}:
                continue
            other_box = _box(other)
            horizontal = _overlap([box[0], 0, box[2], 1], [other_box[0], 0, other_box[2], 1]) / max(1.0, min(box[2] - box[0], other_box[2] - other_box[0]))
            if horizontal < 0.6:
                continue
            gap = min(abs(box[1] - other_box[3]), abs(other_box[1] - box[3]))
            if gap > height * 0.05:
                continue
            if _area(box) > 0.3 * max(1.0, _area(other_box)):
                continue
            anchor = other
            break
        if anchor is not None:
            caption_type = "table_caption" if str(anchor.get("type")) == "table" else "image_caption"
            return {
                "question": f"Is this text the caption of the {anchor.get('type')} block {anchor.get('block_id')} (bound through parent_id), or an independent text block? Chart titles like 'XX（元/吨）' above a chart ARE captions.",
                "allowed_relations": ["standalone", f"caption_of:{anchor.get('block_id')}"],
                "context": {
                    "caption_of": str(anchor.get("block_id")),
                    "anchor_type": str(anchor.get("type")),
                    "caption_type": caption_type,
                    "gap_px": round(gap, 1),
                    "width_overlap": round(horizontal, 2),
                },
            }

    if block_type == "text" and str(block.get("text") or "").strip():
        # the block directly above, in the same column
        above = None
        for other in others:
            other_box = _box(other)
            if str(other.get("type")) not in {"text", "title", "formula", "image_caption", "table_caption"}:
                continue
            if other_box[3] > box[1] + 1:  # not above
                continue
            span = _overlap([box[0], 0, box[2], 1], [other_box[0], 0, other_box[2], 1]) / max(1.0, min(box[2] - box[0], other_box[2] - other_box[0]))
            if span < 0.4:
                continue
            if above is None or other_box[3] > _box(above)[3]:
                above = other
        if above is not None:
            previous_text = str(above.get("text") or "").strip()
            current_text = str(block.get("text") or "").strip()
            gap = box[1] - _box(above)[3]
            line_height = max(4.0, min(box[3] - box[1], _box(above)[3] - _box(above)[1]))
            if (
                previous_text
                and gap <= line_height * 1.5
                and not previous_text.endswith((".", "。", "!", "?", "！", "？", ":", "：", ";", "；"))
                and not _looks_like_table_row(previous_text)
                and not _looks_like_table_row(current_text)
                and len(current_text) < 400
            ):
                return {
                    "question": "Is this block the continuation of the text block above it (one paragraph, split in two), or its own block?",
                    "allowed_relations": ["standalone", f"continuation_of_previous:{above.get('block_id')}"],
                    "context": {
                        "previous_block_id": str(above.get("block_id")),
                        "previous_text_tail": previous_text[-160:],
                        "current_text_head": current_text[:160],
                        "gap_px": round(gap, 1),
                        "line_height_px": round(line_height, 1),
                    },
                }
    return None


def build_semantic_tasks(page: dict[str, Any]) -> list[dict[str, Any]]:
    reviews = page.get("field_reviews", {}).get("blocks", {})
    evidence = evidence_index(page)
    blocks = page["current_candidate"]["blocks"]
    page_texts = [str(block.get("text", "")).strip() for block in blocks]
    is_toc_page = any(text in {"目录", "Contents", "CONTENTS"} for text in page_texts)
    unresolved = []
    for block_index, block in enumerate(blocks):
        fields = reviews.get(block["block_id"], {})
        title_possible = block.get("type") == "title" or "title" in block.get("type_candidates", [])
        title_recheck = title_possible and fields.get("level", {}).get("status") != "PASS"
        needs_type = block.get("type") == "unknown" or fields.get("type", {}).get("status") != "PASS" or title_recheck
        needs_level = title_possible and (needs_type or block.get("type") != "title" or fields.get("level", {}).get("status") != "PASS")
        needs_parent = block.get("type") in CAPTION_TYPES and fields.get("parent_id", {}).get("status") != "PASS"
        # The inventory questions come *before* the "everything passed" shortcut: a block whose fields
        # are all verified can still be a redundant fragment, and that is exactly the case the
        # annotations kept correcting by hand (296 blocks deleted in 99 pages).
        suspicion = _inventory_suspicion(blocks, block_index, page)
        needs_relation = suspicion is not None
        if not (needs_type or needs_level or needs_parent or needs_relation):
            continue
        refs = list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", [])))
        source_summary = [
            {
                "evidence_id": ref,
                "source": evidence[ref].get("model") or evidence[ref].get("kind"),
                "source_type": evidence[ref].get("source_type"),
                "type_candidate": evidence[ref].get("type_candidate"),
                "level_candidate": evidence[ref].get("level_candidate"),
                "text": str(evidence[ref].get("text", ""))[:300],
            }
            for ref in refs if ref in evidence
        ]
        allowed_types = sorted(
            (set(block.get("type_candidates", [])) | {block.get("type")}) & ALLOWED_TYPES
        )
        if block.get("type") == "footnote" and needs_type:
            allowed_types = sorted(set(allowed_types) | {"footnote", "text"})
        if "table" in allowed_types and not _has_verified_table_candidate(block, evidence):
            allowed_types.remove("table")
        if block.get("type") == "table" and "text" in allowed_types and not isinstance(block.get("text"), str):
            allowed_types.remove("text")
        type_method = fields.get("type", {}).get("method")
        if block.get("type_candidates") == ["title"] and type_method == "continued_question_sequence":
            allowed_types = ["title"]
        elif block.get("type") == "title" or "title" in allowed_types:
            allowed_types = sorted(set(allowed_types) | {"title", "text"})
        if is_toc_page and str(block.get("text", "")).strip() in {"目录", "Contents", "CONTENTS"}:
            allowed_types = ["title"]
            semantic_hint = "This is the table-of-contents page heading. Decide its level from document_root_candidates; do not assume level 1 and do not treat it as a replacement document root."
        else:
            semantic_hint = ""
        parent_ids = _parent_candidates(page, block)
        if suspicion and suspicion.get("context", {}).get("caption_of"):
            guest = suspicion["context"]
            # A short line that geometry puts against a figure is a caption candidate: offering
            # "title" as well let the model pick a heading for chart labels like "铝-铝合金" that
            # clearly belong to the chart beside them. Restrict the choice to caption or text.
            allowed_types = sorted({"text"} | {guest["caption_type"]} & (set(allowed_types) | {guest["caption_type"]}))
            allowed_types = sorted(set(allowed_types) | {guest["caption_type"]})
            parent_ids = sorted(set(parent_ids) | {guest["caption_of"]})
            # the model answers with type + parent_id, so the caption needs the parent field too
            if not needs_parent:
                needs_parent = True
            typography0 = (page.get("typography") or {}).get(block["block_id"]) or {}
            body0 = (page.get("typography_summary") or {}).get("body_font_size")
            ratio0 = (float(typography0.get("font_size") or 0) / float(body0)) if (typography0.get("font_size") and body0) else 1.0
            # a short label loses the "title" option only at body size: a larger-type label above a
            # chart is a section heading (周度产量和产能利用率), which the annotators kept as title
            if "title" in allowed_types and len(str(block.get("text", ""))) <= 24 and ratio0 < 1.15:
                allowed_types.remove("title")
        if block.get("type") == "unknown":
            allowed_types = sorted(set(allowed_types) | _caption_types(str(block.get("text", "")), parent_ids, page))
        if not allowed_types:
            allowed_types = ["text"]
        if block.get("geometric_heading_peer_evidence"):
            peer = block["geometric_heading_peer_evidence"]
            semantic_hint = (semantic_hint + " " if semantic_hint else "") + (
                "This short standalone block is geometrically parallel to multiple title candidates and is followed by expanded prose. "
                f"Peer block IDs: {peer.get('peer_block_ids', [])}; following block ID: {peer.get('following_block_id')}. "
                "Geometry only recalls the candidate; decide title/text and level from its semantic role in the supplied reading context and source votes."
            )
        if block.get("document_heading_sequence_evidence"): 
            sequence = block["document_heading_sequence_evidence"]
            semantic_hint = (semantic_hint + " " if semantic_hint else "") + (
                "This block belongs to an explicit cross-page heading sequence. Compare every supplied sequence item before deciding title/text and level: "
                + str(sequence.get("items", []))[:1800]
            )
        if block.get("cross_page_role_evidence"):
            role = block["cross_page_role_evidence"]
            semantic_hint = (semantic_hint + " " if semantic_hint else "") + (
                f"The current {role['candidate_role']} label has normalized cross-page repetition count "
                f"{role['repeat_count']} across {role['document_page_count']} pages and edge_position={role['at_expected_edge']}. "
                "Treat header/footer labels as page furniture only when page-edge geometry and cross-page repetition support that role; otherwise decide from body context."
            )
        # Typography is what separates a heading from body text, so it travels with the item: the model
        # gets the block's measured font size, its weight, its number of lines and the page's own body
        # size to compare against - evidence, not a guess.
        if suspicion:
            semantic_hint = (semantic_hint + " " if semantic_hint else "") + (
                "Inventory question (apply the annotation_policy above, which is what the human annotator did): " + suspicion["question"] + " Answer it in the \"relation\" field "
                f"(allowed: {suspicion['allowed_relations']}); use \"standalone\" when the block is genuinely its own."
            )
        # Clause and enumerated lines ("第一条 …", "1、…") at body size are list/clause text, not
        # headings: every annotated instance stayed text (15/15 clause, 14/16 enumerated). With the
        # measured font as the guard, the title option is withheld outright.
        _head_text = str(block.get("text") or "").lstrip()
        typography = (page.get("typography") or {}).get(block["block_id"]) or {}
        body_size = (page.get("typography_summary") or {}).get("body_font_size")
        font_ratio = None
        if typography.get("font_size") and body_size:
            font_ratio = round(float(typography["font_size"]) / float(body_size), 2)
        import re as _re
        _enumerated = bool(_re.match(r"^(第[一二三四五六七八九十百千\d]+条)", _head_text))
        if _enumerated:
            _body = (page.get("typography_summary") or {}).get("body_font_size")
            _ratio = (float(typography.get("font_size") or 0) / float(_body)) if (typography.get("font_size") and _body) else 1.0
            if _ratio < 1.15 and "title" in allowed_types and len(_head_text) <= 30:
                allowed_types = [t for t in allowed_types if t != "title"]
        if font_ratio is not None:
            semantic_hint = (semantic_hint + " " if semantic_hint else "") + (
                f"Measured font size is {typography['font_size']}px against a page body size of {body_size}px "
                f"(ratio {font_ratio}); bold={typography.get('bold')}, lines={typography.get('line_count')}, "
                f"width={typography.get('width')}px of {page['page_image']['width']}px. "
                "A ratio below 1.1 with several lines is body text however the label looks; a larger ratio with few lines supports a title."
            )
        continued_table = _continued_table_context(page, block, block_index)
        if continued_table:
            semantic_hint = (semantic_hint + " " if semantic_hint else "") + "The previous page ends with a table. Use the supplied cross-page context when deciding the local role; do not change a block to table without verified table structure."
        unresolved.append({
            "block_id": block["block_id"],
            "current_type": block.get("type"),
            "current_level": block.get("level"),
            "text": str(block.get("text", ""))[:500],
            "bbox_image_px": block.get("bbox_image_px"),
            "reading_order": block.get("reading_order"),
            "page_position": round(float(block.get("bbox_image_px", [0, 0, 0, 0])[1]) / page["page_image"]["height"], 4),
            "semantic_context": _semantic_context(page, blocks, block_index, source_summary),
            "allowed_types": allowed_types,
            "allowed_levels": list(range(1, MAX_TITLE_LEVEL + 1)) if "title" in allowed_types or block.get("type") == "title" else [],
            "allowed_parent_ids": parent_ids,
            "needs": [name for name, required in (("type", needs_type), ("level", needs_level), ("parent_id", needs_parent), ("relation", needs_relation)) if required],
            "relation_context": suspicion["context"] if suspicion else None,
            "allowed_relations": suspicion["allowed_relations"] if suspicion else [],
            "evidence": source_summary,
            "semantic_hint": semantic_hint,
            "continued_table_context": continued_table,
            "typography": typography or None,
            "body_font_size": body_size,
            "font_ratio": font_ratio,
            "document_sequence_review": bool(block.get("document_heading_sequence_evidence")),
        })
    tasks = []
    sequence_items = [item for item in unresolved if item.get("document_sequence_review")]
    regular_items = [item for item in unresolved if not item.get("document_sequence_review")]
    batches = [pool[offset : offset + MAX_SEMANTIC_ITEMS] for pool in (sequence_items, regular_items) for offset in range(0, len(pool), MAX_SEMANTIC_ITEMS)]
    for batch_index, items in enumerate(batches, start=1):
        task_id = f"{page['sample_id']}.p{page['page']}.semantic.{batch_index}"
        tasks.append({
            # The ground-truth policy the annotators apply (learned from 99 annotated pages). Without it
            # the model answers "standalone" to every relation question: it judges the OCR fragments as
            # separate blocks, while the target annotation groups them into semantic units.
            "annotation_policy": {
                "text": "One block per semantic unit. Consecutive lines in the same column that continue the same sentence/paragraph belong to ONE block; a numbered reference list is ONE block; a short standalone line (label, single heading) stays its own block. Form/field lines that end with a colon or run of underscores (合同编号：____, 名称：____) each stay their OWN block - a form is a list of fields, not a paragraph.",
                "images": "Only real, standalone figures are blocks. Graphics inside a table, decorations and fragments already covered by another figure/text are not blocks.",
                "furniture": "Headers, footers and page numbers are always kept as their own blocks and are never merged into body text.",
                "captions": "A caption is bound to the figure/table it describes through parent_id.",
                "types": "A heading is only a title when the typography supports it (bigger than body text, or a numbering pattern); otherwise it is body text.",
            },
            "page_typography": page.get("typography_summary") or {},
            "task_version": "ocr-agent-semantic-task.v5",
            "context_version": "target-centered.v1",
            "task_id": task_id,
            "sample_id": page["sample_id"],
            "page": page["page"],
            "heading_context_complete": bool(page.get("document_context", {}).get("previous_pages_complete", False)),
            "document_outline": _document_outline(page),
            "type_rules": {
                name: TYPE_RULES[name]
                for name in sorted({value for item in items for value in item["allowed_types"]})
                if name in TYPE_RULES
            },
            "items": items,
        })
    return tasks


def _has_verified_table_candidate(block: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> bool:
    """A block may only be decided as table when it already carries table HTML.

    The schema requires table blocks to expose HTML, so a candidate whose HTML could not be built must
    not be offered the table type (it would produce an invalid revision).
    """
    del evidence  # kept for signature compatibility with the caller
    return isinstance(block.get("html"), str) and "<table" in block["html"].lower()


def _document_outline(page: dict[str, Any]) -> str:
    for item in page.get("evidence", {}).get("anydoc", []):
        if item.get("kind") == "document_outline":
            return str(item.get("text", ""))[:5000]
    return ""


def build_table_tasks(page: dict[str, Any]) -> list[dict[str, Any]]:
    reviews = page.get("field_reviews", {}).get("blocks", {})
    evidence = evidence_index(page)
    tasks = []
    for block in page["current_candidate"]["blocks"]:
        if block.get("type") != "table" or reviews.get(block["block_id"], {}).get("html", {}).get("status") == "PASS":
            continue
        candidates = []
        for ref in block.get("support_refs", []):
            item = evidence.get(ref, {})
            if isinstance(item.get("html"), str) and "<table" in item["html"].lower():
                candidates.append({"evidence_id": ref, "source": item.get("model") or item.get("kind"), "html": item["html"]})
        tasks.append({
            "task_version": "ocr-agent-table-task.v1",
            "task_id": f"{page['sample_id']}.p{page['page']}.{block['block_id']}.table",
            "sample_id": page["sample_id"],
            "page": page["page"],
            "block_id": block["block_id"],
            "bbox_image_px": block["bbox_image_px"],
            "candidates": candidates,
            "allowed_decisions": [candidate["evidence_id"] for candidate in candidates] + ["needs_reconstruction", "CHECK"],
        })
    return tasks


def _neighbour_blocks(blocks: list[dict[str, Any]], index: int, radius: int = 2) -> list[dict[str, Any]]:
    start, end = max(0, index - radius), min(len(blocks), index + radius + 1)
    return [
        {
            "block_id": block["block_id"],
            "relative": position - index,
            "type": block.get("type"),
            "level": block.get("level"),
            "text": str(block.get("text", ""))[:240],
            "reading_order": block.get("reading_order"),
        }
        for position, block in enumerate(blocks[start:end], start=start)
        if position != index
    ]


def promote_continued_question_candidates(page: dict[str, Any], previous_page_tail: list[dict[str, Any]]) -> int:
    previous_numbers = {
        number
        for item in previous_page_tail
        if item.get("type") == "title" or item.get("type_candidates") == ["title"]
        for number in [_question_number(str(item.get("text", "")))]
        if number is not None
    }
    previous_numbers.update(
        number
        for block in page.get("current_candidate", {}).get("blocks", [])
        if block.get("type") == "title"
        for number in [_question_number(str(block.get("text", "")))]
        if number is not None
    )
    if not previous_numbers:
        return 0
    promoted = 0
    for block in page.get("current_candidate", {}).get("blocks", []):
        text = str(block.get("text", "")).strip()
        number = _question_number(text)
        if block.get("type") not in {"text", "unknown"} or number is None or number not in {value + 1 for value in previous_numbers} or not text.endswith(("?", "？")):
            continue
        block["type"] = "title"
        block["type_candidates"] = ["title"]
        block.setdefault("level", 3)
        fields = page.setdefault("field_reviews", {}).setdefault("blocks", {}).setdefault(block["block_id"], {})
        fields["type"] = {"status": "PASS", "method": "continued_question_sequence", "evidence_refs": block.get("support_refs", [])}
        fields["level"] = {"status": "CHECK", "method": "continued_question_level_candidate", "evidence_refs": block.get("support_refs", [])}
        previous_numbers.add(number)
        promoted += 1
    return promoted


def _question_number(text: str) -> int | None:
    match = re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩]", text)
    if match:
        return "①②③④⑤⑥⑦⑧⑨⑩".index(match.group(0)) + 1
    match = re.match(r"^[（(](\d+)[）)]", text)
    return int(match.group(1)) if match else None


def build_document_relation_tasks(page: dict[str, Any]) -> list[dict[str, Any]]:
    context = page.get("document_context", {})
    previous_tables = [item for item in context.get("previous_page_tail", []) if item.get("type") == "table" and not item.get("repeated_page_header")]
    blocks = page.get("current_candidate", {}).get("blocks", [])
    if not previous_tables or not context.get("previous_pages_complete") or not blocks:
        return []
    height = page["page_image"]["height"]
    top_blocks = [block for block in blocks[:8] if block.get("bbox_image_px", [0, height])[1] <= height * 0.45 and not block.get("repeated_page_header")]
    if not top_blocks:
        return []
    drawings = [
        {"evidence_id": item.get("evidence_id"), "bbox_image_px": item.get("bbox_image_px"), "item_count": item.get("item_count"), "closed": item.get("closed")}
        for item in page.get("evidence", {}).get("pdf", [])
        if item.get("kind") == "pdf_drawing" and item.get("bbox_image_px", [0, height])[1] <= height * 0.90
    ]
    return [{
        "task_version": "ocr-agent-document-relation-task.v1",
        "task_id": f"{page['sample_id']}.p{page['page']}.document_relation.1",
        "sample_id": page["sample_id"],
        "page": page["page"],
        "allowed_relations": ["continuation", "new_section", "uncertain"],
        "previous_tables": previous_tables,
        "current_top_blocks": [
            {
                "block_id": block["block_id"],
                "type": block.get("type"),
                "allowed_types": sorted(({block.get("type"), "text"} & ALLOWED_TYPES)),
                "text": str(block.get("text", ""))[:700],
                "bbox_image_px": block.get("bbox_image_px"),
                "source_refs": block.get("source_refs", []),
                "support_refs": block.get("support_refs", []),
                "word_table_ref": block.get("word_table_ref"),
                "word_row_range": block.get("word_row_range"),
            }
            for block in top_blocks
        ],
        "current_pdf_drawings": drawings,
        "instruction": "Decide whether the current page begins by continuing the previous table's semantic content. A page break may remove visible grid lines. Use textual topic continuity, unfinished cells/sections, repeated headers, numbering, and geometry. Do not infer continuation only because both pages contain tables.",
    }]


def _continued_table_context(page: dict[str, Any], block: dict[str, Any], index: int) -> dict[str, Any] | None:
    context = page.get("document_context", {})
    previous = context.get("previous_page_tail", [])
    previous_tables = [item for item in previous if item.get("type") == "table"]
    if not previous_tables:
        return None
    box = block.get("bbox_image_px") or [0, 0, 0, 0]
    height = page["page_image"]["height"]
    if index > 5 or box[1] > height * 0.30:
        return None
    return {
        "previous_table_ids": [item.get("block_id") for item in previous_tables],
        "previous_table_text": [str(item.get("text", ""))[:240] for item in previous_tables],
        "previous_table_content": [content for item in previous_tables for content in item.get("table_content", [])][:2],
        "signal": "previous_page_table_plus_top_region",
    }


def _caption_types(text: str, parent_ids: list[str], page: dict[str, Any]) -> set[str]:
    if not parent_ids:
        return set()
    label = re.sub(r"\s+", "", text).lower()
    if not re.match(r"^(?:表|table|图|figure|fig\.?)[\d一二三四五六七八九十ivxlcdm]+", label):
        return set()
    media = {block["block_id"]: block.get("type") for block in page["current_candidate"]["blocks"]}
    parent_types = {media.get(block_id) for block_id in parent_ids}
    return ({"table_caption"} if "table" in parent_types else set()) | ({"image_caption"} if parent_types & {"image", "chart"} else set())


def _parent_candidates(page: dict[str, Any], caption: dict[str, Any]) -> list[str]:
    expected = {"table"} if caption.get("type") == "table_caption" else {"image", "chart"}
    if caption.get("type") == "unknown":
        expected = {"table", "image", "chart"}
    result = []
    cb = caption.get("bbox_image_px", [0, 0, 0, 0])
    for block in page["current_candidate"]["blocks"]:
        if block.get("type") not in expected:
            continue
        box = block["bbox_image_px"]
        vertical_gap = max(0.0, box[1] - cb[3], cb[1] - box[3])
        if vertical_gap <= page["page_image"]["height"] * 0.08:
            result.append(block["block_id"])
    return result
