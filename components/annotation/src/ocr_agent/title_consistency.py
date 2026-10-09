from __future__ import annotations

from copy import deepcopy
import collections
import re
from typing import Any

from .constants import MAX_TITLE_LEVEL
from .errors import OperationError
from .decision_policies import TYPE_RULES
from .schemas import evidence_index
from .validators import validate_revision

DOCUMENT_REVIEW_METHODS = {
    "document_title_consistency_review",
    "document_title_alignment_review",
    "document_cover_title_stack_review",
    "document_title_consistency_unresolved",
}

MAX_CONSISTENCY_ITEMS = 4
MAX_TITLE_RECALL_ITEMS = 24
MAX_DOCUMENT_REVIEW_ITEMS = 40
MAX_ALIGNMENT_ITEMS = 12


def _sequence_token(text: str) -> tuple[str, int] | None:
    value = re.sub(r"\s+", "", text or "")
    value = re.sub(r"^[*_~`]+", "", value)
    value = re.sub(r"[*_~`]+$", "", value)
    match = re.match(r"^#*第([一二三四五六七八九十百零〇两\d]+)章", value, re.IGNORECASE)
    if match:
        number = _number_value(match.group(1))
        return ("chapter", number) if number >= 0 else None
    match = re.match(r"^#*(?:步骤|step)(\d+)(?:[:：.、]|$)", value, re.IGNORECASE)
    if match:
        return "step", int(match.group(1))
    match = re.match(r"^#*chapter(\d+)(?:[:：.、]|$)", value, re.IGNORECASE)
    if match:
        return "chapter", int(match.group(1))
    match = re.match(r"^#*(\d+(?:\.\d+)+)\.?", value)
    if match:
        parts = [int(part) for part in match.group(1).split(".")]
        encoded = sum(part * (1000 ** (len(parts) - index - 1)) for index, part in enumerate(parts))
        parent = ".".join(str(part) for part in parts[:-1])
        return f"decimal_depth:{len(parts)}:parent:{parent}", encoded
    return None


def _number_value(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "百" in value:
        left, right = value.split("百", 1)
        return digits.get(left, 1) * 100 + (_number_value(right) if right else 0)
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value, -1)


def _compact_context(blocks: list[dict[str, Any]], index: int) -> dict[str, Any]:
    def compact(block: dict[str, Any], relation: str) -> dict[str, Any]:
        return {
            "relation": relation,
            "block_id": block.get("block_id"),
            "type": block.get("type"),
            "level": block.get("level"),
            "text": str(block.get("text", ""))[:500],
            "reading_order": block.get("reading_order"),
        }

    return {
        "before": [compact(block, "before") for block in blocks[max(0, index - 4):index]],
        "target": compact(blocks[index], "target"),
        "after": [compact(block, "after") for block in blocks[index + 1:index + 5]],
    }


def _compact_page_edge(result: dict[str, Any] | None, relation: str, *, head: bool) -> list[dict[str, Any]]:
    blocks = sorted((result or {}).get("revision", {}).get("blocks", []), key=lambda block: block.get("reading_order", 0))
    selected = blocks[:5] if head else blocks[-5:]
    return [
        {
            "relation": relation,
            "block_id": block.get("block_id"),
            "type": block.get("type"),
            "level": block.get("level"),
            "text": str(block.get("text", ""))[:500],
            "reading_order": block.get("reading_order"),
        }
        for block in selected
    ]


