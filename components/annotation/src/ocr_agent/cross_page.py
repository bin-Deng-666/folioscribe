"""Cross-page contract: text-flow continuation and cross-page table continuation.

Design: rules select candidates and produce deterministic conclusions; the model only
adjudicates text-flow under a constrained gate; the document-level finalizer back-fills
reverse links and table group metadata. Every edge carries method + evidence + basis.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

from .constants import TEXT_TYPES

CROSS_PAGE_TEXT_FIELD = "cross_page_text"
CROSS_PAGE_TABLE_FIELD = "cross_page_table"
CROSS_PAGE_TEXT_TASK_VERSION = "ocr-agent-cross-page-text-task.v1"
CROSS_PAGE_TEXT_METHOD = "model_cross_page_text_flow"

# Gate thresholds (relative, language-agnostic).
EDGE_TOP_RATIO = 0.25
EDGE_BOTTOM_RATIO = 0.82
COLUMN_OVERLAP_MIN = 0.5
CONTENT_BOTTOM_RATIO = 0.95
# A table that ends in the middle of a page is not cut by the page edge: "truncated at page end" needs
# the grid to actually reach the bottom of the page's writing area.
PAGE_END_FLOOR_RATIO = 0.82
COLUMN_TOLERANCE_RATIO = 0.01
COLUMN_TOLERANCE_MIN_PX = 6.0
HEADER_SIMILARITY_MIN = 0.85
RULE_THICKNESS_PX = 5.0
CLOSING_RULE_COVERAGE = 0.6

FLOW_TEXT_KINDS = {"text", "unknown"}
BOUNDARY_KINDS = {"table", "title"}
FURNITURE_KINDS = {"header", "footer", "page_number", "footnote"}

TERMINAL_CHARS = set("。．.！!？?；;：:）)】]｝}」』”\"'…—–、,")
NEW_SECTION_PATTERNS = (
    re.compile(r"^\s*\d{1,3}\s*[.。)）、]"),
    re.compile(r"^\s*[一二三四五六七八九十百千]{1,3}\s*[.、)）]"),
    re.compile(r"^\s*[（(]\s*[0-9一二三四五六七八九十百千]{1,3}\s*[)）]"),
    re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百千]{1,5}\s*[章节節条款款项篇部]"),
    re.compile(r"^\s*(?:附\s*[件录表]|附录|附表|附件)"),
    re.compile(r"^\s*[-•▪◦*·—–－]\s*"),
    re.compile(r"^\s*[^，。；;、：:\s]{1,12}\s*[：:]\s*"),
    re.compile(r"^\s*(?:table|figure|fig\.?|表|图)\s*\d+", re.IGNORECASE),
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).casefold()


def is_open_tail(text: str) -> bool:
    """True when the text does not end on a terminal punctuation mark (sentence is cut)."""
    stripped = (text or "").rstrip()
    if not stripped:
        return False
    last = stripped[-1]
    if last in TERMINAL_CHARS:
        return False
    return unicodedata.category(last) not in {"Po", "Pe", "Pf"}


def starts_new_section(text: str) -> bool:
    head = re.sub(r"\s+", "", text or "")
    if not head:
        return True
    return any(pattern.match(head) for pattern in NEW_SECTION_PATTERNS)


def horizontal_overlap(left: list[float], right: list[float]) -> float:
    width = min(left[2] - left[0], right[2] - right[0])
    if width <= 0:
        return 0.0
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) / width


def _box(value: Any) -> list[float] | None:
    if isinstance(value, list) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [float(item) for item in value]
    return None


def cross_page_text_candidate(page: dict[str, Any], tail_blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rule gate selecting the (previous tail, current head) pair worth adjudicating. Any failing signal -> no candidate."""
    if not page.get("document_context", {}).get("previous_pages_complete"):
        return None
    blocks = sorted(page.get("current_candidate", {}).get("blocks", []), key=lambda block: block.get("reading_order", 0))
    head = next(
        (block for block in blocks if block.get("type") in FLOW_TEXT_KINDS and str(block.get("text", "")).strip()),
        None,
    )
    if head is None:
        return None
    height = float(page["page_image"]["height"])
    head_box = _box(head.get("bbox_image_px"))
    if head_box is None:
        return None
    tail = None
    for item in reversed(tail_blocks):
        kind = item.get("type")
        if kind in FURNITURE_KINDS:
            continue
        if kind in BOUNDARY_KINDS:
            return None
        if kind not in FLOW_TEXT_KINDS:
            continue
        box = _box(item.get("bbox_image_px"))
        if box is None:
            return None
        if box[3] < height * EDGE_BOTTOM_RATIO:
            return None
        tail = item
        break
    if tail is None:
        return None
    tail_box = _box(tail.get("bbox_image_px"))
    if tail_box is None:
        return None
    basis = {
        "edge_geometry": head_box[1] <= height * EDGE_TOP_RATIO,
        "same_column": horizontal_overlap(tail_box, head_box) >= COLUMN_OVERLAP_MIN,
        "tail_open": is_open_tail(str(tail.get("text_tail") or tail.get("text") or "")),
        "head_not_new_section": not starts_new_section(str(head.get("text", ""))),
    }
    if not all(basis.values()):
        return None
    return {"tail": tail, "head": head, "head_box": head_box, "tail_box": tail_box, "basis": basis}


