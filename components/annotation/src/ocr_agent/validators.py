from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
import re
from typing import Any
import unicodedata

from .constants import CAPTION_TYPES, MAX_TITLE_LEVEL, TEXT_TYPES
from .schemas import evidence_index, validate_block


def _contains(outer: list[float], inner: list[float], tolerance: float = 2.0) -> bool:
    return outer[0] <= inner[0] + tolerance and outer[1] <= inner[1] + tolerance and outer[2] + tolerance >= inner[2] and outer[3] + tolerance >= inner[3]


def _overlap_ioa(a: list[float], b: list[float]) -> float:
    intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min(max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1]), max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1]))
    return intersection / smaller if smaller > 0 else 0.0


def _visible_text_norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\\([_`*#{}\[\]()<>+.!-])", r"\1", value)
    return re.sub(r"\s+", "", value).casefold()


def _error(code: str, message: str, *, block_ids: list[str] | None = None, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "block_ids": block_ids or [], "evidence_ids": evidence_ids or []}


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.table_depth = 0
        self.tables = 0
        self.rows = 0
        self.cells = 0
        self.invalid_span = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self.tables += 1
            self.table_depth += 1
        elif tag == "tr" and self.table_depth == 1:
            self.rows += 1
        elif tag in {"td", "th"} and self.table_depth == 1:
            self.cells += 1
            values = dict(attrs)
            for name in ("rowspan", "colspan"):
                if name in values:
                    try:
                        self.invalid_span |= int(values[name] or "0") < 1
                    except ValueError:
                        self.invalid_span = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "table":
            self.table_depth -= 1


def _review_item_open(item: dict[str, Any], field_reviews: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
    """A review item only blocks the page while the question it recorded is still open.

    Document-level passes (word row coverage, quote-verified continuations, page-end reconciliation,
    numbering/label conventions) can settle a field after the item was written; a resolved item must
    not keep the page in CHECK, otherwise the status contradicts the fields it is derived from.
    """
    field = str(item.get("field") or "")
    if not field:
        return True
    if field == "document_relation":
        # the cross-page relation task stands for the page's table page-state: it is answered once
        # every table on the page has a settled cross_page_table review
        tables = [block for block in by_id.values() if block.get("type") == "table"]
        if not tables:
            return False
        return any(
            (field_reviews.get(block["block_id"]) or {}).get("cross_page_table", {}).get("status") != "PASS"
            for block in tables
        )
    target = item.get("target")
    if not isinstance(target, str) or not target or target not in by_id:
        return True
    review = (field_reviews.get(target) or {}).get(field)
    if not isinstance(review, dict):
        return True
    return review.get("status") != "PASS"


def validate_revision(
    page: dict[str, Any],
    revision: dict[str, Any],
    *,
    max_title_level: int = MAX_TITLE_LEVEL,
) -> dict[str, Any]:
    evidence = evidence_index(page)
    image_meta = page.get("page_image") or {}
    page_height = float(image_meta.get("height") or 0)
    errors: list[dict[str, Any]] = []
    block_ids: set[str] = set()
    coverage: Counter[str] = Counter()
    orders: Counter[int] = Counter()
    valid_blocks: list[dict[str, Any]] = []

    for raw in revision.get("blocks", []):
        try:
            block = validate_block(raw, page=page, max_title_level=max_title_level)
            valid_blocks.append(block)
        except Exception as exc:
            errors.append(_error("invalid_block", str(exc), block_ids=[raw.get("block_id", "")] if isinstance(raw, dict) else []))
            continue
        block_id = block["block_id"]
        if block_id in block_ids:
            errors.append(_error("duplicate_block_id", f"duplicate block_id {block_id}", block_ids=[block_id]))
        block_ids.add(block_id)
        orders[block["reading_order"]] += 1
        for ref in block.get("source_refs", []):
            coverage[ref] += 1
            item = evidence.get(ref)
            if item is None:
                errors.append(_error("unknown_evidence", f"unknown evidence {ref}", block_ids=[block_id], evidence_ids=[ref]))
            elif item.get("bbox_image_px") and not _contains(block["bbox_image_px"], item["bbox_image_px"]):
                if item.get("kind") != "pdf_image" or not _contains(item.get("effective_bbox_image_px", item["bbox_image_px"]), block["bbox_image_px"]):
                    errors.append(_error("bbox_does_not_cover_evidence", f"{block_id} does not cover {ref}", block_ids=[block_id], evidence_ids=[ref]))
        if block["type"] == "table":
            _validate_table_html(block, errors)
            _validate_embedded_images(block, evidence, errors)
        for embedded in block.get("embedded_images") or []:
            if embedded.get("image_ref") not in block.get("source_refs", []):
                errors.append(_error(
                    "embedded_image_claim_missing",
                    f"{block['block_id']} embeds {embedded.get('image_ref')} without claiming it as evidence",
                    block_ids=[block_id], evidence_ids=[str(embedded.get("image_ref"))],
                ))

    for evidence_id, item in evidence.items():
        if not item.get("required_coverage"):
            continue
        count = coverage[evidence_id]
        if count == 0:
            # A graphic that the inventory stage classified as a rule/divider (dropped on purpose)
            # is not a coverage failure: the annotators delete the same hairlines. The shape test
            # mirrors the one in inventory_rules.drop_non_blocks.
            exempt = False
            if item.get("kind") == "pdf_line":
                # short watermark echoes and stray glyphs are dropped on purpose (annotators do
                # the same); their unclaimed lines are not coverage failures. A repeated token
                # inside the line (the phone-number watermark pattern) marks it as print noise.
                text = str(item.get("text") or "").strip()
                if len(text) <= 2:
                    exempt = True
                else:
                    grams = [text[i:i + 4] for i in range(len(text) - 3)]
                    if grams and max(grams.count(g) for g in set(grams)) >= 3:
                        exempt = True
            if item.get("kind") == "pdf_image":
                box = [float(v) for v in (item.get("bbox_image_px") or item.get("effective_bbox_image_px") or [0, 0, 0, 0])]
                w, h = max(0.0, box[2] - box[0]), max(0.0, box[3] - box[1])
                if w > 0 and h > 0:
                    ratio = w / h
                    if (ratio >= 8 or ratio <= 0.125) and min(w, h) <= max(8.0, page_height * 0.03):
                        exempt = True
            if not exempt:
                errors.append(_error("unclaimed_evidence", f"required evidence is unclaimed: {evidence_id}", evidence_ids=[evidence_id]))
        elif count > 1:
            owners = [block["block_id"] for block in valid_blocks if evidence_id in block.get("source_refs", [])]
            errors.append(_error("duplicate_evidence_claim", f"required evidence is claimed {count} times: {evidence_id}", block_ids=owners, evidence_ids=[evidence_id]))

    # A picture belongs to exactly one block: a table that binds it as an embedded image and a
    # separate image block claiming the same evidence is the silent duplicate this invariant catches.
    image_owners: dict[str, list[str]] = {}
    for block in valid_blocks:
        for ref in block.get("source_refs", []):
            if evidence.get(ref, {}).get("kind") == "pdf_image":
                image_owners.setdefault(ref, []).append(block["block_id"])
    for ref, owners in sorted(image_owners.items()):
        if len(owners) > 1:
            errors.append(_error(
                "duplicate_image_claim",
                f"{ref} is claimed by {len(owners)} blocks: {', '.join(owners)}",
                block_ids=owners, evidence_ids=[ref],
            ))

    duplicate_orders = [order for order, count in orders.items() if count > 1]
    if duplicate_orders:
        errors.append(_error("duplicate_reading_order", f"duplicate reading orders: {duplicate_orders}"))
    if orders and sorted(orders) != list(range(len(valid_blocks))):
        errors.append(_error("reading_order_gap", f"reading orders must be 0..{len(valid_blocks)-1}: {sorted(orders)}"))

    plain_text = [block for block in valid_blocks if block.get("type") == "text"]
    for index, left in enumerate(plain_text):
        for right in plain_text[index + 1 :]:
            ioa = _overlap_ioa(left["bbox_image_px"], right["bbox_image_px"])
            if ioa >= 0.80:
                errors.append(_error(
                    "overlapping_text_blocks",
                    f"plain text blocks overlap by {ioa:.3f} of the smaller box",
                    block_ids=[left["block_id"], right["block_id"]],
                ))

    textual = [block for block in valid_blocks if block.get("type") in TEXT_TYPES]
    for block in textual:
        text = str(block.get("text") or "")
        if not text:
            continue
        noise = sum(1 for ch in text if unicodedata.category(ch) in {"Cc", "Cf", "Co", "Cn"} and ch not in "\n\t")
        if noise and noise / max(1, len(text)) > 0.02:
            errors.append(_error(
                "glyph_noise_text",
                f"{block['block_id']} text carries {noise} non-content glyph codes (broken font map)",
                block_ids=[block["block_id"]],
            ))
    for index, left in enumerate(textual):
        for right in textual[index + 1 :]:
            if left.get("type") == right.get("type") == "text":
                continue
            left_text = _visible_text_norm(str(left.get("text", "")))
            right_text = _visible_text_norm(str(right.get("text", "")))
            ioa = _overlap_ioa(left["bbox_image_px"], right["bbox_image_px"])
            if left_text and left_text == right_text and ioa >= 0.80:
                errors.append(_error(
                    "duplicate_overlapping_textual_blocks",
                    f"textual blocks with the same visible text overlap by {ioa:.3f}",
                    block_ids=[left["block_id"], right["block_id"]],
                ))

    by_id = {block["block_id"]: block for block in valid_blocks}
    for block in valid_blocks:
        if block["type"] not in CAPTION_TYPES:
            continue
        parent = by_id.get(block["parent_id"])
        expected = {"table"} if block["type"] == "table_caption" else {"image", "chart"}
        if parent is None or parent.get("type") not in expected:
            errors.append(_error("invalid_caption_parent", f"{block['block_id']} parent must be one of {sorted(expected)}", block_ids=[block["block_id"], block.get("parent_id", "")]))

    field_reviews = revision.get("field_reviews", {}).get("blocks", {})
    unresolved_fields = []
    for block in valid_blocks:
        reviews = field_reviews.get(block["block_id"], {})
        required = {"type", "bbox"}
        if block["type"] in TEXT_TYPES:
            required.add("text")
        if block["type"] == "title":
            required.add("level")
        if block["type"] == "table":
            required.add("html")
        if block["type"] in CAPTION_TYPES:
            required.add("parent_id")
        for field in required:
            if reviews.get(field, {}).get("status") != "PASS":
                unresolved_fields.append({"block_id": block["block_id"], "field": field, "status": reviews.get(field, {}).get("status", "missing")})

    review_items = revision.get("review_items", [])
    open_review_items = [item for item in review_items if _review_item_open(item, field_reviews, by_id)]
    status = "CHECK" if errors or open_review_items or unresolved_fields else "PASS"
    return {
        "status": status,
        "error_count": len(errors),
        "review_count": len(open_review_items),
        "resolved_review_count": len(review_items) - len(open_review_items),
        "unresolved_field_count": len(unresolved_fields),
        "unresolved_fields": unresolved_fields,
        "required_evidence": sum(1 for item in evidence.values() if item.get("required_coverage")),
        "claimed_required_evidence": sum(1 for key, item in evidence.items() if item.get("required_coverage") and coverage[key] == 1),
        "errors": errors,
    }


def _validate_embedded_images(block: dict[str, Any], evidence: dict[str, dict[str, Any]], errors: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for image in block.get("embedded_images", []):
        ref = image.get("image_ref") if isinstance(image, dict) else None
        item = evidence.get(ref, {})
        bbox = image.get("bbox_image_px") if isinstance(image, dict) else None
        valid_bbox = isinstance(bbox, list) and len(bbox) == 4
        valid_position = all(isinstance(image.get(name), int) and image[name] >= (1 if name in {"rowspan", "colspan"} else 0) for name in ("row", "col", "rowspan", "colspan")) if isinstance(image, dict) else False
        if not ref or ref in seen or item.get("kind") != "pdf_image" or not valid_bbox or bbox != item.get("effective_bbox_image_px", item.get("bbox_image_px")) or not _contains(block["bbox_image_px"], bbox) or not valid_position or f'data-image-ref="{ref}"' not in block.get("html", ""):
            errors.append(_error("invalid_embedded_table_image", f"{block['block_id']} has invalid embedded image {ref}", block_ids=[block["block_id"]], evidence_ids=[ref] if ref else []))
        seen.add(ref)


def _validate_table_html(block: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    parser = _TableParser()
    try:
        parser.feed(block["html"])
        parser.close()
    except Exception as exc:
        errors.append(_error("invalid_table_html", str(exc), block_ids=[block["block_id"]]))
        return
    if parser.tables != 1 or parser.table_depth != 0 or parser.rows < 1 or parser.cells < 1 or parser.invalid_span:
        errors.append(_error("invalid_table_html", f"table={parser.tables}, rows={parser.rows}, cells={parser.cells}, invalid_span={parser.invalid_span}", block_ids=[block["block_id"]]))