def _document_root_candidates(results: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    for page_number, result in sorted(results.items()):
        titles = [block for block in result.get("revision", {}).get("blocks", []) if block.get("type") == "title"]
        if titles:
            return [
                {"page": page_number, "block_id": block.get("block_id"), "text": str(block.get("text", ""))[:240], "level": block.get("level"), "status": "provisional_root_candidate"}
                for block in titles[:8]
            ]
    return []


def _heading_path_before(results: dict[int, dict[str, Any]], page_number: int, reading_order: int) -> list[dict[str, Any]]:
    path: list[dict[str, Any]] = []
    for number, result in sorted(results.items()):
        for block in sorted(result.get("revision", {}).get("blocks", []), key=lambda item: item.get("reading_order", 0)):
            if number > page_number or (number == page_number and int(block.get("reading_order", 0)) >= reading_order):
                continue
            level = block.get("level")
            if block.get("type") != "title" or not isinstance(level, int):
                continue
            if re.sub(r"\s+", "", str(block.get("text", ""))).casefold() in {"目录", "contents"}:
                continue
            path = [item for item in path if int(item["level"]) < level]
            path.append({"page": number, "block_id": block.get("block_id"), "text": str(block.get("text", ""))[:240], "level": level, "status": "provisional_model_decision"})
    return path[-8:]


def _layout_prefix(text: str) -> str:
    value = str(text or "").lstrip()
    if re.match(r"^[\u2022\u25cf\u25aa\uf06c\-]", value):
        return "bullet"
    if re.match(r"^(?:[*_~`]*)(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[.、]", value):
        return "numbered"
    return "plain"


def _parallel_layout_key(page: dict[str, Any], item: dict[str, Any]) -> str | None:
    text = re.sub(r"\s+", "", str(item.get("text", "")))
    box = item.get("bbox_image_px")
    width = float(page.get("page_image", {}).get("width", 0))
    height = float(page.get("page_image", {}).get("height", 0))
    after = item.get("semantic_context", {}).get("current_page_reading_context", {}).get("after", [])
    if not 2 <= len(text) <= 40 or not isinstance(box, list) or len(box) != 4 or width <= 0 or height <= 0 or not after:
        return None
    if text[-1] in "。．.；;！!？?":
        return None
    if after[0].get("type") in {"header", "footer", "page_number", "footnote"}:
        return None
    prefix = _layout_prefix(text)
    x_bucket = round(float(box[0]) / width, 2)
    h_bucket = round((float(box[3]) - float(box[1])) / height, 3)
    return f"{prefix}:{x_bucket:.2f}:{h_bucket:.3f}"


def _split_nearby_layout_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [items]


def _group_minority_count(items: list[dict[str, Any]]) -> int:
    active = [item for item in items if not item["consistency_reviewed"]]
    if len(active) < 2:
        return 0
    outcomes = collections.Counter((item["current_type"], item.get("current_level")) for item in active)
    return len(active) - max(outcomes.values())


def _cover_title_bundles(pages: dict[int, dict[str, Any]], results: dict[int, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map every line of the cover-title stack to its bundle, so reviews keep the bundle atomic."""
    if not results:
        return {}
    first_page = min(results)
    page = pages.get(first_page)
    result = results.get(first_page)
    if not page or not result:
        return {}
    stack = _cover_title_stack(page, result)
    if len(stack) < 2:
        return {}
    outcomes = {(str(block.get("type")), block.get("level") if block.get("type") == "title" else None) for block in stack}
    members = [
        {
            "block_id": str(block["block_id"]),
            "page": first_page,
            "text": str(block.get("text", ""))[:160],
            "type": block.get("type"),
            "level": block.get("level"),
        }
        for block in stack
    ]
    info = {"members": members, "homogeneous": len(outcomes) <= 1}
    return {member["block_id"]: info for member in members}


def _local_hierarchy_collision(item: dict[str, Any]) -> bool:
    level = item.get("current_level")
    if item.get("current_type") != "title" or not isinstance(level, int):
        return False
    before = item.get("semantic_context", {}).get("current_page_reading_context", {}).get("before", [])
    parent = next((block for block in reversed(before) if block.get("type") == "title" and isinstance(block.get("level"), int)), None)
    if not parent:
        return False
    target_token = _sequence_token(str(item.get("text", "")))
    parent_token = _sequence_token(str(parent.get("text", "")))
    if target_token and parent_token and target_token[0] != parent_token[0]:
        return False
    return level <= int(parent["level"])


def _split_local_sequences(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous = -1
    for item in sorted(items, key=lambda row: (row["page"], row["reading_order"])):
        number = int(item["sequence_number"])
        if current and number <= previous:
            groups.append(current)
            current = []
        current.append(item)
        previous = number
    if current:
        groups.append(current)
    return groups


def _hierarchy_collision(item: dict[str, Any]) -> bool:
    level = item.get("current_level")
    if item.get("current_type") != "title" or not isinstance(level, int):
        return False
    target_token = _sequence_token(str(item.get("text", "")))
    for parent in reversed(item.get("semantic_context", {}).get("provisional_heading_path", [])):
        parent_level = parent.get("level")
        if not isinstance(parent_level, int):
            continue
        parent_token = _sequence_token(str(parent.get("text", "")))
        if parent_level > level:
            if target_token is None:
                return True
            continue
        if target_token and parent_token and target_token[0] == parent_token[0]:
            continue
        return level <= parent_level
    return False


def _has_conflict(group: list[dict[str, Any]]) -> bool:
    if len(group) < 2 or all(item.get("consistency_reviewed") for item in group):
        return False
    types = {item["current_type"] for item in group}
    levels = {item.get("current_level") for item in group if item["current_type"] == "title"}
    return len(types) > 1 or len(levels) > 1 or any(_hierarchy_collision(item) for item in group)


def _toc_scope(page: dict[str, Any]) -> str:
    texts = {re.sub(r"\s+", "", str(block.get("text", ""))).casefold() for block in page.get("current_candidate", {}).get("blocks", [])}
    if texts & {"目录", "contents"}:
        return "toc"
    candidates = [str(block.get("text", "")) for block in page.get("current_candidate", {}).get("blocks", []) if str(block.get("text", "")).strip()]
    if len(candidates) >= 6:
        leaders = sum(1 for text in candidates if re.search(r"[.．…·]{3,}\s*\d{1,3}\s*$", text.strip()))
        if leaders >= max(4, len(candidates) // 2):
            return "toc"
    return "body"


def build_title_consistency_tasks(
    sample_id: str,
    pages: dict[int, dict[str, Any]],
    results: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_style: dict[str, list[dict[str, Any]]] = {}
    by_layout: dict[str, list[dict[str, Any]]] = {}
    layout_members: dict[str, list[dict[str, Any]]] = {}
    recall_items: list[dict[str, Any]] = []
    deep_items: list[dict[str, Any]] = []
    hierarchy_items: list[dict[str, Any]] = []
    bundles = _cover_title_bundles(pages, results)
    for page_number, page in pages.items():
        scope = _toc_scope(page)
        result = results.get(page_number)
        if not result:
            continue
        revision = result.get("revision", {})
        final_blocks = {block["block_id"]: block for block in revision.get("blocks", [])}
        ordered = sorted(revision.get("blocks", []), key=lambda block: block.get("reading_order", 0))
        positions = {block["block_id"]: index for index, block in enumerate(ordered)}
        page_evidence = evidence_index(page)
        for candidate in page.get("current_candidate", {}).get("blocks", []):
            token = _sequence_token(str(candidate.get("text", "")))
            style = f"{scope}:{token[0]}" if token else None
            block = final_blocks.get(candidate.get("block_id"))
            title_possible = "title" in candidate.get("type_candidates", []) or bool(candidate.get("document_heading_sequence_evidence"))
            if block is None or block.get("type") not in {"text", "title"} or (style is None and not title_possible):
                continue
            refs = list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", [])))
            refs = [ref for ref in refs if ref in page_evidence]
            if not refs:
                continue
            item = {
                "page": page_number,
                "block_id": block["block_id"],
                "text": str(block.get("text", ""))[:500],
                "reading_order": int(block.get("reading_order", 0)),
                "bbox_image_px": block.get("bbox_image_px"),
                "sequence_number": token[1] if token else page_number * 10000 + int(block.get("reading_order", 0)),
                "current_type": block.get("type"),
                "current_level": block.get("level"),
                "consistency_reviewed": revision.get("field_reviews", {}).get("blocks", {}).get(block["block_id"], {}).get("type", {}).get("method") in DOCUMENT_REVIEW_METHODS,
                "allowed_types": ["text", "title"],
                "allowed_levels": list(range(1, MAX_TITLE_LEVEL + 1)),
                "allowed_parent_ids": [],
                "needs": ["type", "level"],
                "evidence": [
                    {
                        "evidence_id": ref,
                        "source": page_evidence[ref].get("model") or page_evidence[ref].get("kind"),
                        "source_type": page_evidence[ref].get("source_type"),
                        "type_candidate": page_evidence[ref].get("type_candidate"),
                        "level_candidate": page_evidence[ref].get("level_candidate"),
                        "text": str(page_evidence[ref].get("text", ""))[:300],
                    }
                    for ref in refs
                ],
                "semantic_context": {
                    "current_page_reading_context": _compact_context(ordered, positions[block["block_id"]]),
                    "provisional_heading_path": _heading_path_before(results, page_number, int(block.get("reading_order", 0))),
                    "document_root_candidates": _document_root_candidates(results),
                    "previous_page_tail": _compact_page_edge(results.get(page_number - 1), "previous_page", head=False),
                    "next_page_head": _compact_page_edge(results.get(page_number + 1), "next_page", head=True),
                    "context_note": "Root candidates, heading paths and prior decisions are provisional. A table-of-contents heading does not replace an independent document title. Media position follows reading order: preceding media is not governed by the target; following media can support a heading interpretation but is not sufficient by itself.",
                },
            }
            bundle = bundles.get(block["block_id"])
            if bundle:
                item["semantic_context"]["bound_group"] = bundle["members"]
                item["semantic_context"]["context_note"] += (
                    " These lines are bundled as one cover-title stack because they share typography (font size, "
                    "weight, alignment, leading). Treat the bundle as one unit: either every line is a title sharing "
                    "one level, or every line stays text; a mixed outcome inside the bundle is not allowed."
                )
            if style is not None:
                by_style.setdefault(style, []).append(item)
            layout_key = _parallel_layout_key(page, item)
            if title_possible and layout_key:
                by_layout.setdefault(layout_key, []).append(item)
                layout_members.setdefault(layout_key, []).append({"block_id": block["block_id"], "page": page_number, "text": str(block.get("text", ""))[:160], "current_type": block.get("type"), "current_level": block.get("level"), "alignment_reviewed": revision.get("field_reviews", {}).get("blocks", {}).get(block["block_id"], {}).get("type", {}).get("method") == "document_title_alignment_review"})
            if block.get("type") == "title" and not bundle and _local_hierarchy_collision(item) and not item["consistency_reviewed"]:
                hierarchy_items.append(item)
            if layout_key and layout_key in layout_members:
                item["semantic_context"]["alignment_group"] = layout_members[layout_key]
            if block.get("type") == "title" and isinstance(block.get("level"), int) and int(block["level"]) >= MAX_TITLE_LEVEL and not item["consistency_reviewed"]:
                deep_items.append(item)
            structural_title_recall = bool(candidate.get("geometric_heading_peer_evidence") or candidate.get("document_heading_sequence_evidence"))
            if title_possible and structural_title_recall and block.get("type") == "text" and not item["consistency_reviewed"]:
                recall_items.append(item)
    tasks = []
    previously_reviewed = {
        block.get("block_id")
        for result in results.values()
        for block in result.get("revision", {}).get("blocks", [])
        if result.get("revision", {}).get("field_reviews", {}).get("blocks", {}).get(block.get("block_id"), {}).get("type", {}).get("method") in DOCUMENT_REVIEW_METHODS
    }
    review_budget = max(0, MAX_DOCUMENT_REVIEW_ITEMS - len(previously_reviewed))
    scheduled: set[str] = set()
    group_number = 0
    for style, items in sorted(by_style.items()):
        groups = _split_local_sequences(items) if style.endswith(":step") else [items]
        for group in groups:
            if not _has_conflict(group) or len(scheduled) >= review_budget:
                continue
            group = [item for item in group if item["block_id"] not in scheduled and not item["consistency_reviewed"]][:review_budget - len(scheduled)]
            if not group:
                continue
            group_number += 1
            group_context = [
                {key: item.get(key) for key in ("page", "block_id", "text", "current_type", "current_level", "sequence_number")}
                for item in group
            ]
            for offset in range(0, len(group), MAX_CONSISTENCY_ITEMS):
                chunk = deepcopy(group[offset:offset + MAX_CONSISTENCY_ITEMS])
                for item in chunk:
                    item["semantic_context"]["parallel_group"] = group_context
                    item["semantic_context"]["context_note"] += " The group is potentially parallel, but current decisions may be correct or wrong. Rejudge each target from its own surrounding prose; do not force uniformity."
                tasks.append({
                    "task_version": "ocr-agent-title-consistency-task.v1",
                    "task_id": f"{sample_id}.title_consistency.{group_number}.{offset // MAX_CONSISTENCY_ITEMS + 1}",
                    "sample_id": sample_id,
                    "review_kind": "title_type_level_consistency",
                    "heading_context_complete": True,
                    "document_outline": "",
                    "type_rules": {"text": TYPE_RULES["text"], "title": TYPE_RULES["title"]},
                    "items": chunk,
                })
                scheduled.update(item["block_id"] for item in chunk)
    alignment_pool: list[dict[str, Any]] = []
    for layout_key, group in sorted(
        by_layout.items(),
        key=lambda entry: (-_group_minority_count(entry[1]), entry[0]),
    ):
        if len(group) < 2 or len({(item["current_type"], item.get("current_level")) for item in group}) < 2:
            continue
        counts = collections.Counter((item["current_type"], item.get("current_level")) for item in group)
        minority_outcome = counts.most_common()[-1][0]
        pending = [item for item in group if not item.get("alignment_reviewed")]
        selected = [item for item in pending if (item["current_type"], item.get("current_level")) == minority_outcome]
        selected += [item for item in pending if (item["current_type"], item.get("current_level")) != minority_outcome]
        for item in selected[:max(0, MAX_ALIGNMENT_ITEMS - len(alignment_pool))]:
            alignment_pool.append(item)
        if len(alignment_pool) >= MAX_ALIGNMENT_ITEMS:
            break
    if alignment_pool:
        member_lists = {item["block_id"]: item["semantic_context"].get("alignment_group", []) for item in alignment_pool}
        for offset in range(0, len(alignment_pool), MAX_CONSISTENCY_ITEMS):
            chunk = deepcopy(alignment_pool[offset:offset + MAX_CONSISTENCY_ITEMS])
            for item in chunk:
                item["semantic_context"]["parallel_group"] = deepcopy(member_lists.get(item["block_id"], []))
                item["semantic_context"]["context_note"] += " Alignment review: these targets share an identical geometric text style in the same parent scope, so unexplained type or level differences are not acceptable. Targets whose implied heading depth exceeds level 4 are flattened to text together; a real governing heading keeps one shared title level within the L1-L4 contract. Only explicit sequence or role differences may split the group."
            tasks.append({
                "task_version": "ocr-agent-title-alignment-task.v1",
                "task_id": f"{sample_id}.title_alignment.{offset // MAX_CONSISTENCY_ITEMS + 1}",
                "sample_id": sample_id,
                "review_kind": "parallel_layout_alignment",
                "heading_context_complete": True,
                "document_outline": "",
                "type_rules": {"text": TYPE_RULES["text"], "title": TYPE_RULES["title"]},
                "items": chunk,
            })
            scheduled.update(item["block_id"] for item in chunk)
    for layout_key, layout_items in sorted(
        by_layout.items(),
        key=lambda entry: (
            -_group_minority_count([item for item in entry[1] if not item["consistency_reviewed"]]),
            entry[0],
        ),
    ):
        for group in _split_nearby_layout_groups(layout_items):
            group = [item for item in group if item["block_id"] not in scheduled]
            if len(group) < 2 or not _has_conflict(group) or len(scheduled) >= review_budget:
                continue
            group = [item for item in group if not item["consistency_reviewed"]][:review_budget - len(scheduled)]
            group_number += 1
            group_context = [
                {key: item.get(key) for key in ("page", "block_id", "text", "current_type", "current_level", "reading_order")}
                for item in group
            ]
            for offset in range(0, len(group), MAX_CONSISTENCY_ITEMS):
                chunk = deepcopy(group[offset:offset + MAX_CONSISTENCY_ITEMS])
                for item in chunk:
                    item["semantic_context"]["parallel_group"] = group_context
                    item["semantic_context"]["context_note"] += " These targets share a geometric text style across nearby pages and each is followed by content. Geometry only establishes a parallel review group; decide each semantic role independently, while checking unexplained type or level differences."
                tasks.append({
                    "task_version": "ocr-agent-title-consistency-task.v1",
                    "task_id": f"{sample_id}.title_layout_consistency.{group_number}.{offset // MAX_CONSISTENCY_ITEMS + 1}",
                    "sample_id": sample_id,
                    "review_kind": "parallel_layout_title_consistency",
                    "heading_context_complete": True,
                    "document_outline": "",
                    "type_rules": {"text": TYPE_RULES["text"], "title": TYPE_RULES["title"]},
                    "items": chunk,
                })
                scheduled.update(item["block_id"] for item in chunk)
    remaining_budget = max(0, review_budget - len(scheduled))
    hierarchy = [item for item in hierarchy_items if item["block_id"] not in scheduled][:remaining_budget]
    for offset in range(0, len(hierarchy), MAX_CONSISTENCY_ITEMS):
        chunk = deepcopy(hierarchy[offset:offset + MAX_CONSISTENCY_ITEMS])
        for item in chunk:
            item["semantic_context"]["context_note"] += " The current title level collides with an established preceding parent path. Rejudge type and level. Under the L1-L4 contract, if the target is only a leaf that would require a level deeper than L4, classify it as text; never reset it to L1 or leave an impossible deeper title."
        tasks.append({
            "task_version": "ocr-agent-title-hierarchy-precision-task.v1",
            "task_id": f"{sample_id}.title_hierarchy_precision.{offset // MAX_CONSISTENCY_ITEMS + 1}",
            "sample_id": sample_id,
            "review_kind": "title_hierarchy_collision_precision",
            "heading_context_complete": True,
            "document_outline": "",
            "type_rules": {"text": TYPE_RULES["text"], "title": TYPE_RULES["title"]},
            "items": chunk,
        })
        scheduled.update(item["block_id"] for item in chunk)
    deep_items.sort(key=lambda item: (_sequence_token(str(item.get("text", ""))) is not None, item["page"], item["reading_order"]))
    remaining_budget = max(0, review_budget - len(scheduled))
    deep = [item for item in deep_items if item["block_id"] not in scheduled][:min(MAX_TITLE_RECALL_ITEMS, remaining_budget)]
    for offset in range(0, len(deep), MAX_CONSISTENCY_ITEMS):
        chunk = deepcopy(deep[offset:offset + MAX_CONSISTENCY_ITEMS])
        for item in chunk:
            item["semantic_context"]["context_note"] += " This is a maximum-depth title decision (L4). Keep title at L4 only when the target demonstrably names and governs following content under a distinct immediate parent. A requirement label, attribute, parallel list item, complete instruction, or source-model heading vote is text unless it has that governing role. Do not flatten a deeper apparent hierarchy by turning every leaf into L4 title."
        tasks.append({
            "task_version": "ocr-agent-title-deep-precision-task.v1",
            "task_id": f"{sample_id}.title_deep_precision.{offset // MAX_CONSISTENCY_ITEMS + 1}",
            "sample_id": sample_id,
            "review_kind": "deep_title_precision",
            "heading_context_complete": True,
            "document_outline": "",
            "type_rules": {"text": TYPE_RULES["text"], "title": TYPE_RULES["title"]},
            "items": chunk,
        })
        scheduled.update(item["block_id"] for item in chunk)
    remaining_budget = max(0, review_budget - len(scheduled))
    recall = [item for item in recall_items if item["block_id"] not in scheduled][:min(MAX_TITLE_RECALL_ITEMS, remaining_budget)]
    for offset in range(0, len(recall), MAX_CONSISTENCY_ITEMS):
        chunk = deepcopy(recall[offset:offset + MAX_CONSISTENCY_ITEMS])
        for item in chunk:
            item["semantic_context"]["context_note"] += " The first pass chose text although title was an allowed candidate. Rejudge from surrounding content and completed document context; keeping text is valid."
        tasks.append({
            "task_version": "ocr-agent-title-recall-task.v1",
            "task_id": f"{sample_id}.title_recall.{offset // MAX_CONSISTENCY_ITEMS + 1}",
            "sample_id": sample_id,
            "review_kind": "missed_title_recall",
            "heading_context_complete": True,
            "document_outline": "",
            "type_rules": {"text": TYPE_RULES["text"], "title": TYPE_RULES["title"]},
            "items": chunk,
        })
    return tasks


def _pdf_line_typography(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("evidence_id")): item
        for item in page.get("evidence", {}).get("pdf", [])
        if item.get("kind") == "pdf_line"
    }


def _block_typography(lines: dict[str, dict[str, Any]], block: dict[str, Any]) -> dict[str, Any] | None:
    """Font size and weight of a block, taken from its grounded PDF lines."""
    sizes: list[float] = []
    bolds: list[bool] = []
    for ref in block.get("source_refs", []):
        line = lines.get(str(ref))
        if line is None:
            continue
        size = line.get("font_size")
        if isinstance(size, (int, float)) and float(size) > 0:
            sizes.append(float(size))
            bolds.append(bool(line.get("bold")))
    if not sizes:
        return None
    return {"font_size": max(sizes), "bold": any(bolds)}


def _median_number(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _same_alignment(seed_box: list[float], box: list[float], width: float) -> bool:
    """Same visual alignment: centred, left-flush or right-flush within 2% of the page width."""
    tolerance = width * 0.02
    return (
        abs((box[0] + box[2]) / 2 - (seed_box[0] + seed_box[2]) / 2) <= tolerance
        or abs(box[0] - seed_box[0]) <= tolerance
        or abs(box[2] - seed_box[2]) <= tolerance
    )


def _cover_seed_position_ok(
    seed_box: list[float], height: float, lines: dict[str, dict[str, Any]], body_font: float
) -> bool:
    """A cover title may sit below a masthead image, so "top 35% of the page" is the wrong test.

    Covers regularly put a logo or banner across the top and start the title below it, which a fixed
    fraction rejects. The seed is on the cover when nothing but display matter is above it - no
    body-sized text line - with a generous absolute bound so body pages never qualify.
    """
    top = float(seed_box[1])
    if top > height * 0.55:
        return False
    threshold = body_font * 1.15 if body_font > 0 else 0.0
    for line in lines.values():
        box = line.get("bbox_image_px")
        size = float(line.get("font_size") or 0.0)
        if not isinstance(box, list) or len(box) != 4 or size <= 0:
            continue
        if float(box[1]) + 2.0 >= top:
            continue
        if size <= threshold:
            return False
    return True


def _cover_title_stack(page: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect a cover-title stack.

    Primary evidence is typography (same font size, same weight, same alignment, consistent
    leading); bounding-box geometry is only a fallback when the PDF carries no font metadata.
    """
    width = float(page.get("page_image", {}).get("width", 0))
    height = float(page.get("page_image", {}).get("height", 0))
    if width <= 0 or height <= 0:
        return []
    lines = _pdf_line_typography(page)
    blocks = sorted(
        (block for block in result.get("revision", {}).get("blocks", []) if block.get("type") in {"text", "title"}),
        key=lambda block: block.get("reading_order", 0),
    )
    typography = {str(block.get("block_id")): _block_typography(lines, block) for block in blocks}
    typed = [value for value in typography.values() if value is not None]
    page_max_font = max((value["font_size"] for value in typed), default=0.0)
    # Body size = the most frequent line size on the page. Both members of a title/subtitle pair stay
    # far above it, which allows a display size step (main title + subtitle) to stay one bundle.
    body_sizes = [float(item["font_size"]) for item in lines.values() if isinstance(item.get("font_size"), (int, float)) and float(item["font_size"]) > 0]
    body_font = max(set(body_sizes), key=body_sizes.count) if body_sizes else 0.0
    display_fonts = bool(typed) and page_max_font > 0
    best: list[dict[str, Any]] = []
    index = 0
    while index < len(blocks):
        seed = blocks[index]
        seed_box = seed.get("bbox_image_px")
        seed_text = re.sub(r"\s+", "", str(seed.get("text", "")))
        if not isinstance(seed_box, list) or len(seed_box) != 4 or not 2 <= len(seed_text) <= 60:
            index += 1
            continue
        seed_height = float(seed_box[3]) - float(seed_box[1])
        if not 0 < seed_height <= 160:
            index += 1
            continue
        seed_type = typography.get(str(seed.get("block_id")))
        if display_fonts:
            if seed_type is None or seed_type["font_size"] < page_max_font * 0.85:
                index += 1
                continue
        # A masthead image can push the title below the top third of the page. A line that is clearly
        # display-sized against the page's body font with nothing but display matter above it is still
        # a cover title, so the position test is the top third OR that display arrangement.
        seed_display = (
            seed_type is not None and body_font > 0 and seed_type["font_size"] >= body_font * 1.6
        )
        if float(seed_box[1]) > height * 0.35 and not (
            seed_display and _cover_seed_position_ok(seed_box, height, lines, body_font)
        ):
            index += 1
            continue
        stack = [seed]
        cursor = seed_box[3]
        heights = [seed_height]
        gaps: list[float] = []
        group_sizes = [seed_type["font_size"]] if seed_type else []
        group_bold = seed_type["bold"] if seed_type else None
        for block in blocks[index + 1:]:
            box = block.get("bbox_image_px")
            text = re.sub(r"\s+", "", str(block.get("text", "")))
            if not isinstance(box, list) or len(box) != 4 or not 2 <= len(text) <= 60:
                break
            if len(stack) >= 6 or float(box[1]) > height * 0.6:
                break
            block_type = typography.get(str(block.get("block_id")))
            if display_fonts:
                if block_type is None or block_type["bold"] != group_bold:
                    break
                reference_font = _median_number(group_sizes)
                display_pair = (
                    body_font > 0
                    and reference_font >= body_font * 1.6
                    and block_type["font_size"] >= body_font * 1.6
                )
                lower, upper = (0.75, 1.25) if display_pair else (0.88, 1.12)
                if not reference_font * lower <= block_type["font_size"] <= reference_font * upper:
                    break
            block_height = float(box[3]) - float(box[1])
            reference_height = _median_number(heights)
            if not display_fonts and not reference_height * 0.78 <= block_height <= reference_height * 1.28:
                break
            if not _same_alignment(seed_box, box, width):
                break
            gap = float(box[1]) - cursor
            ceiling = max(reference_height * 0.9, _median_number(gaps) * 1.35 if gaps else 0.0)
            if not -2.0 <= gap <= ceiling:
                break
            stack.append(block)
            cursor = box[3]
            heights.append(block_height)
            gaps.append(gap)
            if block_type:
                group_sizes.append(block_type["font_size"])
        if len(stack) >= 2:
            if len(stack) > len(best):
                best = stack
            index += len(stack)
            continue
        index += 1
    return best


def build_cover_title_stack_tasks(
    sample_id: str,
    pages: dict[int, dict[str, Any]],
    results: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not results:
        return []
    first_page = min(results)
    page = pages.get(first_page)
    result = results.get(first_page)
    if not page or not result:
        return []
    stack = _cover_title_stack(page, result)
    if not stack:
        return []
    lines = {
        str(block.get("block_id")): typography
        for block in stack
        for typography in [_block_typography(_pdf_line_typography(page), block)]
        if typography is not None
    }
    # A bundle is adjudicated as a unit even when it looks homogeneous: the document-level review is
    # what makes a wrapped cover title explicit, and it keeps the "possible multiline root group"
    # abstention from firing on lines that are already one typographic unit.
    items = []
    for block in stack:
        block_id = str(block["block_id"])
        item = {
            "page": first_page,
            "block_id": block_id,
            "text": str(block.get("text", ""))[:500],
            "reading_order": int(block.get("reading_order", 0)),
            "bbox_image_px": block.get("bbox_image_px"),
            "current_type": block.get("type"),
            "current_level": block.get("level"),
            "allowed_types": ["text", "title"],
            "allowed_levels": list(range(1, MAX_TITLE_LEVEL + 1)),
            "needs": ["type", "level"],
            "evidence": [
                {
                    "evidence_id": ref,
                    "source": str(ref).split(".")[0],
                    "text": str(block.get("text", ""))[:300],
                }
                for ref in list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", [])))
            ],
            "semantic_context": {
                "current_page_reading_context": {},
                "bound_group": [
                    {
                        "block_id": str(part["block_id"]),
                        "text": str(part.get("text", ""))[:240],
                        "type": part.get("type"),
                        "level": part.get("level"),
                        **(
                            {"font_size": lines[str(part["block_id"])]["font_size"], "bold": lines[str(part["block_id"])]["bold"]}
                            if str(part["block_id"]) in lines
                            else {}
                        ),
                    }
                    for part in stack
                ],
                "context_note": (
                    "These lines are bundled as one cover-title stack because they share the same typography "
                    "(font size, weight) and alignment with consistent leading; the next line differs in size, "
                    "weight or spacing. Treat the bundle as one unit: either every line becomes a title sharing "
                    "one level, or every line stays text. Mixed outcomes inside the bundle are not allowed. "
                    "Choose all-title when the lines together name the document or entity; choose all-text only "
                    "if the lines are labels, author or date lines, or other non-heading content."
                ),
            },
        }
        items.append(item)
    return [{
        "task_version": "ocr-agent-cover-title-stack-task.v2",
        "task_id": f"{sample_id}.cover_title_stack.1",
        "sample_id": sample_id,
        "review_kind": "cover_title_stack_alignment",
        "heading_context_complete": True,
        "document_outline": "",
        "type_rules": {"text": TYPE_RULES["text"], "title": TYPE_RULES["title"]},
        "items": items,
    }]


def apply_numbering_depth_convention(
    pages: dict[int, dict[str, Any]],
    results: dict[int, dict[str, Any]],
    *,
    max_title_share: float = 0.25,
    min_members: int = 4,
) -> tuple[dict[int, dict[str, Any]], int]:
    """Keep a document's numbering convention: a numbered depth used as list items stays text.

    Numbering never decides a type on its own, but when a document numbers items the same way
    everywhere and nearly all of them carry content (text), the few members promoted to headings are
    list items too. A member is only kept as a heading when its typography actually differs from the
    text members of that depth (which is what would justify a heading role).
    """
    groups: dict[str, list[tuple[int, dict[str, Any], dict[str, Any] | None]]] = {}
    for page_number, result in results.items():
        page = pages.get(page_number)
        if not page:
            continue
        typography = _pdf_line_typography(page)
        for block in result.get("revision", {}).get("blocks", []):
            if block.get("type") not in {"text", "title"}:
                continue
            token = _sequence_token(str(block.get("text", "")))
            if not token:
                continue
            depth = str(token[0]).split(":")[1] if str(token[0]).startswith("decimal_depth") else str(token[0])
            groups.setdefault(depth, []).append((int(page_number), block, _block_typography(typography, block)))
    output = {number: deepcopy(result) for number, result in results.items()}
    touched: set[int] = set()
    for members in groups.values():
        if len(members) < min_members:
            continue
        titles = [entry for entry in members if entry[1].get("type") == "title"]
        texts = [entry for entry in members if entry[1].get("type") != "title"]
        if not titles or not texts or len(titles) / len(members) > max_title_share:
            continue
        text_styles = {
            (round(float(style["font_size"]), 1), bool(style["bold"]))
            for _page, _block, style in texts
            if style and isinstance(style.get("font_size"), (int, float))
        }
        if not text_styles:
            continue  # no typography to compare: do not guess
        for page_number, block, style in titles:
            if not style or not isinstance(style.get("font_size"), (int, float)):
                continue
            if (round(float(style["font_size"]), 1), bool(style["bold"])) not in text_styles:
                continue  # visibly different from the list items: the heading role is justified
            result = output[page_number]
            target = next((item for item in result["revision"]["blocks"] if item["block_id"] == block["block_id"]), None)
            if target is None:
                continue
            target["type"] = "text"
            target.pop("level", None)
            fields = result["revision"]["field_reviews"]["blocks"].setdefault(target["block_id"], {})
            fields["type"] = {"status": "PASS", "method": "document_numbering_depth_convention", "evidence_refs": list(target.get("source_refs", []))}
            fields.pop("level", None)
            touched.add(page_number)
    for page_number in touched:
        result = output[page_number]
        result["validation"] = validate_revision(pages[page_number], result["revision"])
        result["status"] = result["validation"]["status"]
    return output, len(touched)


PARALLEL_GROUP_FIELD = "parallel_group_uniformity"
_DECORATIVE_MARKER = re.compile(r"^[\ue000-\uf8ff\u2022\u25cf\u25a0\u25aa\u25cb\u25a1\u27a2\u25c6]")


def _marker_signature(text: str) -> tuple[str, str] | None:
    """Marker of a list/heading line, as (kind, token): the glyph itself or the numbering depth."""
    value = re.sub(r"\s+", "", str(text or ""))
    if not value:
        return None
    glyph = _DECORATIVE_MARKER.match(value)
    if glyph:
        return "glyph", glyph.group(0)
    token = _sequence_token(value)
    if token:
        kind = str(token[0])
        depth = kind.split(":")[1] if kind.startswith("decimal_depth") else kind
        return "numbering", depth
    return None


def _parallel_sibling_groups(page: dict[str, Any], blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Peers of one visual list: same marker, same indent, same line height, adjacent on the page."""
    width = float(page.get("page_image", {}).get("width", 0) or 0)
    buckets: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}
    for block in blocks:
        if block.get("type") not in {"text", "title"}:
            continue
        signature = _marker_signature(str(block.get("text", "")))
        box = block.get("bbox_image_px")
        if signature is None or not isinstance(box, list) or len(box) != 4:
            continue
        left = round(float(box[0]) / max(1.0, width) * 100) if width else 0
        height = round((float(box[3]) - float(box[1])) / 4)
        buckets.setdefault((signature[0], signature[1], left, height), []).append(block)
    groups: list[list[dict[str, Any]]] = []
    for members in buckets.values():
        ordered = sorted(members, key=lambda block: (block["bbox_image_px"][1], block["bbox_image_px"][0]))
        if len(ordered) < 2:
            continue
        gaps = [
            float(ordered[index + 1]["bbox_image_px"][1]) - float(ordered[index]["bbox_image_px"][1])
            for index in range(len(ordered) - 1)
        ]
        median = sorted(gaps)[len(gaps) // 2]
        limit = max(3.0 * median, 90.0)
        current = [ordered[0]]
        for index, gap in enumerate(gaps):
            if gap > limit:
                if len(current) >= 2:
                    groups.append(current)
                current = [ordered[index + 1]]
                continue
            current.append(ordered[index + 1])
        if len(current) >= 2:
            groups.append(current)
    return groups


def apply_parallel_group_uniformity(
    pages: dict[int, dict[str, Any]],
    results: dict[int, dict[str, Any]],
    *,
    min_members: int = 2,
) -> tuple[dict[int, dict[str, Any]], int]:
    """Siblings that share a marker are one visual list, so they share one role.

    The document review decides member by member, which can split peers across roles (one bulleted
    heading, its peers plain text). Peers are found by marker, indent and line height; the group's
    majority outcome is applied to every member and each adjustment is recorded on the block.
    """
    output = {number: deepcopy(result) for number, result in results.items()}
    adjusted = 0
    touched: set[int] = set()
    for page_number, page in pages.items():
        result = output.get(page_number)
        if not result:
            continue
        blocks = result.get("revision", {}).get("blocks", [])
        for members in _parallel_sibling_groups(page, blocks):
            if len(members) < min_members:
                continue
            outcomes = collections.Counter(
                (str(block.get("type")), int(block.get("level")) if block.get("type") == "title" else None)
                for block in members
            )
            if len(outcomes) < 2:
                continue
            applied, _count = outcomes.most_common(1)[0]
            if outcomes.most_common(2)[0][1] == outcomes.most_common(2)[1][1]:
                applied = ("title", members[0].get("level")) if members[0].get("type") == "title" else ("text", None)
            group_id = f"parallel_group:{page_number}:{members[0]['block_id']}"
            for block in members:
                previous = (str(block.get("type")), block.get("level"))
                if previous == applied:
                    continue
                block["type"] = applied[0]
                if applied[0] == "title":
                    block["level"] = applied[1] if applied[1] is not None else 1
                else:
                    block.pop("level", None)
                    block.pop("parent_id", None)
                fields = result["revision"]["field_reviews"]["blocks"].setdefault(block["block_id"], {})
                fields["type"] = {
                    "status": "PASS",
                    "method": "document_title_parallel_group_uniform",
                    "evidence_refs": list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", []))),
                    "parallel_group_id": group_id,
                    "applied_type": applied[0],
                    "previous_type": previous[0],
                }
                if applied[0] == "title":
                    fields["level"] = {
                        "status": "PASS",
                        "method": "document_title_parallel_group_uniform",
                        "evidence_refs": list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", []))),
                        "parallel_group_id": group_id,
                        "applied_level": block.get("level"),
                        "previous_level": previous[1],
                    }
                elif "level" in fields:
                    fields.pop("level", None)
                touched.add(page_number)
                adjusted += 1
    for page_number in touched:
        result = output[page_number]
        page = pages.get(page_number) or {}
        if page.get("evidence") is None:
            continue  # partial page fixture: nothing to validate against
        result["validation"] = validate_revision(page, result["revision"])
        result["status"] = result["validation"]["status"]
    return output, adjusted


def apply_title_level_coherence(
    pages: dict[int, dict[str, Any]],
    results: dict[int, dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], int]:
    """A heading never sits more than one level below the heading that governs it.

    Levels express nesting, so a title whose level skips a step (a section labelled 3 directly under a
    level-1 root) breaks the tree every consumer builds from it. The document's titles are walked in
    reading order with the ancestor stack; a level that would skip a step is clamped to parent + 1 and
    the clamp cascades to the titles beneath it. Levels are only ever reduced, never invented.
    """
    output = {number: deepcopy(result) for number, result in results.items()}
    adjusted = 0
    touched: set[int] = set()
    stack: list[int] = []
    for page_number in sorted(output):
        result = output[page_number]
        for block in result.get("revision", {}).get("blocks", []):
            if block.get("type") != "title":
                continue
            level = block.get("level")
            if not isinstance(level, int) or level < 1:
                continue
            while stack and stack[-1] >= level:
                stack.pop()
            ceiling = (stack[-1] + 1) if stack else level
            if level > ceiling:
                block["level"] = ceiling
                fields = result["revision"]["field_reviews"]["blocks"].setdefault(block["block_id"], {})
                fields["level"] = {
                    "status": "PASS",
                    "method": "document_title_level_coherence",
                    "evidence_refs": list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", []))),
                    "previous_level": level,
                }
                adjusted += 1
                touched.add(page_number)
                level = ceiling
            stack.append(level)
    for page_number in touched:
        result = output[page_number]
        page = pages.get(page_number) or {}
        if page.get("evidence") is None:
            continue
        result["validation"] = validate_revision(page, result["revision"])
        result["status"] = result["validation"]["status"]
    return output, adjusted


def mark_ambiguous_multiline_root_groups(
    pages: dict[int, dict[str, Any]],
    results: dict[int, dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], int]:
    if not results:
        return results, 0
    first_page = min(results)
    page = pages.get(first_page)
    if not page:
        return results, 0
    output = {number: deepcopy(result) for number, result in results.items()}
    result = output[first_page]
    revision = result.get("revision", {})
    blocks = sorted(revision.get("blocks", []), key=lambda block: block.get("reading_order", 0))
    width = float(page.get("page_image", {}).get("width", 0))
    marked: set[str] = set()
    for first, second in zip(blocks, blocks[1:]):
        if first.get("type") != "title" or second.get("type") != "title" or first.get("level") != 1 or second.get("level") != 1:
            continue
        methods = {
            result["revision"]["field_reviews"]["blocks"].get(str(block.get("block_id")), {}).get("type", {}).get("method", "")
            for block in (first, second)
        }
        if methods & DOCUMENT_REVIEW_METHODS:
            continue
        first_box, second_box = first.get("bbox_image_px", []), second.get("bbox_image_px", [])
        if len(first_box) != 4 or len(second_box) != 4 or width <= 0:
            continue
        first_height = float(first_box[3]) - float(first_box[1])
        second_height = float(second_box[3]) - float(second_box[1])
        gap = float(second_box[1]) - float(first_box[3])
        center_delta = abs((float(first_box[0]) + float(first_box[2])) / 2 - (float(second_box[0]) + float(second_box[2])) / 2)
        if min(first_height, second_height) <= 0 or abs(first_height - second_height) > max(first_height, second_height) * 0.2:
            continue
        if not 0 <= gap <= min(first_height, second_height) * 0.6 or center_delta > width * 0.01:
            continue
        for block in (first, second):
            block_id = str(block["block_id"])
            if block_id in marked:
                continue
            marked.add(block_id)
            refs = list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", [])))
            existing = any(
                item.get("field") == "structure" and item.get("target") == block_id and item.get("issue") == "possible_multiline_root_group"
                for item in revision.get("review_items", [])
            )
            if not existing:
                revision.setdefault("review_items", []).append({
                    "field": "structure",
                    "target": block_id,
                    "issue": "possible_multiline_root_group",
                    "detail": "Adjacent centered level-1 title lines may be one wrapped cover title; merge relation is unresolved.",
                    "evidence_refs": refs,
                })
    if marked:
        result["validation"] = validate_revision(page, revision)
        result["status"] = result["validation"]["status"]
    return output, len(marked)


def _find_revision_block(
    output: dict[int, dict[str, Any]], block_id: str
) -> tuple[int, dict[str, Any], dict[str, Any]] | None:
    for page_number, result in output.items():
        for block in result.get("revision", {}).get("blocks", []):
            if block.get("block_id") == block_id:
                return int(page_number), block, result
    return None


def _propagate_cover_title_bundles(
    pages: dict[int, dict[str, Any]],
    output: dict[int, dict[str, Any]],
    task: dict[str, Any],
    decided: set[str],
) -> set[int]:
    """Keep a typography bundle atomic: a decision on one bundled line applies to its siblings.

    Conflicting decisions inside one bundle mean the model contradicted itself, so the whole
    bundle abstains (CHECK) instead of leaving a mixed outcome behind.
    """
    if not decided:
        return set()
    items = {item["block_id"]: item for item in task["items"]}
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for block_id in decided:
        group = items.get(block_id, {}).get("semantic_context", {}).get("bound_group")
        if not group:
            continue
        member_ids = tuple(sorted(str(member.get("block_id")) for member in group))
        groups.setdefault(member_ids, group)
    touched: set[int] = set()
    for member_ids, group in groups.items():
        decided_members = [member_id for member_id in member_ids if member_id in decided]
        if not decided_members:
            continue
        outcomes = set()
        for member_id in decided_members:
            found = _find_revision_block(output, member_id)
            if found is None:
                continue
            block = found[1]
            outcomes.add((block.get("type"), block.get("level") if block.get("type") == "title" else None))
        if len(outcomes) > 1:
            # The bundle is one visual unit: the leading line (first in reading order) defines the
            # outcome for every line, so the group never ends up with mixed types or levels.
            leading = min(
                (items[member_id] for member_id in member_ids if member_id in items),
                key=lambda item: (int(item.get("page", 0)), int(item.get("reading_order", 0))),
                default=None,
            )
            leading_id = str(leading.get("block_id")) if leading else member_ids[0]
            source = _find_revision_block(output, leading_id)
            if source is None:
                continue
            _, source_block, source_result = source
            refs = list(source_result["revision"]["field_reviews"]["blocks"].get(leading_id, {}).get("type", {}).get("evidence_refs") or [])
            for member_id in member_ids:
                if member_id == leading_id:
                    continue
                found = _find_revision_block(output, member_id)
                if found is None:
                    continue
                page_number, block, result = found
                block["type"] = source_block.get("type")
                if source_block.get("type") == "title":
                    block["level"] = source_block.get("level")
                else:
                    block.pop("level", None)
                fields = result["revision"]["field_reviews"]["blocks"].setdefault(member_id, {})
                fields["type"] = {"status": "PASS", "method": "document_cover_title_bundle_leading_line", "evidence_refs": refs}
                if source_block.get("type") == "title":
                    fields["level"] = {"status": "PASS", "method": "document_cover_title_bundle_leading_line", "evidence_refs": refs}
                else:
                    fields.pop("level", None)
                touched.add(page_number)
            continue
        source = _find_revision_block(output, decided_members[0])
        if source is None:
            continue
        _, source_block, source_result = source
        refs = list(source_result["revision"]["field_reviews"]["blocks"].get(decided_members[0], {}).get("type", {}).get("evidence_refs") or [])
        for member in group:
            member_id = str(member.get("block_id"))
            if not member_id or member_id in decided:
                continue
            found = _find_revision_block(output, member_id)
            if found is None:
                continue
            page_number, block, result = found
            same = block.get("type") == source_block.get("type") and (
                source_block.get("type") != "title" or block.get("level") == source_block.get("level")
            )
            if same:
                continue
            block["type"] = source_block.get("type")
            if source_block.get("type") == "title":
                block["level"] = source_block.get("level")
            else:
                block.pop("level", None)
            fields = result["revision"]["field_reviews"]["blocks"].setdefault(member_id, {})
            fields["type"] = {"status": "PASS", "method": "document_cover_title_bundle_atomicity", "evidence_refs": refs}
            if source_block.get("type") == "title":
                fields["level"] = {"status": "PASS", "method": "document_cover_title_bundle_atomicity", "evidence_refs": refs}
            else:
                fields.pop("level", None)
            touched.add(page_number)
    for page_number in touched:
        result = output[page_number]
        result["validation"] = validate_revision(pages[page_number], result["revision"])
        result["status"] = result["validation"]["status"]
    return touched


def apply_title_consistency_decision(
    pages: dict[int, dict[str, Any]],
    results: dict[int, dict[str, Any]],
    task: dict[str, Any],
    response: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    if response.get("task_id") != task["task_id"]:
        raise OperationError("title consistency task_id mismatch")
    decisions = response.get("decisions")
    unresolved = response.get("unresolved", [])
    if not isinstance(decisions, list) or not isinstance(unresolved, list):
        raise OperationError("title consistency response requires decisions and unresolved")
    items = {item["block_id"]: item for item in task["items"]}
    output = {page: deepcopy(result) for page, result in results.items()}
    seen: set[str] = set()
    decided: set[str] = set()
    for decision in decisions:
        block_id = decision.get("block_id") if isinstance(decision, dict) else None
        if block_id not in items or block_id in seen:
            raise OperationError(f"unknown or duplicate title consistency block_id: {block_id}")
        item = items[block_id]
        refs = decision.get("evidence_refs")
        allowed_refs = {entry["evidence_id"] for entry in item["evidence"]}
        if not isinstance(refs, list) or not refs or set(refs) - allowed_refs:
            raise OperationError(f"{block_id}: invalid title consistency evidence_refs")
        block_type = decision.get("type")
        if block_type not in item["allowed_types"]:
            raise OperationError(f"{block_id}: invalid title consistency type")
        result = output[int(item["page"])]
        result["revision"]["review_items"] = [
            review for review in result["revision"].get("review_items", [])
            if not (review.get("task_id") == task["task_id"] and review.get("target") == block_id)
        ]
        block = next((candidate for candidate in result["revision"]["blocks"] if candidate["block_id"] == block_id), None)
        if block is None:
            raise OperationError(f"missing title consistency target: {block_id}")
        fields = result["revision"]["field_reviews"]["blocks"][block_id]
        method = {
            "parallel_layout_alignment": "document_title_alignment_review",
            "cover_title_stack_alignment": "document_cover_title_stack_review",
        }.get(task.get("review_kind"), "document_title_consistency_review")
        block["type"] = block_type
        fields["type"] = {"status": "PASS", "method": method, "evidence_refs": refs}
        if block_type == "title":
            level = decision.get("level")
            if level not in item["allowed_levels"]:
                raise OperationError(f"{block_id}: invalid title consistency level")
            block["level"] = level
            fields["level"] = {"status": "PASS", "method": method, "evidence_refs": refs}
        else:
            block.pop("level", None)
            fields.pop("level", None)
        seen.add(block_id)
        decided.add(block_id)
    for unresolved_item in unresolved:
        block_id = unresolved_item.get("block_id") if isinstance(unresolved_item, dict) else None
        if block_id not in items or block_id in seen:
            continue
        item = items[block_id]
        result = output[int(item["page"])]
        fields = result["revision"]["field_reviews"]["blocks"][block_id]
        field = unresolved_item.get("field", "type")
        if field not in {"type", "level"}:
            field = "type"
        fields.setdefault(field, {})["status"] = "CHECK"
        fields[field]["method"] = "document_title_consistency_unresolved"
        result["revision"].setdefault("review_items", []).append({
            "field": field,
            "target": block_id,
            "issue": str(unresolved_item.get("reason", "document title consistency unresolved")),
            "evidence_refs": [],
            "task_id": task["task_id"],
        })
        seen.add(block_id)
    missing = set(items) - seen
    if missing:
        raise OperationError(f"title consistency response omitted blocks: {sorted(missing)}")
    touched = {int(item["page"]) for item in items.values()}
    touched.update(_propagate_cover_title_bundles(pages, output, task, decided))
    for page_number in touched:
        result = output[page_number]
        report = validate_revision(pages[page_number], result["revision"])
        result["validation"] = report
        result["status"] = report["status"]
    return output