def build_cross_page_text_task(page: dict[str, Any], tail_blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidate = cross_page_text_candidate(page, tail_blocks)
    if candidate is None:
        return None
    head, tail = candidate["head"], candidate["tail"]
    return {
        "task_version": CROSS_PAGE_TEXT_TASK_VERSION,
        "task_id": f"{page['sample_id']}.p{page['page']}.cross_page_text.1",
        "sample_id": page["sample_id"],
        "page": page["page"],
        "allowed_relations": ["continuous", "separate", "uncertain"],
        "tail": {
            "block_id": tail.get("block_id"),
            "page": tail.get("page") or int(page["page"]) - 1,
            "type": tail.get("type"),
            "text": str(tail.get("text_tail") or tail.get("text") or "")[:320],
            "bbox_image_px": tail.get("bbox_image_px"),
            "source_refs": tail.get("source_refs", []),
        },
        "head": {
            "block_id": head["block_id"],
            "type": head.get("type"),
            "text": str(head.get("text", ""))[:320],
            "bbox_image_px": head.get("bbox_image_px"),
            "source_refs": head.get("source_refs", []),
        },
        "page_size": {"width": page["page_image"]["width"], "height": page["page_image"]["height"]},
        "gate_basis": candidate["basis"],
        "instruction": (
            "Decide only whether the previous page's final text block is CUT MID-SENTENCE and continues "
            "into the current page's first text block. Return continuous only for direct sentence/paragraph "
            "continuation. A list item, numbering, heading, caption or table row that merely follows is separate. "
            "If the tail already ends a sentence, return separate. If evidence is insufficient, return uncertain."
        ),
    }


def _link_evidence(tail: dict[str, Any], head: dict[str, Any]) -> list[str]:
    refs = list(tail.get("source_refs", [])) + list(head.get("source_refs", []))
    return list(dict.fromkeys(str(ref) for ref in refs if ref))


def apply_cross_page_text_decision(page: dict[str, Any], task: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Write the forward text-flow link on the head block. Returns the audit record."""
    blocks = {block["block_id"]: block for block in page.get("current_candidate", {}).get("blocks", [])}
    head_id = task["head"]["block_id"]
    tail_id = task["tail"]["block_id"]
    head = blocks.get(head_id)
    if head is None:
        return {"status": "CHECK", "task_id": task["task_id"], "issue": "cross-page head block not found"}
    relation = response.get("relation")
    refs = _link_evidence(task["tail"], task["head"])
    fields = page.setdefault("field_reviews", {}).setdefault("blocks", {}).setdefault(head_id, {})
    field: dict[str, Any] = {
        "continues_block_id": None,
        "continued_by_block_id": None,
        "confidence_basis": task.get("gate_basis", {}),
        "evidence_refs": refs,
    }
    if relation == "continuous":
        field.update({"continues_block_id": tail_id, "status": "PASS", "method": CROSS_PAGE_TEXT_METHOD})
    elif relation == "separate":
        field.update({"status": "PASS", "method": CROSS_PAGE_TEXT_METHOD})
    else:
        field.update({"status": "CHECK", "method": "cross_page_text_flow_unresolved"})
    head[CROSS_PAGE_TEXT_FIELD] = field
    fields[CROSS_PAGE_TEXT_FIELD] = {
        "status": field["status"],
        "method": field["method"],
        "evidence_refs": refs,
    }
    return {
        "task_id": task["task_id"],
        "page": page["page"],
        "tail_block_id": tail_id,
        "head_block_id": head_id,
        "relation": relation if relation in {"continuous", "separate"} else "uncertain",
        "status": field["status"],
        "method": field["method"],
        "evidence_refs": refs,
    }


def mark_cross_page_text_failure(page: dict[str, Any], task: dict[str, Any], error: str) -> dict[str, Any]:
    blocks = {block["block_id"]: block for block in page.get("current_candidate", {}).get("blocks", [])}
    head_id = task["head"]["block_id"]
    head = blocks.get(head_id)
    if head is not None:
        head[CROSS_PAGE_TEXT_FIELD] = {
            "continues_block_id": None,
            "continued_by_block_id": None,
            "status": "CHECK",
            "method": "cross_page_text_flow_failure",
            "confidence_basis": task.get("gate_basis", {}),
            "evidence_refs": _link_evidence(task["tail"], task["head"]),
        }
    return {
        "task_id": task["task_id"],
        "page": page["page"],
        "tail_block_id": task["tail"]["block_id"],
        "head_block_id": head_id,
        "relation": "uncertain",
        "status": "CHECK",
        "method": "cross_page_text_flow_failure",
        "issue": error,
        "evidence_refs": _link_evidence(task["tail"], task["head"]),
    }


# ---------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------


def _rules_inside(drawings: list[dict[str, Any]], box: list[float]) -> tuple[list[float], list[float]]:
    vertical: list[float] = []
    horizontal: list[float] = []
    for item in drawings:
        rule = _box(item.get("bbox_image_px"))
        if rule is None:
            continue
        if not (box[0] - 2 <= (rule[0] + rule[2]) / 2 <= box[2] + 2 and box[1] - 2 <= (rule[1] + rule[3]) / 2 <= box[3] + 2):
            continue
        if rule[2] - rule[0] <= RULE_THICKNESS_PX and rule[3] - rule[1] >= (box[3] - box[1]) * 0.5:
            vertical.append((rule[0] + rule[2]) / 2)
        elif rule[3] - rule[1] <= RULE_THICKNESS_PX and rule[2] - rule[0] >= (box[2] - box[0]) * 0.3:
            horizontal.append((rule[1] + rule[3]) / 2)
    return sorted(set(round(value, 1) for value in vertical)), sorted(set(round(value, 1) for value in horizontal))


def _cluster(values: list[float], tolerance: float) -> list[float]:
    clusters: list[list[float]] = []
    for value in sorted(values):
        if clusters and value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def column_boundaries(
    drawings: list[dict[str, Any]], lines: list[dict[str, Any]], box: list[float], page_width: float
) -> list[float]:
    """Column boundary x positions: ruled verticals first, else clustered text left edges."""
    vertical, _ = _rules_inside(drawings, box)
    if len(vertical) >= 2:
        return [round(value, 1) for value in vertical]
    tolerance = max(COLUMN_TOLERANCE_MIN_PX, page_width * COLUMN_TOLERANCE_RATIO)
    lefts = [
        line["bbox_image_px"][0]
        for line in lines
        if str(line.get("text", "")).strip()
        and _box(line.get("bbox_image_px")) is not None
        and box[0] - 2 <= (line["bbox_image_px"][0] + line["bbox_image_px"][2]) / 2 <= box[2] + 2
        and box[1] - 2 <= (line["bbox_image_px"][1] + line["bbox_image_px"][3]) / 2 <= box[3] + 2
    ]
    if len(lefts) < 2:
        return []
    return [round(value, 1) for value in _cluster([float(value) for value in lefts], tolerance)]


def same_column_structure(previous: list[float] | None, current: list[float] | None, page_width: float) -> bool:
    if not previous or not current or len(previous) != len(current):
        return False
    tolerance = max(COLUMN_TOLERANCE_MIN_PX, page_width * COLUMN_TOLERANCE_RATIO)
    return all(abs(left - right) <= tolerance for left, right in zip(previous, current))


def has_closing_rule(drawings: list[dict[str, Any]], box: list[float]) -> bool:
    _, horizontal = _rules_inside(drawings, box)
    width = box[2] - box[0]
    for value in horizontal:
        if abs(value - box[3]) <= 3.0:
            return True
    if width <= 0:
        return False
    for item in drawings:
        rule = _box(item.get("bbox_image_px"))
        if rule is None:
            continue
        if rule[3] - rule[1] > RULE_THICKNESS_PX:
            continue
        if abs((rule[1] + rule[3]) / 2 - box[3]) > 3.0:
            continue
        if (rule[2] - rule[0]) / width >= CLOSING_RULE_COVERAGE:
            return True
    return False


def first_row_text(html_value: str) -> str:
    match = re.search(r"<tr[^>]*>(.*?)</tr>", html_value or "", flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    return normalize_text(re.sub(r"<[^>]+>", "", match.group(1)))


MIN_QUOTE_LENGTH = 3


_TAG = re.compile(r"<[^>]+>")


def _fragment_text(item: dict[str, Any]) -> str:
    """All text a fragment carries: block text (head and tail), the tail's table HTML without tags."""
    parts = [str(item.get("text", "")), str(item.get("text_tail", ""))]
    for value in [str(item.get("html", ""))] + [str(entry) for entry in item.get("table_content", []) or []]:
        if value:
            parts.append(_TAG.sub("", value))
    return normalize_text("\n".join(parts))


def _quote_found(quote: str, haystack: str, min_ratio: float = 0.85) -> bool:
    """A quote counts as grounded when it is there, allowing a single symbol variant (≤ vs <)."""
    if not quote:
        return False
    if quote in haystack:
        return True
    matcher = difflib.SequenceMatcher(None, quote, haystack, autojunk=False)
    match = matcher.find_longest_match(0, len(quote), 0, len(haystack))
    return match.size / len(quote) >= min_ratio


def verified_quote_continuation(
    previous_tables: list[dict[str, Any]], table: dict[str, Any], response: dict[str, Any] | None
) -> str | None:
    """Adopt a model continuation only when the boundary text it cites is really in both fragments.

    A model reason is not evidence. When the rule itself cannot confirm the link, the model must quote
    the tail of the previous fragment and the head of the current one; both quotes have to be found in
    the supplied text. A quote that is missing, too short or not found leaves the block unresolved.
    """
    if not response or not previous_tables:
        return None
    before = normalize_text(str(response.get("boundary_before") or ""))
    after = normalize_text(str(response.get("boundary_after") or ""))
    if len(before) < MIN_QUOTE_LENGTH or len(after) < MIN_QUOTE_LENGTH:
        return None
    named = str(response.get("previous_block_id") or "")
    previous = next((item for item in previous_tables if str(item.get("block_id")) == named), None)
    if previous is None:
        return None
    if not _quote_found(before, _fragment_text(previous)):
        return None
    if not _quote_found(after, _fragment_text(table)):
        return None
    return str(previous.get("block_id"))


def word_row_coverage_state(table: dict[str, Any]) -> tuple[bool, bool] | None:
    """Exact page state from the Word table's row coverage.

    The native binding knows how many rows the table has and which rows this fragment carries, so
    "cut off at the page end" is simply "rows remain after this fragment" - no geometry needed.
    """
    shape = table.get("word_table_shape")
    rows = table.get("word_row_range")
    if not isinstance(shape, list) or not shape or not isinstance(rows, list) or len(rows) != 2:
        return None
    try:
        total, first, last = int(shape[0]), int(rows[0]), int(rows[1])
    except (TypeError, ValueError):
        return None
    if total < 1 or first < 0 or last < first:
        return None
    return last + 1 < total, first > 0


def word_contiguous_previous(previous_tables: list[dict[str, Any]], table: dict[str, Any]) -> str | None:
    """The previous fragment of the same Word table when its rows run straight into this one."""
    reference = str(table.get("word_table_ref") or "")
    rows = table.get("word_row_range")
    if not reference or not isinstance(rows, list) or len(rows) != 2:
        return None
    try:
        first = int(rows[0])
    except (TypeError, ValueError):
        return None
    for item in reversed(previous_tables):
        if str(item.get("word_table_ref") or "") != reference:
            continue
        other = item.get("word_row_range")
        if isinstance(other, list) and len(other) == 2:
            try:
                if int(other[1]) + 1 == first:
                    return str(item.get("block_id"))
            except (TypeError, ValueError):
                continue
    return None


def annotate_cross_page_tables(
    page: dict[str, Any],
    tail_blocks: list[dict[str, Any]],
    relation_response: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Rules-based cross-page table annotation. Only annotates when grounded evidence exists."""
    blocks = page.get("current_candidate", {}).get("blocks", [])
    tables = sorted((block for block in blocks if block.get("type") == "table"), key=lambda block: block.get("reading_order", 0))
    if not tables:
        return []
    drawings = [item for item in page.get("evidence", {}).get("pdf", []) if item.get("kind") == "pdf_drawing"]
    lines = [item for item in page.get("evidence", {}).get("pdf", []) if item.get("kind") == "pdf_line"]
    height = float(page["page_image"]["height"])
    width = float(page["page_image"]["width"])
    # "Page content bottom" spans every flow block, not just text: a page whose last flow block is a
    # table or a figure must not make an earlier table look truncated at the page edge.
    furniture_types = {"header", "footer", "page_number", "footnote"}
    flow_bottoms = {
        str(block.get("block_id")): float(block["bbox_image_px"][3])
        for block in blocks
        if block.get("type") not in furniture_types
        and (str(block.get("text", "")).strip() or block.get("type") not in TEXT_TYPES)
    }
    previous_tables = [item for item in tail_blocks if item.get("type") == "table" and not item.get("repeated_page_header")]
    model_continues = bool(relation_response and relation_response.get("relation") == "continuation")
    model_separate = bool(relation_response and relation_response.get("relation") == "new_section")
    records: list[dict[str, Any]] = []
    for index, table in enumerate(tables):
        if table.get("repeated_page_header"):
            table.pop(CROSS_PAGE_TABLE_FIELD, None)
            continue
        table.pop(CROSS_PAGE_TABLE_FIELD, None)  # idempotent re-annotation
        verified_quote_refs: list[str] = []
        box = _box(table.get("bbox_image_px"))
        if box is None:
            continue
        vertical, horizontal = _rules_inside(drawings, box)
        ruled = bool(vertical or horizontal)
        closing = has_closing_rule(drawings, box)
        boundaries = column_boundaries(drawings, lines, box, width)
        continues_from: str | None = None
        repeated_header: bool | None = None
        word_linked = False
        if index == 0 and previous_tables and box[1] <= height * EDGE_TOP_RATIO:
            word_previous = word_contiguous_previous(previous_tables, table)
            previous = next(
                (item for item in previous_tables if str(item.get("block_id")) == word_previous),
                previous_tables[-1],
            )
            if word_previous is not None:
                # the Word table's own row numbering is exact evidence: this fragment carries the rows
                # that follow the previous fragment's rows, so the link does not need the model
                continues_from = word_previous
                word_linked = True
            previous_state = (previous.get(CROSS_PAGE_TABLE_FIELD) or {}).get("truncated_at_page_end")
            if not word_linked and previous_state is not False and same_column_structure(previous.get("column_boundaries"), boundaries, width):
                continues_from = str(previous.get("block_id"))
                previous_first_row = ""
                for html_value in previous.get("table_content", []) or []:
                    previous_first_row = first_row_text(str(html_value))
                    if previous_first_row:
                        break
                current_first_row = first_row_text(str(table.get("html", "")))
                if previous_first_row and current_first_row:
                    repeated_header = difflib.SequenceMatcher(None, previous_first_row, current_first_row).ratio() >= HEADER_SIMILARITY_MIN
        truncated_end: bool | None = None
        word_state = word_row_coverage_state(table)
        truncated_start: bool | None = None
        if word_state is not None:
            truncated_end, truncated_start = word_state
        elif ruled:
            # The table itself must not define the content bottom it is measured against, otherwise a
            # table that is simply the last block on its page always reports itself as cut off.
            others = [bottom for block_id, bottom in flow_bottoms.items() if block_id != str(table.get("block_id"))]
            other_bottom = max(others, default=0.0)
            reaches_page_end = height > 0 and box[3] >= height * PAGE_END_FLOOR_RATIO
            reaches_content_bottom = other_bottom <= 0 or box[3] >= other_bottom * CONTENT_BOTTOM_RATIO
            truncated_end = bool(reaches_page_end and reaches_content_bottom)
        top_table = index == 0 and bool(previous_tables)
        if continues_from is None and truncated_end is None and not (top_table and model_continues):
            continue
        status = "PASS"
        if word_linked and continues_from:
            method = "word_table_contiguous_row_ranges_cross_page"
        elif continues_from:
            method = "pdf_ruled_grid_cross_page_continuation"
        elif word_state is not None:
            method = "word_table_row_coverage_page_state"
        else:
            method = "pdf_ruled_grid_page_end_state"
        if top_table and continues_from and model_separate:
            # rule proposes a continuation, the model explicitly rejects it: keep the conflict visible without linking
            continues_from, status, method = None, "CHECK", "cross_page_table_rule_model_conflict"
        elif top_table and continues_from and model_continues and not word_linked:
            method = "pdf_ruled_grid_cross_page_continuation_model_agreed"
        elif top_table and not continues_from and model_continues:
            quoted = verified_quote_continuation(previous_tables, table, relation_response)
            if quoted:
                # the rule could not link, the model could: its boundary quotes are checked verbatim
                continues_from = quoted
                method = "pdf_ruled_grid_cross_page_continuation_quote_verified"
                verified_quote_refs = [
                    str(ref)
                    for item in previous_tables
                    if str(item.get("block_id")) == quoted
                    for ref in item.get("source_refs", [])
                    if str(ref)
                ][:4]
            else:
                status, method = "CHECK", "cross_page_table_rule_model_unconfirmed"
        field: dict[str, Any] = {
            "document_table_identity": table.get("document_table_identity"),
            "continues_block_id": continues_from,
            "continued_by_block_id": None,
            "truncated_at_page_end": truncated_end,
            "truncated_at_page_start": (
                True if continues_from else truncated_start if word_state is not None else None
            ),
            "repeated_header": repeated_header,
            "has_closing_rule": closing,
            "status": status,
            "method": method,
            "evidence_refs": list(
                dict.fromkeys(
                    [str(item.get("evidence_id")) for item in drawings if _box(item.get("bbox_image_px")) and box[1] - 2 <= (item["bbox_image_px"][1] + item["bbox_image_px"][3]) / 2 <= box[3] + 2]
                    + verified_quote_refs
                )
            )[:12],
        }
        table["cross_page_table"] = field
        page.setdefault("field_reviews", {}).setdefault("blocks", {}).setdefault(table["block_id"], {})["cross_page_table"] = {
            "status": status,
            "method": method,
            "evidence_refs": field["evidence_refs"],
        }
        records.append({"page": page["page"], "block_id": table["block_id"], **{key: field[key] for key in ("continues_block_id", "truncated_at_page_end", "status", "method")}})
    return records


# ---------------------------------------------------------------------------------------
# Document-level finalize
# ---------------------------------------------------------------------------------------


def finalize_cross_page_flow(pages: dict[int, dict[str, Any]], results: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Back-fill reverse links, table group metadata and the document flow summary.

    Mutates ``results`` blocks/reviews; callers re-validate affected pages.
    """
    index: dict[str, tuple[int, dict[str, Any]]] = {}
    for page_number, result in results.items():
        for block in result.get("revision", {}).get("blocks", []):
            index[block["block_id"]] = (page_number, block)
    flow: dict[str, Any] = {"text_edges": [], "table_groups": []}
    conflicts: list[str] = []

    # ---- text edges ----------------------------------------------------------------
    for page_number in sorted(results):
        result = results[page_number]
        for block in result.get("revision", {}).get("blocks", []):
            field = block.get(CROSS_PAGE_TEXT_FIELD)
            if not isinstance(field, dict) or not field.get("continues_block_id"):
                continue
            tail_page, tail = index.get(field["continues_block_id"], (None, None))
            ok = tail is not None and tail_page == page_number - 1
            if not ok:
                field["status"] = "CHECK"
                field["method"] = "cross_page_text_flow_link_conflict"
                result["revision"].setdefault("field_reviews", {}).setdefault("blocks", {}).setdefault(block["block_id"], {})[CROSS_PAGE_TEXT_FIELD] = {
                    "status": "CHECK",
                    "method": field["method"],
                    "evidence_refs": field.get("evidence_refs", []),
                }
                result["revision"].setdefault("review_items", []).append({
                    "field": CROSS_PAGE_TEXT_FIELD,
                    "target": block["block_id"],
                    "issue": "text-flow link does not point to the immediately previous page",
                    "evidence_refs": field.get("evidence_refs", []),
                })
                conflicts.append(block["block_id"])
                continue
            tail_field = tail.setdefault(CROSS_PAGE_TEXT_FIELD, {"continues_block_id": None, "continued_by_block_id": None})
            if tail_field.get("continued_by_block_id") not in (None, block["block_id"]):
                conflicts.append(tail["block_id"])
                tail_field["status"] = "CHECK"
                tail_field["method"] = "cross_page_text_flow_link_conflict"
                tail_page_result = results[tail_page]
                tail_page_result["revision"].setdefault("review_items", []).append({
                    "field": CROSS_PAGE_TEXT_FIELD,
                    "target": tail["block_id"],
                    "issue": "text-flow tail is continued by more than one head block",
                    "evidence_refs": tail_field.get("evidence_refs", []),
                })
                continue
            tail_field["continued_by_block_id"] = block["block_id"]
            tail_field.setdefault("status", "PASS")
            tail_field.setdefault("method", "document_cross_page_text_flow_reverse")
            tail_page_result = results[tail_page]
            tail_page_result["revision"].setdefault("field_reviews", {}).setdefault("blocks", {}).setdefault(tail["block_id"], {})[CROSS_PAGE_TEXT_FIELD] = {
                "status": tail_field.get("status", "PASS"),
                "method": tail_field.get("method", "document_cross_page_text_flow_reverse"),
                "evidence_refs": tail_field.get("evidence_refs", []),
            }
            sample_id = str(pages.get(page_number, {}).get("sample_id", ""))
            flow["text_edges"].append({
                "from": {"sample_id": sample_id, "page": tail_page, "block_id": tail["block_id"]},
                "to": {"sample_id": sample_id, "page": page_number, "block_id": block["block_id"]},
                "status": field.get("status", "PASS"),
                "method": field.get("method", CROSS_PAGE_TEXT_METHOD),
                "evidence_refs": field.get("evidence_refs", []),
            })

    # ---- table groups --------------------------------------------------------------
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent.get(node, node) != node:
            parent[node] = parent.get(parent[node], parent[node])
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    linked: set[str] = set()
    for page_number in sorted(results):
        for block in results[page_number].get("revision", {}).get("blocks", []):
            field = block.get(CROSS_PAGE_TABLE_FIELD)
            if not isinstance(field, dict) or not field.get("continues_block_id"):
                continue
            previous = field["continues_block_id"]
            if previous not in index or index[previous][0] != page_number - 1:
                field["status"] = "CHECK"
                field["method"] = "cross_page_table_link_conflict"
                results[page_number]["revision"].setdefault("review_items", []).append({
                    "field": CROSS_PAGE_TABLE_FIELD,
                    "target": block["block_id"],
                    "issue": "table continuation does not point to the immediately previous page",
                    "evidence_refs": field.get("evidence_refs", []),
                })
                conflicts.append(block["block_id"])
                continue
            union(previous, block["block_id"])
            linked.add(previous)
            linked.add(block["block_id"])
            field["truncated_at_page_start"] = True

    groups: dict[str, list[str]] = {}
    for block_id in linked:
        groups.setdefault(find(block_id), []).append(block_id)
    for root in sorted(groups, key=lambda key: index[key][0] if key in index else 0):
        parts = sorted(groups[root], key=lambda block_id: (index[block_id][0], index[block_id][1].get("reading_order", 0)))
        part_count = len(parts)
        identity = None
        for block_id in parts:
            candidate = index[block_id][1].get(CROSS_PAGE_TABLE_FIELD, {}).get("document_table_identity")
            if candidate:
                identity = candidate
                break
        identity = identity or f"table.{parts[0]}"
        group_parts = []
        for position, block_id in enumerate(parts, start=1):
            page_number, block = index[block_id]
            field = block.setdefault(CROSS_PAGE_TABLE_FIELD, {})
            field["document_table_identity"] = identity
            field["part_index"] = position
            field["part_count"] = part_count
            field["continued_by_block_id"] = parts[position] if position < part_count else None
            if position > 1:
                field["continues_block_id"] = parts[position - 2]
            field["truncated_at_page_start"] = position > 1
            field["truncated_at_page_end"] = position < part_count
            results[page_number]["revision"].setdefault("field_reviews", {}).setdefault("blocks", {}).setdefault(block_id, {})[CROSS_PAGE_TABLE_FIELD] = {
                "status": field.get("status", "PASS"),
                "method": field.get("method", "pdf_ruled_grid_cross_page_continuation"),
                "evidence_refs": field.get("evidence_refs", []),
            }
            group_parts.append({
                "page": page_number,
                "block_id": block_id,
                "truncated_at_page_end": field.get("truncated_at_page_end"),
                "truncated_at_page_start": field.get("truncated_at_page_start"),
                "repeated_header": field.get("repeated_header"),
            })
        flow["table_groups"].append({"document_table_identity": identity, "part_count": part_count, "parts": group_parts})

    # A page-end truncation asserts that the table continues on the next page. Keep the assertion only
    # when the next page really carries a table fragment (or the Word table's own rows say rows remain);
    # otherwise the measured geometry only saw a table that ends near the bottom of its own page, and
    # the honest value is "undetermined" rather than a truncation claim.
    for page_number in sorted(results):
        next_page = pages.get(page_number + 1) or {}
        next_blocks = (results.get(page_number + 1) or {}).get("revision", {}).get("blocks", [])
        next_height = float((next_page.get("page_image") or {}).get("height") or 0)
        for block in results[page_number].get("revision", {}).get("blocks", []):
            field = block.get(CROSS_PAGE_TABLE_FIELD)
            if not isinstance(field, dict) or field.get("truncated_at_page_end") is not True:
                continue
            if field.get("continued_by_block_id") or field.get("part_count"):
                continue
            word_state = word_row_coverage_state(block)
            if word_state is not None and word_state[0]:
                continue
            if next_height > 0 and any(
                item.get("type") == "table"
                and isinstance(item.get("bbox_image_px"), list)
                and len(item["bbox_image_px"]) == 4
                and item["bbox_image_px"][1] <= next_height * EDGE_TOP_RATIO
                for item in next_blocks
            ):
                continue
            field["truncated_at_page_end"] = None
            field["method"] = "page_end_state_unlinked"
            results[page_number]["revision"].setdefault("field_reviews", {}).setdefault("blocks", {}).setdefault(block["block_id"], {})[CROSS_PAGE_TABLE_FIELD] = {
                "status": field.get("status", "PASS"),
                "method": field["method"],
                "evidence_refs": field.get("evidence_refs", []),
            }

    flow["conflicts"] = sorted(set(conflicts))
    return flow
