from __future__ import annotations

import difflib
import hashlib
import html
import json
import math
import re
import sys
import time
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

try:  # tolerant HTML parsing for model tables that contain void <img> tokens
    from lxml import html as _lxml_html
except Exception:  # pragma: no cover
    _lxml_html = None

from PIL import Image

from .constants import FIELD_REVIEW_VERSION, MAX_TITLE_LEVEL, SCHEMA_VERSION, TEXT_TYPES
from .jsonio import write_json_atomic
from .inventory_rules import apply_inventory_rules
from .reading_order import build_page_order
from .schemas import add_coordinate_views
from .word_reader import match_table_rows, normalize as normalize_word_text, read_word_tables, read_word_text, table_fragment_html

MINERU_TYPE_MAP = {
    "text": "text",
    "list": "text",
    "table": "table",
    "image": "image",
    "chart": "chart",
    "equation": "formula",
    "header": "header",
    "footer": "footer",
    "page_number": "page_number",
    "page_footnote": "footnote",
    "code": "text",
    "aside_text": "text",
    "ref_text": "text",
}

PADDLE_TYPE_MAP = {
    "text": "text",
    "content": "text",
    "paragraph_title": "title",
    "doc_title": "title",
    "table": "table",
    "image": "image",
    "chart": "chart",
    "seal": "seal",
    "display_formula": "formula",
    "inline_formula": "formula",
    "header": "header",
    "header_image": "header",
    "footer": "footer",
    "footer_image": "footer",
    "number": "page_number",
    "footnote": "footnote",
    "vision_footnote": "footnote",
    "figure_title": "caption_candidate",
    "algorithm": "text",
    "aside_text": "text",
    "vertical_text": "text",
    "abstract": "text",
    "reference": "text",
    "reference_content": "text",
    "formula_number": "text",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.sub(r"\\[a-zA-Z]+", " ", value)
    value = re.sub(r"[$^_{}~°]", " ", value)
    return re.sub(r"\s+", "", value).casefold()


def _duplicate_norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\\times", "×", value, flags=re.IGNORECASE)
    value = re.sub(r"\\([_`*#{}\[\]()<>+.!-])", r"\1", value)
    value = re.sub(r"[xX×]+", "<placeholder>", value)
    value = re.sub(r"[\s$.,．，。:：;；]+", "", value).casefold()
    return re.sub(r"(?:<placeholder>)+", "<placeholder>", value)


_CJK_RANGES = (
    (0x2E80, 0x303F),   # CJK punctuation
    (0x3040, 0x30FF),   # kana
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xAC00, 0xD7AF),   # hangul
    (0xF900, 0xFAFF),   # CJK compatibility
    (0xFF00, 0xFFEF),   # fullwidth forms
)


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def _needs_space(left: str, right: str) -> bool:
    """True when two text fragments were separated by a space the PDF no longer reports.

    A line is stored as one string per *span*, and a word-processed or vector PDF splits a Latin line
    into many spans (one per style run, sometimes one per word). Joining them with ``""`` glues the
    words together ("Toachieveabalance"), which then poisons the text GT. CJK needs no separator, and
    punctuation binds to the fragment before it.
    """
    if not left or not right:
        return False
    if left[-1].isspace() or right[0].isspace():
        return False
    if _is_cjk(left[-1]) or _is_cjk(right[0]):
        return False
    if right[0] in ".,;:!?)]}%’”\"'-/":
        return False
    if left[-1] in "([{“‘":
        return False
    return True


def _join_parts(parts: "list[str] | tuple[str, ...]") -> str:
    """Join text fragments, restoring the spaces a span split removed."""
    out = ""
    for part in parts:
        value = str(part or "")
        if not value:
            continue
        if _needs_space(out, value):
            out += " "
        out += value
    return out


_GLYPH_NOISE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ue000-\uf8ff\ufffd\u200b-\u200f\ufeff]")


def _sanitize_text(value: Any) -> str:
    """Strip glyph codes that carry no text (symbol fonts, broken font encodings).

    PDF text layers routinely spell bullet/symbol fonts as control codes or private-use glyphs
    (``\uf06e`` for a Wingdings bullet, ``\x02\x03`` for a broken CID map). They are not content:
    they must never reach a revision, where they would look like text and poison downstream
    comparisons.
    """
    text = str(value or "")
    if not text:
        return ""
    cleaned = _GLYPH_NOISE.sub("", text)
    # Removing a glyph code can leave the two spaces it sat between ("S  x"); one is enough and the
    # collapsed form compares equal to the model reading.
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def _text_is_unusable(value: Any) -> bool:
    """True when a PDF text layer entry is font noise rather than readable text.

    Some PDFs (old journal typesetting, broken ToUnicode maps) extract their whole text layer as
    control codes. That text cannot verify or correct anything, and adopting it overwrites the
    model's correct reading of the page.
    """
    text = str(value or "")
    if not text.strip():
        return False
    cleaned = _sanitize_text(text)
    if not cleaned.strip():
        return True
    return (len(text) - len(cleaned)) / max(1, len(text)) > 0.05


def _lines_text(lines: list[dict[str, Any]]) -> str:
    """Text of the usable lines only; junk lines contribute nothing."""
    keep = [str(line.get("text", "")) for line in lines if _sanitize_text(line.get("text")).strip()]
    return "\n".join(keep)


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value or ""))


def _union(boxes: list[list[float]]) -> list[float]:
    return [
        round(min(box[0] for box in boxes), 3),
        round(min(box[1] for box in boxes), 3),
        round(max(box[2] for box in boxes), 3),
        round(max(box[3] for box in boxes), 3),
    ]


def _intersection(a: list[float], b: list[float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _box_area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _iou(a: list[float], b: list[float]) -> float:
    inter = _intersection(a, b)
    union = _box_area(a) + _box_area(b) - inter
    return inter / union if union > 0 else 0.0


def _ioa(inner: list[float], outer: list[float]) -> float:
    area = max(1.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return _intersection(inner, outer) / area


def _text_similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return min(len(na), len(nb)) / max(len(na), len(nb))
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _mineru_text(item: dict[str, Any]) -> str:
    if item.get("type") == "list":
        return "\n".join(str(value) for value in item.get("list_items", []))
    if item.get("type") == "table":
        return _strip_html(str(item.get("table_body", "")))
    return str(item.get("text") or item.get("content") or "")


def _paddle_text(item: dict[str, Any]) -> str:
    value = str(item.get("block_content", ""))
    return _strip_html(value) if item.get("block_label") == "table" else value


def _clip_pdf_box(box: list[float], width_pt: float, height_pt: float) -> tuple[list[float], bool] | None:
    if len(box) != 4 or not all(isinstance(value, (int, float)) for value in box):
        return None
    x1, y1, x2, y2 = [float(value) for value in box]
    clipped = [max(0.0, min(width_pt, x1)), max(0.0, min(height_pt, y1)), max(0.0, min(width_pt, x2)), max(0.0, min(height_pt, y2))]
    if clipped[2] - clipped[0] <= 0.5 or clipped[3] - clipped[1] <= 0.5:
        return None
    return clipped, clipped != [x1, y1, x2, y2]


def _image_box(pdf_box: list[float], sx: float, sy: float) -> list[float]:
    return [round(pdf_box[0] * sx, 3), round(pdf_box[1] * sy, 3), round(pdf_box[2] * sx, 3), round(pdf_box[3] * sy, 3)]


def _pdf_facts(pdf_path: Path, page_number: int, image_width: int, image_height: int) -> tuple[dict[str, float], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    import pymupdf

    document = pymupdf.open(pdf_path)
    try:
        page = document[page_number - 1]
        width_pt, height_pt = float(page.rect.width), float(page.rect.height)
        sx, sy = image_width / width_pt, image_height / height_pt
        raw = page.get_text("dict", sort=True)
        lines = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [span for span in line.get("spans", []) if str(span.get("text", "")).strip()]
                if not spans:
                    continue
                box_result = _clip_pdf_box(list(line["bbox"]), width_pt, height_pt)
                if box_result is None:
                    continue
                box, clipped = box_result
                raw_text = _join_parts([str(span.get("text", "")) for span in spans])
                lines.append({
                    "kind": "pdf_line",
                    "bbox_pdf_pt": [round(float(v), 4) for v in box],
                    "bbox_image_px": _image_box(box, sx, sy),
                    "clipped_to_page": clipped,
                    "text": raw_text,
                    "font_size": round(max(float(span.get("size", 0)) for span in spans), 3),
                    "bold": any("bold" in str(span.get("font", "")).lower() for span in spans),
                    "required_coverage": True,
                })
        images = []
        for info in page.get_image_info(xrefs=True):
            box_result = _clip_pdf_box(list(info.get("bbox", [])), width_pt, height_pt)
            if box_result is None:
                continue
            box, clipped = box_result
            images.append({
                "kind": "pdf_image",
                "bbox_pdf_pt": [round(float(v), 4) for v in box],
                "bbox_image_px": _image_box(box, sx, sy),
                "clipped_to_page": clipped,
                "xref": info.get("xref"),
                "required_coverage": True,
            })
        drawings = []
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is None:
                continue
            x1, y1, x2, y2 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
            if x2 - x1 <= 0.5:
                x1, x2 = max(0.0, x1 - 0.5), min(width_pt, x2 + 0.5)
            if y2 - y1 <= 0.5:
                y1, y2 = max(0.0, y1 - 0.5), min(height_pt, y2 + 0.5)
            box_result = _clip_pdf_box([x1, y1, x2, y2], width_pt, height_pt)
            if box_result is None:
                continue
            box, clipped = box_result
            drawings.append({
                "kind": "pdf_drawing",
                "bbox_pdf_pt": [round(value, 4) for value in box],
                "bbox_image_px": _image_box(box, sx, sy),
                "clipped_to_page": clipped,
                "item_count": len(drawing.get("items", [])),
                "closed": bool(drawing.get("closePath")),
                "required_coverage": False,
            })
    finally:
        document.close()
    return {"width_pt": width_pt, "height_pt": height_pt}, lines, images, drawings


def _load_mineru(path: Path, page_number: int, image_width: int, image_height: int) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for index, item in enumerate(value for value in data if value.get("page_idx") == page_number - 1):
        box = item.get("bbox")
        if not isinstance(box, list) or len(box) != 4 or min(box) < 0 or max(box) > 1000:
            continue
        raw_type = str(item.get("type", ""))
        mapped = MINERU_TYPE_MAP.get(raw_type, "unknown")
        level = item.get("text_level")
        if raw_type == "text" and isinstance(level, int) and 1 <= level <= MAX_TITLE_LEVEL:
            mapped = "title"
        result.append({
            "evidence_id": f"mineru.block.p{page_number}.{index:04d}",
            "kind": "model_block",
            "model": "mineru",
            "source_type": raw_type,
            "type_candidate": mapped,
            "level_candidate": level if mapped == "title" and isinstance(level, int) else None,
            "bbox_image_px": [round(box[0] / 1000 * image_width, 3), round(box[1] / 1000 * image_height, 3), round(box[2] / 1000 * image_width, 3), round(box[3] / 1000 * image_height, 3)],
            "text": _mineru_text(item),
            "list_items": [str(value) for value in item.get("list_items", [])] if raw_type == "list" else None,
            "html": item.get("table_body") if raw_type == "table" else None,
            "required_coverage": False,
        })
    return result


def _match_list_item_lines(text: str, lines: list[dict[str, Any]], used: set[str]) -> list[dict[str, Any]]:
    available = [line for line in lines if line["evidence_id"] not in used and _norm(line.get("text", ""))]
    best: tuple[float, list[dict[str, Any]]] = (0.0, [])
    for start in range(len(available)):
        for length in range(1, min(6, len(available) - start) + 1):
            group = available[start : start + length]
            score = _text_similarity(text, "".join(line["text"] for line in group))
            if score > best[0]:
                best = (score, group)
    return best[1] if best[0] >= 0.45 else []


def _expand_mineru_lists(records: list[dict[str, Any]], pdf_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for record in records:
        items = record.get("list_items") if record.get("source_type") == "list" else None
        if not isinstance(items, list) or not items:
            record.pop("list_items", None)
            expanded.append(record)
            continue
        lines = _lines_in_box(pdf_lines, record["bbox_image_px"])
        used: set[str] = set()
        for index, text in enumerate(items):
            matched = _match_list_item_lines(text, lines, used)
            used.update(line["evidence_id"] for line in matched)
            if matched:
                box = _union([line["bbox_image_px"] for line in matched])
            else:
                x1, y1, x2, y2 = record["bbox_image_px"]
                step = (y2 - y1) / len(items)
                box = [x1, round(y1 + step * index, 3), x2, round(y1 + step * (index + 1), 3)]
            item = {key: value for key, value in record.items() if key != "list_items"}
            item["evidence_id"] = f"{record['evidence_id']}.item{index:03d}"
            item["source_type"] = "list_item"
            item["type_candidate"] = "text"
            item["bbox_image_px"] = box
            item["text"] = text
            expanded.append(item)
    return expanded


def _load_paddle(path: Path, page_number: int, image_width: int, image_height: int) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("width"), data.get("height")) != (image_width, image_height):
        raise ValueError(f"Paddle dimensions do not match page image: {path}")
    result = []
    for index, item in enumerate(data.get("parsing_res_list", [])):
        box = item.get("block_bbox")
        if not isinstance(box, list) or len(box) != 4:
            continue
        raw_type = str(item.get("block_label", ""))
        result.append({
            "evidence_id": f"paddle.block.p{page_number}.{index:04d}",
            "kind": "model_block",
            "model": "paddle",
            "source_type": raw_type,
            "type_candidate": PADDLE_TYPE_MAP.get(raw_type, "unknown"),
            "bbox_image_px": [float(v) for v in box],
            "text": _paddle_text(item),
            "html": item.get("block_content") if raw_type == "table" else None,
            "required_coverage": False,
        })
    return result


def _cluster(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, left in enumerate(records):
        for j in range(i + 1, len(records)):
            right = records[j]
            overlap = _iou(left["bbox_image_px"], right["bbox_image_px"])
            table_authority = (
                (left.get("type_candidate") == "table" and isinstance(left.get("html"), str) and "<table" in left["html"].lower())
                or (right.get("type_candidate") == "table" and isinstance(right.get("html"), str) and "<table" in right["html"].lower())
            )
            similarity = _text_similarity(left.get("text", ""), right.get("text", ""))
            same_nontext = left["type_candidate"] == right["type_candidate"] and left["type_candidate"] in {"table", "image", "chart", "seal"}
            if left["model"] == right["model"]:
                continue
            table_contains_fragment = (
                (left.get("type_candidate") == "table" and _ioa(right["bbox_image_px"], left["bbox_image_px"]) >= 0.75)
                or (right.get("type_candidate") == "table" and _ioa(left["bbox_image_px"], right["bbox_image_px"]) >= 0.75)
            )
            if (overlap >= 0.18 or table_contains_fragment) and (similarity >= 0.35 or same_nontext or (table_authority and table_contains_fragment)):
                union(i, j)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        groups.setdefault(find(index), []).append(record)
    return list(groups.values())


def _lines_in_box(lines: list[dict[str, Any]], box: list[float]) -> list[dict[str, Any]]:
    return [line for line in lines if _ioa(line["bbox_image_px"], box) >= 0.55]


def _base_score(record: dict[str, Any], lines: list[dict[str, Any]]) -> float:
    inside = _lines_in_box(lines, record["bbox_image_px"])
    page_text = "\n".join(line["text"] for line in inside)
    similarity = _text_similarity(record.get("text", ""), page_text)
    geometry = min(1.0, len(inside) / 2) if record["type_candidate"] not in {"image", "chart", "seal"} else 0.5
    return 0.8 * similarity + 0.2 * geometry


def _cjk_content(value: Any) -> str:
    """CJK characters of a text, ignoring latin runs that models split or decorate differently."""
    return "".join(character for character in _norm(value) if "\u4e00" <= character <= "\u9fff")


def _cjk_coverage(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    shared = sum((Counter(left) & Counter(right)).values())
    return min(shared / len(left), shared / len(right))


def _marginal_band_line(
    block: dict[str, Any], pdf_lines: list[dict[str, Any]], page: dict[str, Any], claimed: set[str]
) -> dict[str, Any] | None:
    """A header/footer line the block shares with other content.

    Page furniture is often one PDF line carrying more than the block holds (a company name plus a
    document number, or a logo string plus the section). The line then lies outside the model block
    box, line selection finds nothing, and the block is reported as unverified text even though the
    page proves it. Accept the line whose CJK content matches the block's in the same band; the
    block box grows over the measured line, so the geometry stays evidence-backed.
    """
    block_text = _cjk_content(block.get("text", ""))
    if len(block_text) < 2:
        return None
    box = block.get("bbox_image_px")
    if not isinstance(box, list) or len(box) != 4:
        return None
    height = float(page.get("page_image", {}).get("height", 0) or 0)
    if height > 0 and box[1] > height * 0.25 and box[3] < height * 0.75:
        return None
    best: dict[str, Any] | None = None
    best_score = 0.0
    for line in pdf_lines:
        if line.get("evidence_id") in claimed:
            continue
        line_box = line.get("bbox_image_px")
        if not isinstance(line_box, list) or len(line_box) != 4:
            continue
        vertical = min(box[3], line_box[3]) - max(box[1], line_box[1])
        if vertical < min(box[3] - box[1], line_box[3] - line_box[1]) * 0.5:
            continue
        horizontal = min(box[2], line_box[2]) - max(box[0], line_box[0])
        if horizontal < min(box[2] - box[0], line_box[2] - line_box[0]) * 0.4:
            continue
        score = _cjk_coverage(block_text, _cjk_content(line.get("text", "")))
        if score >= 0.6 and score > best_score:
            best, best_score = line, score
    return best


def _select_lines(record: dict[str, Any], lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inside = _lines_in_box(lines, record["bbox_image_px"])
    if record.get("source_type") == "list_item":
        return inside
    target = _norm(record.get("text", ""))
    if not target or record["type_candidate"] == "table":
        return inside
    matched = [line for line in inside if _norm(line["text"]) in target or target in _norm(line["text"])]
    return matched or inside


def _is_page_scan(image: dict[str, Any], page: dict[str, Any]) -> bool:
    box = image["bbox_image_px"]
    width, height = page["page_image"]["width"], page["page_image"]["height"]
    area_ratio = (box[2] - box[0]) * (box[3] - box[1]) / (width * height)
    return area_ratio >= 0.72 and box[0] <= width * 0.04 and box[1] <= height * 0.04 and box[2] >= width * 0.96 and box[3] >= height * 0.96


def _mask_pdf_images(page: dict[str, Any], models: list[dict[str, Any]], pdf_lines: list[dict[str, Any]], pdf_images: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    content_images = []
    for image in pdf_images:
        if _is_page_scan(image, page):
            image["required_coverage"] = False
            image["page_scan"] = True
        else:
            image["effective_bbox_image_px"] = _consensus_image_box(
                image,
                [item.get("evidence_id", "") for item in models],
                models,
            )
            content_images.append(image)
    nested_by_image: dict[str, list[str]] = {image["evidence_id"]: [] for image in content_images}
    visible_models = []
    for model in models:
        owner = next((image for image in content_images if _inside_pdf_image_with_ocr_tolerance(model["bbox_image_px"], image["bbox_image_px"])), None)
        if owner is None:
            visible_models.append(model)
        else:
            nested_by_image[owner["evidence_id"]].append(model["evidence_id"])
            model["inside_pdf_image_ref"] = owner["evidence_id"]
    visible_lines = []
    for line in pdf_lines:
        owner = next((image for image in content_images if _ioa(line["bbox_image_px"], image["bbox_image_px"]) >= 0.8), None)
        if owner is None:
            visible_lines.append(line)
        else:
            line["required_coverage"] = False
            line["inside_pdf_image_ref"] = owner["evidence_id"]
    return visible_models, visible_lines, content_images, nested_by_image


def _inside_pdf_image_with_ocr_tolerance(box: list[float], image_box: list[float]) -> bool:
    width, height = image_box[2] - image_box[0], image_box[3] - image_box[1]
    tolerance = min(12.0, max(4.0, min(width, height) * 0.02))
    expanded = [image_box[0] - tolerance, image_box[1] - tolerance, image_box[2] + tolerance, image_box[3] + tolerance]
    return _ioa(box, image_box) >= 0.50 and _ioa(box, expanded) >= 0.90


def _drop_unsupported_image_regions(models: list[dict[str, Any]], pdf_lines: list[dict[str, Any]], pdf_images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = []
    for item in models:
        box = item["bbox_image_px"]
        model_area = max(1.0, _box_area(box))
        image_contributions = [_intersection(image["bbox_image_px"], box) / model_area for image in pdf_images]
        covers_multiple_images = sum(value >= 0.15 for value in image_contributions) >= 2 and sum(image_contributions) >= 0.75
        if item.get("type_candidate") == "table" and covers_multiple_images:
            item["rejected_as_table"] = "single_model_container_of_multiple_pdf_images"
            continue
        if item.get("type_candidate") != "image":
            kept.append(item)
            continue
        if covers_multiple_images:
            item["rejected_as_image"] = "single_model_container_of_multiple_pdf_images"
            continue
        has_pdf_image = any(_iou(box, image["bbox_image_px"]) >= 0.20 for image in pdf_images)
        text_lines = [line for line in pdf_lines if _ioa(line["bbox_image_px"], box) >= 0.75 and _norm(line.get("text", ""))]
        other_text = [other for other in models if other.get("model") != item.get("model") and other.get("type_candidate") in TEXT_TYPES and _ioa(other["bbox_image_px"], box) >= 0.35]
        if not has_pdf_image and len(text_lines) >= 2 and other_text:
            item["rejected_as_image"] = "pdf_native_text_plus_cross_model_text"
            continue
        kept.append(item)
    return kept


def _drop_redundant_text_containers(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dropped: set[str] = set()
    for outer in models:
        if outer.get("type_candidate") not in TEXT_TYPES or not _norm(outer.get("text", "")):
            continue
        outer_text = _norm(outer["text"])
        children = [
            inner for inner in models
            if inner.get("model") != outer.get("model")
            and (inner.get("source_type") == "list_item" or inner.get("type_candidate") == "title")
            and _ioa(inner["bbox_image_px"], outer["bbox_image_px"]) >= 0.8
            and _norm(inner.get("text", ""))
            and _norm(inner["text"]) in outer_text
        ]
        if len(children) >= 3:
            dropped.add(outer["evidence_id"])
    return [item for item in models if item["evidence_id"] not in dropped]


def _bind_list_items(models: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    list_items = [item for item in models if item.get("source_type") == "list_item"]
    consumed: set[str] = set()
    support: dict[str, list[str]] = {}
    for item in list_items:
        matches = []
        for other in models:
            if other.get("model") == item.get("model") or other.get("source_type") == "list_item":
                continue
            overlap = _ioa(item["bbox_image_px"], other["bbox_image_px"])
            similarity = _text_similarity(item.get("text", ""), other.get("text", ""))
            if overlap >= 0.75 and similarity >= 0.45:
                matches.append((similarity, overlap, other))
        if matches:
            match = max(matches, key=lambda value: (value[0], value[1]))[2]
            support[item["evidence_id"]] = [match["evidence_id"]]
            consumed.add(match["evidence_id"])
    return [item for item in models if item["evidence_id"] not in consumed], support


def _suppress_page_number_lines(models: list[dict[str, Any]], pdf_lines: list[dict[str, Any]], page: dict[str, Any]) -> None:
    page_number_models = [item for item in models if item.get("type_candidate") == "page_number"]
    if not page_number_models:
        return
    height = page["page_image"]["height"]
    for line in pdf_lines:
        box = line["bbox_image_px"]
        if box[1] < height * 0.86:
            continue
        normalized = re.sub(r"[^0-9A-Za-z]", "", str(line.get("text", "")))
        if not normalized.isdigit():
            continue
        nearby = any(
            _ioa(box, item["bbox_image_px"]) >= 0.05
            or (abs(box[1] - item["bbox_image_px"][1]) < max(12, (box[3] - box[1]) * 0.6)
                and abs((box[0] + box[2]) / 2 - (item["bbox_image_px"][0] + item["bbox_image_px"][2]) / 2) < page["page_image"]["width"] * 0.15)
            for item in page_number_models
        )
        if nearby:
            line["required_coverage"] = False
            line["inside_page_number"] = True


def _build_candidates(page: dict[str, Any], models: list[dict[str, Any]], pdf_lines: list[dict[str, Any]], pdf_images: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    blocks = []
    reviews: dict[str, Any] = {"schema_version": FIELD_REVIEW_VERSION, "blocks": {}}
    issues = []
    claimed_lines: set[str] = set()
    claimed_images: set[str] = set()

    all_models = list(models)
    models, pdf_lines, content_images, nested_by_image = _mask_pdf_images(page, models, pdf_lines, pdf_images)
    models = _drop_unsupported_image_regions(models, pdf_lines, content_images)
    models = _drop_redundant_text_containers(models)
    models, list_support = _bind_list_items(models)
    _suppress_page_number_lines(models, pdf_lines, page)
    block_serial = iter(range(10000))
    clusters = _cluster(models)
    clusters.sort(key=lambda group: (min(item["bbox_image_px"][1] for item in group), min(item["bbox_image_px"][0] for item in group)))
    for group in clusters:
        base = max(group, key=lambda item: (_base_score(item, pdf_lines), len(_norm(item.get("text", "")))))
        mapped = {item["type_candidate"] for item in group if item["type_candidate"] not in {"unknown", "caption_candidate"}}
        table_html_items = [item for item in group if item.get("type_candidate") == "table" and isinstance(item.get("html"), str) and "<table" in item["html"].lower()]
        caption_candidate = any(item["type_candidate"] == "caption_candidate" for item in group)
        if table_html_items and not caption_candidate:
            final_type = "table"
            base = max(table_html_items, key=lambda item: _base_score(item, pdf_lines))
        else:
            final_type = next(iter(mapped)) if len(mapped) == 1 and not caption_candidate else "unknown"
        if caption_candidate:
            final_type = "unknown"
        selected_lines = [line for line in _select_lines(base, pdf_lines) if line["evidence_id"] not in claimed_lines]
        marginal_band = False
        if not selected_lines and final_type in {"header", "footer", "page_number"}:
            band_line = _marginal_band_line(base, pdf_lines, page, claimed_lines)
            if band_line is not None:
                selected_lines, marginal_band = [band_line], True
        source_refs: list[str] = []
        final_box = list(base["bbox_image_px"])
        text = base.get("text", "")
        html_value = None

        if final_type in TEXT_TYPES or final_type in {"unknown", "table"}:
            if selected_lines:
                source_refs = [line["evidence_id"] for line in selected_lines]
                claimed_lines.update(source_refs)
                if final_type != "table":
                    pdf_text_unusable = not _lines_text(selected_lines)
                    text = _lines_text(selected_lines) or str(base.get("text") or "")
                    final_box = _union([line["bbox_image_px"] for line in selected_lines])
        if final_type == "table" and selected_lines:
            final_box = _union([base["bbox_image_px"]] + [line["bbox_image_px"] for line in selected_lines])
        if final_type == "table" and table_html_items:
            pdf_fragment = _join_parts([str(line.get("text", "")) for line in selected_lines])
            best_table = max(table_html_items, key=lambda item: _table_html_match_score(str(item.get("html", "")), pdf_fragment))
            html_value = best_table["html"]
            text = _lines_text(selected_lines) or str(base.get("text") or "")
        graphic_region = False
        pdf_text_unusable = bool(source_refs) and not _lines_text(selected_lines)
        if final_type in {"image", "chart", "seal"}:
            image_matches = [image for image in pdf_images if image["evidence_id"] not in claimed_images and _iou(image["bbox_image_px"], base["bbox_image_px"]) >= 0.25]
            if len(image_matches) == 1:
                source_refs = [image_matches[0]["evidence_id"]]
                claimed_images.add(image_matches[0]["evidence_id"])
                final_box = list(image_matches[0]["bbox_image_px"])
            else:
                # A diagram is not one picture in the file: its region is carried by several embedded
                # pictures and vector drawings, so it is grounded by that graphic region as a whole.
                graphic_refs = _graphic_region_refs(page, base["bbox_image_px"])
                if graphic_refs:
                    source_refs = graphic_refs
                    graphic_region = True

        block_id = f"candidate.block.p{page['page']}.{next(block_serial):04d}"
        support_refs = [item["evidence_id"] for item in group]
        for item in group:
            support_refs.extend(list_support.get(item["evidence_id"], []))
        block: dict[str, Any] = {
            "block_id": block_id,
            "type": final_type,
            "bbox_image_px": final_box,
            "reading_order": len(blocks),
            "source_refs": source_refs,
            "support_refs": list(dict.fromkeys(support_refs)),
            "type_candidates": sorted(mapped | ({"table_caption", "image_caption", "title", "text"} if caption_candidate else set())),
            "issues": [],
        }
        # Typography is the evidence the type decision was missing: a heading differs from body text by
        # font size, weight and alignment, and the PDF text layer already carries it per span. It lives
        # in a page-level map (not on the block) so the block schema stays unchanged.
        sizes = [float(line.get("font_size") or 0) for line in selected_lines if line.get("font_size")]
        typography: dict[str, Any] = {}
        if sizes:
            ordered = sorted(sizes)
            typography["font_size"] = round(ordered[len(ordered) // 2], 2)
        if selected_lines:
            typography["bold"] = any(bool(line.get("bold")) for line in selected_lines)
            typography["line_count"] = len(selected_lines)
            left = min(float(line["bbox_image_px"][0]) for line in selected_lines)
            right = max(float(line["bbox_image_px"][2]) for line in selected_lines)
            typography["width"] = round(right - left, 1)
            typography["left"] = round(left, 1)
        if typography:
            page.setdefault("typography", {})[block_id] = typography
        if final_type in TEXT_TYPES or final_type == "unknown":
            block["text"] = text
        if final_type == "title":
            levels = {item.get("level_candidate") for item in group if isinstance(item.get("level_candidate"), int)}
            block["level"] = next(iter(levels)) if len(levels) == 1 else 1
            block["issues"].append("title_level_review")
        if final_type == "table":
            if isinstance(html_value, str) and "<table" in html_value.lower():
                block["html"] = html_value
                block["text"] = text
            else:
                block["type"] = "unknown"
                block["text"] = "\n".join(line["text"] for line in selected_lines)
                block["issues"].append("table_html_missing")
        if final_type == "unknown":
            block["issues"].append("type_conflict")
        add_coordinate_views(block, page)
        blocks.append(block)
        type_models = {item.get("model") for item in group if item.get("type_candidate") == final_type}
        grounded_text_item = final_type == "text" and any(item.get("source_type") == "list_item" for item in group) and bool(source_refs)
        type_pass = final_type != "unknown" and (len(type_models) >= 2 or grounded_text_item)
        reviews["blocks"][block_id] = {
            "type": {"status": "PASS" if type_pass else "CHECK", "method": "cross_model_type_agreement" if len(type_models) >= 2 else "grounded_text_item" if grounded_text_item else "single_model_type", "evidence_refs": block["support_refs"]},
            "bbox": {
                "status": "PASS" if source_refs else "CHECK",
                "method": "marginal_band_pdf_text_contained" if marginal_band else "pdf_graphic_region_bbox" if graphic_region else "model_block_pdf_snap" if selected_lines else "model_bbox_unverified",
                "base_bbox_ref": base["evidence_id"],
                "evidence_refs": source_refs,
            },
        }
        if block["type"] in TEXT_TYPES or block["type"] == "unknown":
            if pdf_text_unusable:
                reviews["blocks"][block_id]["text"] = {"status": "CHECK", "method": "pdf_text_unusable_model_text_kept", "evidence_refs": source_refs}
            else:
                reviews["blocks"][block_id]["text"] = {"status": "PASS" if selected_lines else "CHECK", "method": "marginal_band_pdf_text_contained" if marginal_band else "pdf_text_exact" if selected_lines else "model_text_unverified", "evidence_refs": source_refs}
        if block["type"] == "title":
            reviews["blocks"][block_id]["level"] = {"status": "CHECK", "method": "model_level_candidate", "evidence_refs": block["support_refs"]}
        if block["type"] == "table":
            reviews["blocks"][block_id]["html"] = {"status": "CHECK", "method": "model_html_candidate", "evidence_refs": block["support_refs"]}
            if source_refs:
                reviews["blocks"][block_id]["text"] = ({"status": "CHECK", "method": "pdf_text_unusable_model_text_kept", "evidence_refs": source_refs} if pdf_text_unusable else {"status": "PASS", "method": "pdf_text_exact", "evidence_refs": source_refs})
        if block["issues"]:
            issues.append({
                "issue_id": f"issue.p{page['page']}.{len(issues):04d}",
                "type": block["issues"][0],
                "block_ids": [block_id],
            })

    _suppress_compound_pdf_lines(blocks, reviews, pdf_lines, all_models, claimed_lines)
    _suppress_partitioned_pdf_lines(blocks, reviews, pdf_lines, all_models, claimed_lines)
    _attach_inline_orphan_pdf_lines(blocks, reviews, pdf_lines, claimed_lines, page)
    orphan_lines = [line for line in pdf_lines if line.get("required_coverage") and line["evidence_id"] not in claimed_lines]
    for group in _group_orphan_lines(orphan_lines):
        refs = [line["evidence_id"] for line in group]
        text = "\n".join(line["text"] for line in group)
        block_id = f"candidate.block.p{page['page']}.{next(block_serial):04d}"
        block = {
            "block_id": block_id,
            "type": "unknown",
            "bbox_image_px": _union([line["bbox_image_px"] for line in group]),
            "text": text,
            "reading_order": len(blocks),
            "source_refs": refs,
            "support_refs": [],
            "type_candidates": ["text"],
            "issues": ["model_missed_pdf_text"],
        }
        add_coordinate_views(block, page)
        blocks.append(block)
        reviews["blocks"][block_id] = {
            "type": {"status": "CHECK", "method": "no_model_type", "evidence_refs": refs},
            "bbox": {"status": "PASS", "method": "pdf_span_union", "evidence_refs": refs},
            "text": {"status": "PASS", "method": "pdf_text_exact", "evidence_refs": refs},
        }
        issues.append({"issue_id": f"issue.p{page['page']}.{len(issues):04d}", "type": "model_missed_pdf_text", "block_ids": [block_id]})

    blocks, issues = _merge_contained_plain_text_blocks(blocks, reviews, issues, pdf_lines, page)

    for image in content_images:
        if image["evidence_id"] in claimed_images:
            continue
        final_box = _consensus_image_box(image, nested_by_image.get(image["evidence_id"], []), all_models)
        support_refs = nested_by_image.get(image["evidence_id"], [])
        block_id = f"candidate.block.p{page['page']}.{next(block_serial):04d}"
        block = {
            "block_id": block_id,
            "type": "image",
            "bbox_image_px": final_box,
            "reading_order": len(blocks),
            "source_refs": [image["evidence_id"]],
            "support_refs": support_refs,
            "type_candidates": ["image"],
            "issues": [],
        }
        footer_support = [item for item in all_models if item.get("source_type") in {"footer", "footer_image"} and _ioa(item["bbox_image_px"], final_box) >= 0.35]
        if footer_support and final_box[1] >= page["page_image"]["height"] * 0.82:
            block["content_role"] = "footer"
            block["support_refs"] = list(dict.fromkeys(support_refs + [item["evidence_id"] for item in footer_support]))
        add_coordinate_views(block, page)
        blocks.append(block)
        claimed_images.add(image["evidence_id"])
        reviews["blocks"][block_id] = {
            "type": {"status": "PASS", "method": "pdf_image_object", "evidence_refs": [image["evidence_id"]] + support_refs},
            "bbox": {"status": "PASS", "method": "pdf_image_object_model_consensus_trim" if final_box != image["bbox_image_px"] else "pdf_image_object", "evidence_refs": [image["evidence_id"]] + support_refs},
        }

    blocks, issues = _drop_unanchored_duplicate_blocks(blocks, reviews, issues, pdf_lines)
    blocks = _drop_duplicate_page_number_blocks(blocks, page)
    _confirm_page_number_consensus(blocks, reviews, all_models, page)
    blocks = _sort_blocks_in_reading_bands(blocks)
    drawings = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_drawing"]
    _promote_textual_content_images(page, blocks, reviews, nested_by_image, all_models, drawings)
    blocks, issues, _ = _consolidate_nested_text_blocks(page, blocks, reviews, issues)
    return blocks, reviews, issues


def _attach_inline_orphan_pdf_lines(
    blocks: list[dict[str, Any]], reviews: dict[str, Any], pdf_lines: list[dict[str, Any]], claimed_lines: set[str], page: dict[str, Any]
) -> None:
    evidence = {line["evidence_id"]: line for line in pdf_lines}
    attached: dict[str, list[str]] = {}
    for line in pdf_lines:
        if not line.get("required_coverage") or line["evidence_id"] in claimed_lines:
            continue
        box = line["bbox_image_px"]
        line_height = max(1.0, box[3] - box[1])
        candidates = []
        for block in blocks:
            if block.get("type") not in {"text", "unknown"} or not block.get("source_refs"):
                continue
            owner = block["bbox_image_px"]
            vertical = max(0.0, min(box[3], owner[3]) - max(box[1], owner[1])) / line_height
            if vertical < 0.70:
                continue
            gap = max(0.0, owner[0] - box[2], box[0] - owner[2])
            if gap > max(12.0, line_height * 1.5):
                continue
            candidates.append((gap, abs((box[1] + box[3]) - (owner[1] + min(owner[3], box[3]))), block))
        candidates.sort(key=lambda item: (item[0], item[1]))
        if not candidates or (len(candidates) > 1 and candidates[1][:2] == candidates[0][:2]):
            continue
        owner = candidates[0][2]
        owner["source_refs"] = list(dict.fromkeys(owner.get("source_refs", []) + [line["evidence_id"]]))
        claimed_lines.add(line["evidence_id"])
        attached.setdefault(owner["block_id"], []).append(line["evidence_id"])
    for block in blocks:
        if block["block_id"] not in attached:
            continue
        source_lines = [evidence[ref] for ref in block["source_refs"] if ref in evidence]
        if _lines_text(source_lines):
            block["text"] = _ordered_pdf_text(source_lines)
            block["bbox_image_px"] = _union([line["bbox_image_px"] for line in source_lines])
            add_coordinate_views(block, page)
            fields = reviews["blocks"][block["block_id"]]
            fields["bbox"] = {"status": "PASS", "method": "pdf_inline_fragment_union", "evidence_refs": block["source_refs"]}
            fields["text"] = {"status": "PASS", "method": "pdf_inline_reading_flow", "evidence_refs": block["source_refs"]}


def _sort_blocks_in_reading_bands(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = sorted(blocks, key=lambda block: (block["bbox_image_px"][1], block["bbox_image_px"][0]))
    ordered: list[dict[str, Any]] = []
    while remaining:
        first = remaining.pop(0)
        box = first["bbox_image_px"]
        height = max(1.0, box[3] - box[1])
        band = [first]
        keep = []
        for block in remaining:
            other = block["bbox_image_px"]
            other_height = max(1.0, other[3] - other[1])
            top_close = abs(other[1] - box[1]) <= max(3.0, min(height, other_height) * 0.25)
            vertical_overlap = max(0.0, min(box[3], other[3]) - max(box[1], other[1])) / min(height, other_height)
            if top_close and vertical_overlap >= 0.70:
                band.append(block)
            else:
                keep.append(block)
        ordered.extend(sorted(band, key=lambda block: block["bbox_image_px"][0]))
        remaining = keep
    return ordered


def _ordered_pdf_text(lines: list[dict[str, Any]]) -> str:
    if not lines:
        return ""
    heights = [max(1.0, line["bbox_image_px"][3] - line["bbox_image_px"][1]) for line in lines]
    tolerance = max(2.0, min(10.0, sorted(heights)[len(heights) // 2] * 0.35))
    rows: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=lambda item: ((item["bbox_image_px"][1] + item["bbox_image_px"][3]) / 2, item["bbox_image_px"][0])):
        center = (line["bbox_image_px"][1] + line["bbox_image_px"][3]) / 2
        if not rows:
            rows.append([line])
            continue
        previous_center = sum((item["bbox_image_px"][1] + item["bbox_image_px"][3]) / 2 for item in rows[-1]) / len(rows[-1])
        if abs(center - previous_center) <= tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])
    return "\n".join(_join_parts([str(item.get("text", "")) for item in sorted(row, key=lambda entry: entry["bbox_image_px"][0])]) for row in rows)


MARKER_ONLY = re.compile(r"^[\ue000-\uf8ff\u2022\u25cf\u25a0\u25aa\u25cb\u25a1\u27a2\u25c6\u25b8\u25ba\u2666\u25cc\u25ce\u25d0-\u25d3\u25a3\u25e6\u2756\u2758]{1,3}$")
NON_TEXT_PLACEHOLDER = "[Non-Text]"


def _is_marker_only(text: str) -> bool:
    return bool(MARKER_ONLY.fullmatch(re.sub(r"\s+", "", str(text or ""))))


def _merge_decorative_marker_blocks(blocks: list[dict[str, Any]], reviews: dict[str, Any], page: dict[str, Any]) -> None:
    """A list bullet the model emitted as its own block belongs to the item next to it.

    List markers are visible text, but as a standalone block they add a stray line to the document
    (and to every consumer's reading order). They are folded into the item they mark, which keeps the
    marker in the text and the measured line claimed, and the merged box covers both.
    """
    # a marker is often undecided at this point (single-model vote): its text is the only content,
    # so the type it would have received cannot change the fact that it marks the item beside it
    markers = [
        block for block in blocks
        if block.get("type") in TEXT_TYPES | {"unknown"}
        and _is_marker_only(block.get("text", ""))
        and str(block.get("text", "")).strip()
    ]
    if not markers:
        return
    removed: set[str] = set()
    for marker in markers:
        marker_box = marker["bbox_image_px"]
        marker_text = str(marker.get("text", "")).strip()
        best: dict[str, Any] | None = None
        best_score = 0.0
        for block in blocks:
            if block is marker or block.get("block_id") in removed:
                continue
            if block.get("type") not in TEXT_TYPES:
                continue
            box = block["bbox_image_px"]
            if box[2] <= marker_box[0]:
                continue  # the item must follow the marker, not precede it
            gap = box[0] - marker_box[2]
            if gap < -2 or gap > max(30.0, (marker_box[2] - marker_box[0]) * 2.5):
                continue
            vertical = min(box[3], marker_box[3]) - max(box[1], marker_box[1])
            if vertical < min(box[3] - box[1], marker_box[3] - marker_box[1]) * 0.5:
                continue
            score = vertical / max(1.0, marker_box[3] - marker_box[1])
            if score > best_score:
                best, best_score = block, score
        if best is None:
            continue
        current = str(best.get("text", ""))
        if current.startswith(marker_text):
            merged_text = current
        else:
            merged_text = f"{marker_text}{current}" if current else marker_text
        best["text"] = merged_text
        best["bbox_image_px"] = _union([best["bbox_image_px"], marker_box])
        # the merged box must keep its coordinate views consistent, or the page fails validation
        add_coordinate_views(best, page)
        best["source_refs"] = list(dict.fromkeys(best.get("source_refs", []) + marker.get("source_refs", [])))
        best["support_refs"] = list(dict.fromkeys(best.get("support_refs", []) + marker.get("support_refs", [])))
        best["type_candidates"] = list(dict.fromkeys(best.get("type_candidates", []) + marker.get("type_candidates", [])))
        fields = reviews.setdefault("blocks", {}).setdefault(best["block_id"], {})
        if "text" in fields:
            fields["text"] = {
                "status": fields["text"].get("status", "PASS"),
                "method": "decorative_marker_merged_into_item",
                "evidence_refs": best["source_refs"],
            }
        reviews.get("blocks", {}).pop(marker["block_id"], None)
        removed.add(marker["block_id"])
    if removed:
        blocks[:] = [block for block in blocks if block["block_id"] not in removed]


def _drop_glyph_noise_text_blocks(
    blocks: list[dict[str, Any]], reviews: dict[str, Any], issues: list[dict[str, Any]]
) -> None:
    """Text blocks whose text is pure glyph noise (control/PUA codes) are not content.

    They usually come from a broken font map over a page whose real text the models read correctly;
    keeping them would poison the GT and trip text comparisons. A block that survives sanitisation
    with readable text is kept instead (its stray codes are already stripped).
    """
    dropped: list[str] = []
    for block in blocks:
        if block.get("type") not in TEXT_TYPES | {"unknown"}:
            continue
        text = str(block.get("text") or "")
        if not text or not _text_is_unusable(text):
            continue
        cleaned = _sanitize_text(text).strip()
        if cleaned:
            block["text"] = cleaned
            fields = reviews.setdefault("blocks", {}).setdefault(block["block_id"], {})
            if fields.get("text", {}).get("status") == "PASS":
                fields["text"] = {"status": "CHECK", "method": "glyph_noise_sanitised", "evidence_refs": fields["text"].get("evidence_refs", [])}
            continue
        if block.get("html") or block.get("source_refs") and any(str(ref).startswith("pdf.image") for ref in block.get("source_refs", [])):
            block.pop("text", None)
            reviews.setdefault("blocks", {}).setdefault(block["block_id"], {}).pop("text", None)
            continue
        dropped.append(block["block_id"])
    if not dropped:
        return
    for block_id in dropped:
        reviews.get("blocks", {}).pop(block_id, None)
        issues.append({"issue_id": f"issue.{block_id}.glyph_noise", "type": "glyph_noise_text_block_dropped", "block_ids": [block_id]})
    blocks[:] = [block for block in blocks if block["block_id"] not in dropped]
    for order, block in enumerate(blocks):
        block["reading_order"] = order


def _drop_placeholder_blocks(blocks: list[dict[str, Any]], reviews: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    """A block whose only content is the "[Non-Text]" placeholder carries nothing measurable.

    Such blocks come from a model guess over a page whose text layer does not contain them: without a
    measured line or image they cannot be verified, cannot be typed as text-bearing content, and would
    only add unresolvable fields. A placeholder that does claim measured evidence keeps its block, with
    the placeholder text cleared so the type decision is made on the geometry alone.
    """
    removed: set[str] = set()
    for block in blocks:
        if str(block.get("text", "")).strip() != NON_TEXT_PLACEHOLDER:
            continue
        if block.get("source_refs"):
            block["text"] = ""
            block.pop("text", None)
            fields = reviews.setdefault("blocks", {}).setdefault(block["block_id"], {})
            fields.pop("text", None)
            continue
        removed.add(block["block_id"])
        reviews.get("blocks", {}).pop(block["block_id"], None)
        issues.append({"issue_id": f"issue.{block['block_id']}.placeholder", "type": "non_text_placeholder_without_evidence", "block_ids": [block["block_id"]]})
    if removed:
        blocks[:] = [block for block in blocks if block["block_id"] not in removed]


def _merge_contained_plain_text_blocks(
    blocks: list[dict[str, Any]], reviews: dict[str, Any], issues: list[dict[str, Any]], pdf_lines: list[dict[str, Any]], page: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evidence = {line["evidence_id"]: line for line in pdf_lines}
    dropped: set[str] = set()
    ordered = sorted(blocks, key=lambda block: _box_area(block["bbox_image_px"]), reverse=True)
    for owner in ordered:
        if owner["block_id"] in dropped or owner.get("type") not in {"text", "unknown"}:
            continue
        owner_refs = [ref for ref in owner.get("source_refs", []) if ref in evidence]
        if not owner_refs:
            continue
        for child in ordered:
            if child is owner or child["block_id"] in dropped:
                continue
            child_refs = [ref for ref in child.get("source_refs", []) if ref in evidence]
            child_mergeable = child.get("type") in {"text", "unknown"} or (
                # a plain contained line inside a merged text container (table of contents entries
                # modelled separately) belongs to that container: keeping it creates two overlapping
                # plain text blocks over the same region.
                child.get("type") == "title"
                and len(owner_refs) >= 3
                and len(child_refs) == 1
            )
            if not child_mergeable:
                continue
            if not child_refs or set(owner_refs) & set(child_refs):
                continue
            owner_area = _box_area(owner["bbox_image_px"])
            if not owner_area or _ioa(child["bbox_image_px"], owner["bbox_image_px"]) < 0.80:
                continue
            owner_refs.extend(child_refs)
            owner["source_refs"] = list(dict.fromkeys(owner_refs))
            owner["support_refs"] = list(dict.fromkeys(owner.get("support_refs", []) + child.get("support_refs", [])))
            source_lines = [evidence[ref] for ref in owner["source_refs"]]
            if _lines_text(source_lines):
                owner["text"] = _ordered_pdf_text(source_lines)
                owner["bbox_image_px"] = _union([line["bbox_image_px"] for line in source_lines])
                add_coordinate_views(owner, page)
                owner.setdefault("issues", []).append("contained_plain_text_flow_merged")
            fields = reviews["blocks"].setdefault(owner["block_id"], {})
            fields["bbox"] = {"status": "PASS", "method": "pdf_line_ownership_union", "evidence_refs": owner["source_refs"]}
            fields["text"] = {"status": "PASS", "method": "pdf_line_reading_flow", "evidence_refs": owner["source_refs"]}
            dropped.add(child["block_id"])
    if not dropped:
        return blocks, issues
    for block_id in dropped:
        reviews["blocks"].pop(block_id, None)
    kept = [block for block in blocks if block["block_id"] not in dropped]
    for order, block in enumerate(sorted(kept, key=lambda item: (item["bbox_image_px"][1], item["bbox_image_px"][0]))):
        block["reading_order"] = order
    return kept, [issue for issue in issues if not dropped.intersection(issue.get("block_ids", []))]


def _consensus_image_box(image: dict[str, Any], support_refs: list[str], models: list[dict[str, Any]]) -> list[float]:
    candidates = [
        item["bbox_image_px"]
        for item in models
        if item.get("evidence_id") in support_refs
        and item.get("type_candidate") == "image"
        and _iou(item["bbox_image_px"], image["bbox_image_px"]) >= 0.70
    ]
    if len(candidates) < 2 or _iou(candidates[0], candidates[1]) < 0.70:
        return list(image["bbox_image_px"])
    return [
        max(box[0] for box in candidates),
        max(box[1] for box in candidates),
        min(box[2] for box in candidates),
        min(box[3] for box in candidates),
    ]


def _page_number_value(text: str) -> str:
    return "".join(re.findall(r"\d+", text))


def _confirm_page_number_consensus(blocks: list[dict[str, Any]], reviews: dict[str, Any], models: list[dict[str, Any]], page: dict[str, Any]) -> None:
    evidence = {item["evidence_id"]: item for item in models}
    height = page["page_image"]["height"]
    for block in blocks:
        if block.get("type") != "page_number" or block["bbox_image_px"][1] < height * 0.86:
            continue
        supports = [evidence[ref] for ref in block.get("support_refs", []) if ref in evidence and evidence[ref].get("type_candidate") == "page_number"]
        models_seen = {item.get("model") for item in supports}
        values = {_page_number_value(str(item.get("text", ""))) for item in supports}
        values.discard("")
        if len(models_seen) < 2 or len(values) != 1:
            continue
        fields = reviews["blocks"].setdefault(block["block_id"], {})
        refs = [item["evidence_id"] for item in supports]
        fields["type"] = {"status": "PASS", "method": "cross_model_page_number_consensus", "evidence_refs": refs}
        fields["text"] = {"status": "PASS", "method": "cross_model_page_number_consensus", "evidence_refs": refs}
        fields["bbox"] = {"status": "PASS", "method": "cross_model_page_number_consensus", "evidence_refs": refs}


def _drop_duplicate_page_number_blocks(blocks: list[dict[str, Any]], page: dict[str, Any]) -> list[dict[str, Any]]:
    height = page["page_image"]["height"]
    candidates = [block for block in blocks if block.get("type") == "page_number" and block["bbox_image_px"][1] >= height * 0.86]
    dropped: set[str] = set()
    for index, left in enumerate(candidates):
        if left["block_id"] in dropped:
            continue
        for right in candidates[index + 1 :]:
            if right["block_id"] in dropped:
                continue
            ly = (left["bbox_image_px"][1] + left["bbox_image_px"][3]) / 2
            ry = (right["bbox_image_px"][1] + right["bbox_image_px"][3]) / 2
            lx = (left["bbox_image_px"][0] + left["bbox_image_px"][2]) / 2
            rx = (right["bbox_image_px"][0] + right["bbox_image_px"][2]) / 2
            close = abs(ly - ry) <= max(18, (left["bbox_image_px"][3] - left["bbox_image_px"][1]) * 0.8) and abs(lx - rx) <= page["page_image"]["width"] * 0.15
            if not close:
                continue
            keep, discard = max((left, right), key=lambda block: (len(block.get("support_refs", [])), block["bbox_image_px"][2] - block["bbox_image_px"][0])) , min((left, right), key=lambda block: (len(block.get("support_refs", [])), block["bbox_image_px"][2] - block["bbox_image_px"][0]))
            keep["support_refs"] = list(dict.fromkeys(keep.get("support_refs", []) + discard.get("support_refs", [])))
            dropped.add(discard["block_id"])
            left = keep
    return [block for block in blocks if block["block_id"] not in dropped]


INK_DELTA = 12  # grey-level difference that counts as painted ink in the rendered page


def _mark_visually_absent_lines(page: dict[str, Any], lines: list[dict[str, Any]]) -> int:
    """Mark PDF text lines that leave no visible ink in the rendered page.

    Visibility is decided on the rendered page, not on colour metadata: white-on-white text, text
    hidden behind an opaque fill, zero-opacity text and text painted with its own background colour
    all render as a region without ink. Such a line is not visible content, so it must not become a
    top-level block and must not require coverage.
    """
    image_path = str(page.get("page_image", {}).get("path") or "")
    if not image_path:
        return 0
    try:
        from PIL import Image
        image = Image.open(image_path).convert("L")
    except Exception:
        return 0
    width, height = image.size
    scale_x = width / float(page["page_image"].get("width") or width)
    scale_y = height / float(page["page_image"].get("height") or height)
    marked = 0
    for line in lines:
        if not str(line.get("text", "")).strip():
            continue
        box = line.get("bbox_image_px")
        if not isinstance(box, list) or len(box) != 4:
            continue
        x0, y0 = int(box[0] * scale_x), int(box[1] * scale_y)
        x1, y1 = int(math.ceil(box[2] * scale_x)), int(math.ceil(box[3] * scale_y))
        if x1 - x0 < 6 or y1 - y0 < 6:
            continue
        crop = image.crop((max(0, x0), max(0, y0), min(width, x1), min(height, y1)))
        histogram = crop.histogram()
        total = sum(histogram) or 1
        background = max(range(256), key=lambda value: histogram[value])
        # A faint grey glyph is still visible content: the ink threshold only has to separate "some ink"
        # from "nothing painted here", not to grade contrast.
        ink = sum(count for value, count in enumerate(histogram) if abs(value - background) > INK_DELTA)
        fraction = ink / total
        if fraction < 0.002:
            line["visually_absent"] = True
            line["visual_absence"] = {
                "method": "rendered_region_has_no_ink",
                "ink_fraction": round(fraction, 5),
                "evidence_refs": [line["evidence_id"]],
            }
            line["required_coverage"] = False
            marked += 1
    return marked


def _drop_nonprintable_text_blocks(
    blocks: list[dict[str, Any]], reviews: dict[str, Any], issues: list[dict[str, Any]]
) -> None:
    """Text blocks made of control characters are font-mapping garbage, not content.

    Broken ToUnicode maps produce blocks like "\x00\r#\x07\b" that overlap the real text block on the
    same area and then trip the overlapping-text-block validator. They are dropped, with a record.
    """
    dropped: list[str] = []
    for block in list(blocks):
        if block.get("type") not in TEXT_TYPES | {"unknown"}:
            continue
        text = str(block.get("text") or "")
        if not text:
            continue
        control = sum(1 for character in text if unicodedata.category(character) in {"Cc", "Cf", "Co", "Cn"})
        printable = sum(1 for character in text if character.isprintable() and not character.isspace())
        # A broken font map either yields nothing readable at all, or mixes a couple of stray glyphs
        # ("#" "$") into control characters. Both are garbage; real short text like "第1页" survives.
        mostly_control = control / max(1, len(text)) >= 0.5 and printable <= 3
        if printable > 0 and not mostly_control:
            continue
        dropped.append(block["block_id"])
    if not dropped:
        return
    for block_id in dropped:
        reviews.get("blocks", {}).pop(block_id, None)
        issues.append({
            "issue_id": f"issue.{block_id}.nonprintable",
            "type": "nonprintable_text_block_dropped",
            "block_ids": [block_id],
        })
    blocks[:] = [block for block in blocks if block["block_id"] not in set(dropped)]


def _drop_invisible_text_blocks(
    page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], issues: list[dict[str, Any]], pdf_lines: list[dict[str, Any]]
) -> None:
    """Text that renders without ink is not visible content: drop the block or strip the hidden lines."""
    absent = {str(line["evidence_id"]) for line in pdf_lines if line.get("visually_absent")}
    if not absent:
        return
    evidence = {str(line["evidence_id"]): line for line in pdf_lines}
    dropped: list[str] = []
    for block in blocks:
        if block.get("type") not in TEXT_TYPES | {"unknown"}:
            continue
        refs = [str(ref) for ref in block.get("source_refs", []) if str(ref).startswith("pdf.line")]
        hidden = [ref for ref in refs if ref in absent]
        if not hidden:
            continue
        if len(hidden) == len(refs):
            dropped.append(block["block_id"])
            continue
        visible = [ref for ref in refs if ref not in absent]
        lines = [evidence[ref] for ref in visible if ref in evidence]
        if not _lines_text(lines):
            continue
        block["source_refs"] = visible
        block["text"] = _ordered_pdf_text(lines)
        block["bbox_image_px"] = _union([line["bbox_image_px"] for line in lines])
        add_coordinate_views(block, page)
        fields = reviews.setdefault("blocks", {}).setdefault(block["block_id"], {})
        fields["text"] = {"status": "PASS", "method": "pdf_text_visible_lines_only", "evidence_refs": visible}
        fields["bbox"] = {"status": "PASS", "method": "pdf_span_union", "evidence_refs": visible}
    if dropped:
        blocks[:] = [block for block in blocks if block["block_id"] not in dropped]
        for order, block in enumerate(blocks):
            block["reading_order"] = order
        for block_id in dropped:
            reviews.get("blocks", {}).pop(block_id, None)
    if dropped or absent:
        issues.append({
            "issue_id": f"issue.p{page['page']}.{len(issues):04d}",
            "type": "invisible_text_not_visible_content",
            "block_ids": dropped,
            "evidence_ids": sorted(absent),
        })


def _text_overlap_ratio(value: str, other: str) -> float:
    """Share of the shorter string's character bigrams that also occur in the longer one.

    Wording comparison has to survive OCR variants (a checkbox read as a box glyph or as a character),
    so it is measured on character bigrams instead of exact or substring equality.
    """
    if len(value) < 2 or len(other) < 2:
        return 0.0
    left = {value[index:index + 2] for index in range(len(value) - 1)}
    right = {other[index:index + 2] for index in range(len(other) - 1)}
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _drop_unanchored_duplicate_blocks(
    blocks: list[dict[str, Any]],
    reviews: dict[str, Any],
    issues: list[dict[str, Any]],
    pdf_lines: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dropped: set[str] = set()
    line_boxes = {
        str(item["evidence_id"]): [float(value) for value in item["bbox_image_px"]]
        for item in (pdf_lines or [])
        if isinstance(item.get("bbox_image_px"), list) and len(item["bbox_image_px"]) == 4
    }
    for block in blocks:
        if (
            not block.get("source_refs")
            and block.get("type") in {"text", "title", "header", "footer", "page_number"}
            and not _norm(str(block.get("text", "")))
        ):
            dropped.add(block["block_id"])
    anchored = [block for block in blocks if block.get("source_refs")]
    for block in blocks:
        if block.get("source_refs"):
            continue
        single_model = len(block.get("support_refs", [])) == 1
        text = _duplicate_norm(str(block.get("text", "")))
        latex_free = _norm(str(block.get("text", "")))
        for owner in anchored:
            box = block["bbox_image_px"]
            tolerance = max(4.0, min(box[2] - box[0], box[3] - box[1]) * 0.25)
            expanded_owner = [owner["bbox_image_px"][0] - tolerance, owner["bbox_image_px"][1] - tolerance, owner["bbox_image_px"][2] + tolerance, owner["bbox_image_px"][3] + tolerance]
            backward = _ioa(
                owner["bbox_image_px"],
                [box[0] - tolerance, box[1] - tolerance, box[2] + tolerance, box[3] + tolerance],
            )
            if max(_ioa(box, expanded_owner), backward) < 0.8:
                continue
            owner_text = _duplicate_norm(str(owner.get("text", "")))
            owner_latex_free = _norm(str(owner.get("text", "")))
            same_region = _iou(block["bbox_image_px"], owner["bbox_image_px"]) >= 0.75
            exact_text = bool(text and owner_text and text == owner_text)
            latex_contained = single_model and bool(latex_free) and len(latex_free) >= 8 and latex_free in owner_latex_free
            prefix_fragment = bool(text) and len(text) <= 12 and owner_text.startswith(text) and _ioa(box, owner["bbox_image_px"]) >= 0.9
            alnum = re.sub(r"[\W_]+", "", str(block.get("text", "")), flags=re.UNICODE)
            owner_alnum = re.sub(r"[\W_]+", "", str(owner.get("text", "")), flags=re.UNICODE)
            alnum_contained = single_model and bool(alnum) and len(alnum) >= 2 and alnum in owner_alnum and _ioa(box, owner["bbox_image_px"]) >= 0.9
            contained_short = (
                single_model
                and bool(text)
                and len(text) <= 12
                and len(owner_text) >= len(text) * 3
                and _ioa(box, owner["bbox_image_px"]) >= 0.9
            )
            # An unanchored block that sits inside an anchored text block cannot be verified and
            # duplicates a region the PDF already carries: keep its model votes as support only.
            # Same wording over the same region, but the two boxes differ wildly in size (a wide model
            # read of one line against the narrow anchored line): the smaller box being covered by the
            # larger is the right containment test, not the block's own coverage ratio.
            block_area, owner_area = _box_area(box), _box_area(owner["bbox_image_px"])
            smaller_box, larger_box = (box, owner["bbox_image_px"]) if block_area <= owner_area else (owner["bbox_image_px"], box)
            slack = max(4.0, min(smaller_box[2] - smaller_box[0], smaller_box[3] - smaller_box[1]) * 0.25)
            covered = _ioa(smaller_box, [larger_box[0] - slack, larger_box[1] - slack, larger_box[2] + slack, larger_box[3] + slack]) >= 0.80
            owner_line_boxes = [line_boxes[str(ref)] for ref in owner.get("source_refs", []) if str(ref) in line_boxes]
            line_region_covered = bool(owner_line_boxes) and _ioa(smaller_box, _union(owner_line_boxes)) >= 0.80
            same_wording = _text_overlap_ratio(str(block.get("text", "")), str(owner.get("text", ""))) >= 0.60
            inside_anchored_text = (
                covered
                and owner.get("type") in TEXT_TYPES | {"unknown"}
                and any(str(ref).startswith("pdf.line") for ref in owner.get("source_refs", []))
                and (same_wording or line_region_covered)
            )
            if exact_text or latex_contained or prefix_fragment or alnum_contained or contained_short or inside_anchored_text or (single_model and (not text or text in owner_text or (same_region and owner_text))):
                owner["support_refs"] = list(dict.fromkeys(owner.get("support_refs", []) + block.get("support_refs", [])))
                owner["type_candidates"] = sorted(set(owner.get("type_candidates", [])) | set(block.get("type_candidates", [])))
                if exact_text:
                    fields = reviews.get("blocks", {}).setdefault(owner["block_id"], {})
                    fields["type"] = {"status": "CHECK", "method": "collapsed_model_duplicate_type_review", "evidence_refs": owner["source_refs"] + owner["support_refs"]}
                    owner["type"] = "unknown"
                    if owner.get("type") != "title":
                        owner.pop("level", None)
                    owner.setdefault("issues", []).append("collapsed_model_duplicate_type_review")
                if inside_anchored_text and not (exact_text or latex_contained or prefix_fragment or alnum_contained or contained_short):
                    fields = reviews.get("blocks", {}).setdefault(owner["block_id"], {})
                    fields["unanchored_text_inside_anchored"] = {
                        "status": "PASS",
                        "method": "pdf_line_region_ownership",
                        "evidence_refs": list(owner.get("source_refs", []))[:12],
                    }
                dropped.add(block["block_id"])
                break
    if not dropped:
        return blocks, issues
    for block_id in dropped:
        reviews.get("blocks", {}).pop(block_id, None)
    kept_issues = [issue for issue in issues if not dropped.intersection(issue.get("block_ids", []))]
    return [block for block in blocks if block["block_id"] not in dropped], kept_issues


def _suppress_compound_pdf_lines(
    blocks: list[dict[str, Any]], reviews: dict[str, Any], pdf_lines: list[dict[str, Any]], models: list[dict[str, Any]], claimed_lines: set[str]
) -> None:
    model_index = {item["evidence_id"]: item for item in models}
    available = [line for line in pdf_lines if line.get("required_coverage") and line["evidence_id"] not in claimed_lines]
    for block in blocks:
        if block.get("source_refs") or block.get("type") not in (TEXT_TYPES | {"unknown"}):
            continue
        block_text = _duplicate_norm(str(block.get("text", "")))
        if not block_text:
            continue
        box = block["bbox_image_px"]
        nearby = [line for line in available if max(0.0, min(box[3], line["bbox_image_px"][3]) - max(box[1], line["bbox_image_px"][1])) / max(1.0, line["bbox_image_px"][3] - line["bbox_image_px"][1]) >= 0.70]
        nearby.sort(key=lambda item: (item["bbox_image_px"][1], item["bbox_image_px"][0]))
        if len(nearby) < 2 or "".join(_duplicate_norm(str(line.get("text", ""))) for line in nearby) != block_text:
            continue
        support = [model_index[ref] for ref in block.get("support_refs", []) if ref in model_index and _duplicate_norm(str(model_index[ref].get("text", "")))]
        by_model: dict[str, list[dict[str, Any]]] = {}
        for item in support:
            by_model.setdefault(str(item.get("model")), []).append(item)
        model_texts = []
        for items in by_model.values():
            items.sort(key=lambda item: (item["bbox_image_px"][1], item["bbox_image_px"][0]))
            model_texts.append("".join(_duplicate_norm(str(item.get("text", ""))) for item in items))
        if sum(text == block_text for text in model_texts) < 2:
            continue
        refs = [line["evidence_id"] for line in nearby]
        for line in nearby:
            line["required_coverage"] = False
            line["compounded_by_model_block"] = block["block_id"]
        block["pdf_compound_refs"] = refs
        fields = reviews["blocks"][block["block_id"]]
        evidence_refs = block.get("support_refs", []) + refs
        fields["bbox"] = {"status": "PASS", "method": "cross_model_bbox_pdf_multiline_compound", "evidence_refs": evidence_refs}
        fields["text"] = {"status": "PASS", "method": "cross_model_text_pdf_multiline_exact", "evidence_refs": evidence_refs}


def _suppress_partitioned_pdf_lines(
    blocks: list[dict[str, Any]], reviews: dict[str, Any], pdf_lines: list[dict[str, Any]], models: list[dict[str, Any]], claimed_lines: set[str]
) -> None:
    model_index = {item["evidence_id"]: item for item in models}
    for line in pdf_lines:
        if not line.get("required_coverage") or line["evidence_id"] in claimed_lines:
            continue
        line_box = line["bbox_image_px"]
        fragments = []
        for block in blocks:
            if block.get("source_refs") or block.get("type") not in (TEXT_TYPES | {"unknown"}) or not _duplicate_norm(str(block.get("text", ""))):
                continue
            support = [model_index[ref] for ref in block.get("support_refs", []) if ref in model_index]
            if len({item.get("model") for item in support}) < 2:
                continue
            vertical_overlap = max(0.0, min(line_box[3], block["bbox_image_px"][3]) - max(line_box[1], block["bbox_image_px"][1]))
            if vertical_overlap / max(1.0, block["bbox_image_px"][3] - block["bbox_image_px"][1]) < 0.70:
                continue
            if block["bbox_image_px"][0] < line_box[0] - 12 or block["bbox_image_px"][2] > line_box[2] + 12:
                continue
            fragments.append(block)
        fragments.sort(key=lambda block: block["bbox_image_px"][0])
        exact = [block for block in fragments if _duplicate_norm(str(block.get("text", ""))) == _duplicate_norm(str(line.get("text", "")))]
        if len(exact) == 1:
            block = exact[0]
            support = [model_index[ref] for ref in block.get("support_refs", []) if ref in model_index]
            support_boxes = [item["bbox_image_px"] for item in support]
            model_consensus = len({item.get("model") for item in support}) >= 2 and len(support_boxes) >= 2 and min(_iou(left, right) for index, left in enumerate(support_boxes) for right in support_boxes[index + 1 :]) >= 0.70
            contained = _ioa(block["bbox_image_px"], line_box) >= 0.95
            tighter = (line_box[2] - line_box[0]) >= (block["bbox_image_px"][2] - block["bbox_image_px"][0]) * 1.35
            if model_consensus and contained and tighter:
                line["required_coverage"] = False
                line["tightened_by_model_block"] = block["block_id"]
                block["pdf_tightening_ref"] = line["evidence_id"]
                fields = reviews["blocks"][block["block_id"]]
                refs = block.get("support_refs", []) + [line["evidence_id"]]
                fields["bbox"] = {"status": "PASS", "method": "cross_model_bbox_pdf_line_tightening", "evidence_refs": refs}
                fields["text"] = {"status": "PASS", "method": "cross_model_text_pdf_line_exact", "evidence_refs": refs}
                continue
        if len(fragments) < 2:
            continue
        if any(left["bbox_image_px"][2] > right["bbox_image_px"][0] + 8 for left, right in zip(fragments, fragments[1:])):
            continue
        combined = "".join(_duplicate_norm(str(block.get("text", ""))) for block in fragments)
        if combined != _duplicate_norm(str(line.get("text", ""))):
            continue
        line["required_coverage"] = False
        line["partitioned_by_model_blocks"] = [block["block_id"] for block in fragments]
        for block in fragments:
            block["pdf_partition_ref"] = line["evidence_id"]
            fields = reviews["blocks"][block["block_id"]]
            refs = block.get("support_refs", []) + [line["evidence_id"]]
            fields["bbox"] = {"status": "PASS", "method": "cross_model_bbox_pdf_line_partition", "evidence_refs": refs}
            fields["text"] = {"status": "PASS", "method": "cross_model_text_pdf_line_partition", "evidence_refs": refs}


def _group_orphan_lines(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=lambda item: (item["bbox_image_px"][1], item["bbox_image_px"][0])):
        if not groups:
            groups.append([line])
            continue
        previous = groups[-1][-1]
        pb, box = previous["bbox_image_px"], line["bbox_image_px"]
        gap = box[1] - pb[3]
        line_height = max(pb[3] - pb[1], box[3] - box[1])
        same_indent = abs(box[0] - pb[0]) <= 20
        starts_new_item = bool(re.match(r"^\s*(?:[-+*•●]\s+|\d+[.．、]\s*|[（(]\d+[）)]\s*)", line["text"]))
        if same_indent and -2 <= gap <= max(18, line_height * 0.7) and not starts_new_item:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _anydoc_outline(markdown: str, limit: int = 12000) -> str:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    headings = [line for line in lines if line.startswith("#")]
    return "\n".join(headings if headings else lines[:80])[:limit]


def _table_html_match_score(html_value: str, pdf_text: str) -> float:
    html_text = _norm(_strip_html(html_value))
    target = _norm(pdf_text)
    if not html_text or not target:
        return 0.0
    matcher = difflib.SequenceMatcher(None, target, html_text)
    matched = sum(match.size for match in matcher.get_matching_blocks())
    coverage = matched / max(1, len(target))
    precision = matched / max(1, len(html_text))
    return 0.80 * coverage + 0.20 * precision


class _SimpleTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.tables = 0
        self.rows = 0
        self.cells = 0
        self.has_spans = False
        self.text_parts: list[str] = []
        self.row_columns: list[int] = []
        self.current_columns = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self.tables += 1
            self.depth += 1
        elif tag == "tr" and self.depth == 1:
            self.rows += 1
            self.current_columns = 0
        elif tag in {"td", "th"} and self.depth == 1:
            self.cells += 1
            try:
                self.current_columns += max(1, int(dict(attrs).get("colspan") or "1"))
            except ValueError:
                self.current_columns += 1
            self.has_spans |= any(name.lower() in {"rowspan", "colspan"} for name, _ in attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "tr" and self.depth == 1:
            self.row_columns.append(self.current_columns)
        elif tag == "table":
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth == 1:
            self.text_parts.append(data)


def _repair_trailing_empty_table_column(page: dict[str, Any], block: dict[str, Any]) -> None:
    html_value = block.get("html")
    if block.get("type") != "table" or not isinstance(html_value, str):
        return
    parser = _SimpleTableParser()
    try:
        parser.feed(html_value)
        parser.close()
    except Exception:
        return
    if not parser.row_columns or parser.has_spans or len(set(parser.row_columns)) != 1:
        return
    box = block["bbox_image_px"]
    drawings = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_drawing"]
    vertical = [item for item in drawings if item["bbox_image_px"][2] - item["bbox_image_px"][0] <= 5 and item["bbox_image_px"][3] - item["bbox_image_px"][1] >= (box[3] - box[1]) * 0.75 and box[0] - 8 <= (item["bbox_image_px"][0] + item["bbox_image_px"][2]) / 2 <= box[2] + 8]
    x_positions = sorted({round((item["bbox_image_px"][0] + item["bbox_image_px"][2]) / 2, 1) for item in vertical})
    pdf_columns = len(x_positions) - 1
    if pdf_columns <= 0 or parser.row_columns[0] != pdf_columns - 1:
        return
    repaired = re.sub(r"</tr>", "<td></td></tr>", html_value, flags=re.IGNORECASE)
    if repaired != html_value:
        block["html"] = repaired
        block["table_column_repair"] = {"added_trailing_empty_columns": 1, "pdf_grid_columns": pdf_columns, "method": "pdf_ruled_grid_consistent_empty_trailing_column"}


def _table_cell_text_norm(value: str) -> str:
    # Some model HTML serializes visual line breaks as literal backslash escapes.
    return _duplicate_norm(re.sub(r"\\[nrt]", "", value or ""))


def _html_table_grid(html_value: str) -> list[dict[str, Any]] | None:
    try:
        root = ET.fromstring(html_value)
    except ET.ParseError:
        return None
    if root.tag.lower() != "table":
        return None
    occupied: set[tuple[int, int]] = set()
    result: list[dict[str, Any]] = []
    rows = [element for element in root.iter() if element.tag.lower() == "tr"]
    for row_index, row in enumerate(rows):
        column = 0
        cells = [child for child in list(row) if child.tag.lower() in {"td", "th"}]
        if not cells:
            return None
        for cell in cells:
            while (row_index, column) in occupied:
                column += 1
            try:
                rowspan = int(cell.attrib.get("rowspan", "1"))
                colspan = int(cell.attrib.get("colspan", "1"))
            except ValueError:
                return None
            if rowspan < 1 or colspan < 1 or row_index + rowspan > len(rows):
                return None
            slots = {(row, col) for row in range(row_index, row_index + rowspan) for col in range(column, column + colspan)}
            if slots & occupied:
                return None
            occupied.update(slots)
            result.append({
                "row": row_index,
                "col": column,
                "rowspan": rowspan,
                "colspan": colspan,
                "text": _table_cell_text_norm("".join(cell.itertext())),
                "has_image": any(element.tag.lower() == "img" for element in cell.iter()),
            })
            column += colspan
    if not result:
        return None
    column_count = max(col for _, col in occupied) + 1
    if occupied != {(row, col) for row in range(len(rows)) for col in range(column_count)}:
        return None
    return result


def _table_structure_signature(html_value: str) -> tuple[tuple[tuple[int, int, str, bool], ...], ...] | None:
    cells = _html_table_grid(html_value)
    if cells is None:
        return None
    rows: dict[int, list[tuple[int, int, str, bool]]] = {}
    for cell in cells:
        rows.setdefault(cell["row"], []).append((cell["rowspan"], cell["colspan"], cell["text"], cell["has_image"]))
    return tuple(tuple(rows[row]) for row in range(len(rows)))


def _confirm_cross_model_table_html(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> None:
    evidence = {item["evidence_id"]: item for values in page["evidence"].values() for item in values}
    for block in blocks:
        if block.get("type") != "table" or not isinstance(block.get("html"), str):
            continue
        output_signature = _table_structure_signature(block["html"])
        if output_signature is None:
            continue
        candidates = [evidence.get(ref, {}) for ref in block.get("support_refs", [])]
        signatures: dict[str, tuple[tuple[tuple[int, int, str, bool], ...], ...]] = {}
        refs = []
        for candidate in candidates:
            html_value = candidate.get("html")
            model = candidate.get("model")
            signature = _table_structure_signature(html_value) if isinstance(html_value, str) else None
            if model and signature is not None:
                signatures[str(model)] = signature
                refs.append(candidate["evidence_id"])
        if len(signatures) < 2 or any(signature != output_signature for signature in signatures.values()):
            continue
        html_text = "".join(cell[2] for row in output_signature for cell in row)
        pdf_refs = [ref for ref in block.get("source_refs", []) if evidence.get(ref, {}).get("kind") == "pdf_line"]
        pdf_text = "".join(_duplicate_norm(str(evidence[ref].get("text", ""))) for ref in pdf_refs)
        if not pdf_text or html_text != pdf_text:
            continue
        reviews["blocks"][block["block_id"]]["html"] = {
            "status": "PASS",
            "method": "cross_model_table_structure_pdf_text_exact",
            "evidence_refs": list(dict.fromkeys(refs + pdf_refs)),
        }


def _cluster_coordinates(values: list[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - sum(groups[-1]) / len(groups[-1]) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _pdf_ruled_cell_grid(page: dict[str, Any], box: list[float]) -> tuple[list[dict[str, Any]], list[str], list[float]] | None:
    tolerance = max(3.0, page["page_image"]["width"] * 0.0025)
    expanded = [box[0] - tolerance * 2, box[1] - tolerance * 2, box[2] + tolerance * 2, box[3] + tolerance * 2]
    unique: dict[tuple[float, float, float, float], dict[str, Any]] = {}
    for item in page["evidence"]["pdf"]:
        if item.get("kind") != "pdf_drawing":
            continue
        drawing_box = item["bbox_image_px"]
        if drawing_box[2] - drawing_box[0] <= tolerance * 2 or drawing_box[3] - drawing_box[1] <= tolerance * 2:
            continue
        if _ioa(drawing_box, expanded) < 0.95:
            continue
        key = tuple(round(value, 1) for value in drawing_box)
        unique.setdefault(key, item)
    if len(unique) < 2:
        return None
    drawings = list(unique.values())
    x_boundaries = _cluster_coordinates([value for item in drawings for value in (item["bbox_image_px"][0], item["bbox_image_px"][2])], tolerance)
    y_boundaries = _cluster_coordinates([value for item in drawings for value in (item["bbox_image_px"][1], item["bbox_image_px"][3])], tolerance)
    if len(x_boundaries) < 2 or len(y_boundaries) < 2:
        return None

    def boundary_index(value: float, boundaries: list[float]) -> int | None:
        index = min(range(len(boundaries)), key=lambda candidate: abs(boundaries[candidate] - value))
        return index if abs(boundaries[index] - value) <= tolerance else None

    cells = []
    occupied: set[tuple[int, int]] = set()
    for item in drawings:
        drawing_box = item["bbox_image_px"]
        col, col_end = boundary_index(drawing_box[0], x_boundaries), boundary_index(drawing_box[2], x_boundaries)
        row, row_end = boundary_index(drawing_box[1], y_boundaries), boundary_index(drawing_box[3], y_boundaries)
        if None in {row, row_end, col, col_end} or row_end <= row or col_end <= col:
            return None
        slots = {(r, c) for r in range(row, row_end) for c in range(col, col_end)}
        if slots & occupied:
            return None
        occupied.update(slots)
        cells.append({
            "row": row,
            "col": col,
            "rowspan": row_end - row,
            "colspan": col_end - col,
            "bbox_image_px": drawing_box,
            "drawing_ref": item["evidence_id"],
        })
    expected = {(row, col) for row in range(len(y_boundaries) - 1) for col in range(len(x_boundaries) - 1)}
    if occupied != expected:
        return None
    grid_box = [x_boundaries[0], y_boundaries[0], x_boundaries[-1], y_boundaries[-1]]
    return cells, [cell["drawing_ref"] for cell in cells], grid_box


def _interval_is_covered(intervals: list[tuple[float, float]], start: float, end: float, tolerance: float) -> bool:
    position = start
    for left, right in sorted(intervals):
        if right < position - tolerance:
            continue
        if left > position + tolerance:
            return False
        position = max(position, right)
        if position >= end - tolerance:
            return True
    return position >= end - tolerance


def _pdf_segment_ruled_cell_grid(page: dict[str, Any], box: list[float]) -> tuple[list[dict[str, Any]], list[str], list[float]] | None:
    tolerance = max(3.0, page["page_image"]["width"] * 0.0025)
    expanded = [box[0] - tolerance * 2, box[1] - tolerance * 2, box[2] + tolerance * 2, box[3] + tolerance * 2]
    horizontal = []
    vertical = []
    for item in page["evidence"]["pdf"]:
        if item.get("kind") != "pdf_drawing":
            continue
        drawing_box = item["bbox_image_px"]
        if drawing_box[0] < expanded[0] or drawing_box[2] > expanded[2] or drawing_box[1] < expanded[1] or drawing_box[3] > expanded[3]:
            continue
        width = drawing_box[2] - drawing_box[0]
        height = drawing_box[3] - drawing_box[1]
        if width > tolerance * 2 and height <= 5:
            horizontal.append(((drawing_box[1] + drawing_box[3]) / 2, drawing_box[0], drawing_box[2], item["evidence_id"]))
        elif height > tolerance * 2 and width <= 5:
            vertical.append(((drawing_box[0] + drawing_box[2]) / 2, drawing_box[1], drawing_box[3], item["evidence_id"]))
    x_boundaries = _cluster_coordinates([item[0] for item in vertical], tolerance)
    y_boundaries = _cluster_coordinates([item[0] for item in horizontal], tolerance)
    if len(x_boundaries) < 2 or len(y_boundaries) < 2:
        return None

    def horizontal_intervals(y: float) -> list[tuple[float, float]]:
        return [(start, end) for position, start, end, _ in horizontal if abs(position - y) <= tolerance]

    def vertical_intervals(x: float) -> list[tuple[float, float]]:
        return [(start, end) for position, start, end, _ in vertical if abs(position - x) <= tolerance]

    if not (
        _interval_is_covered(horizontal_intervals(y_boundaries[0]), x_boundaries[0], x_boundaries[-1], tolerance)
        and _interval_is_covered(horizontal_intervals(y_boundaries[-1]), x_boundaries[0], x_boundaries[-1], tolerance)
        and _interval_is_covered(vertical_intervals(x_boundaries[0]), y_boundaries[0], y_boundaries[-1], tolerance)
        and _interval_is_covered(vertical_intervals(x_boundaries[-1]), y_boundaries[0], y_boundaries[-1], tolerance)
    ):
        return None

    row_count, column_count = len(y_boundaries) - 1, len(x_boundaries) - 1
    parents = {(row, col): (row, col) for row in range(row_count) for col in range(column_count)}

    def find(slot: tuple[int, int]) -> tuple[int, int]:
        while parents[slot] != slot:
            parents[slot] = parents[parents[slot]]
            slot = parents[slot]
        return slot

    def union(left: tuple[int, int], right: tuple[int, int]) -> None:
        parents[find(left)] = find(right)

    for row in range(row_count):
        for col in range(column_count - 1):
            if not _interval_is_covered(vertical_intervals(x_boundaries[col + 1]), y_boundaries[row], y_boundaries[row + 1], tolerance):
                union((row, col), (row, col + 1))
    for row in range(row_count - 1):
        for col in range(column_count):
            if not _interval_is_covered(horizontal_intervals(y_boundaries[row + 1]), x_boundaries[col], x_boundaries[col + 1], tolerance):
                union((row, col), (row + 1, col))

    components: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for slot in parents:
        components.setdefault(find(slot), set()).add(slot)
    cells = []
    for slots in components.values():
        row = min(slot[0] for slot in slots)
        row_end = max(slot[0] for slot in slots) + 1
        col = min(slot[1] for slot in slots)
        col_end = max(slot[1] for slot in slots) + 1
        expected = {(r, c) for r in range(row, row_end) for c in range(col, col_end)}
        if slots != expected:
            return None
        cells.append({
            "row": row,
            "col": col,
            "rowspan": row_end - row,
            "colspan": col_end - col,
            "bbox_image_px": [x_boundaries[col], y_boundaries[row], x_boundaries[col_end], y_boundaries[row_end]],
        })
    refs = list(dict.fromkeys([item[3] for item in horizontal + vertical]))
    return cells, refs, [x_boundaries[0], y_boundaries[0], x_boundaries[-1], y_boundaries[-1]]


REBUILD_CHAR_DELTA = 2
REBUILD_SYMBOL_DELTA = 2
REBUILD_LINE_IOA_MIN = 0.30
REBUILD_MIN_TEXT_CELL_RATIO = 0.30

# --- Word binding policy -------------------------------------------------------------------
# The document level score (whole-document text similarity) is a poor gate: a document whose text
# layer was re-flowed, re-ordered or partially regenerated scores 0.70-0.95 while every individual
# table still matches its native counterpart exactly. Word evidence is therefore admitted per table:
# the PDF fragment must be almost entirely covered by one native table, and the row range must match
# strongly. The document score now only decides whether Word rows may *replace* model HTML.
WORD_TABLE_COVERAGE_MIN = 0.55
WORD_BINDING_STRONG = 0.99
WORD_COVERAGE_MIN_WEAK = 0.75
WORD_ROW_MATCH_MIN_STRONG = 0.70
WORD_ROW_MATCH_MIN_WEAK = 0.88


def _private_use_free(value: str) -> str:
    return "".join(character for character in value if not "\ue000" <= character <= "\uf8ff")


def _only_decorative_characters(lost: Counter[str]) -> bool:
    """A symbol the PDF text layer draws as a private-use glyph is not content the rebuild dropped."""
    return all(
        not character.isalnum() and not "\u4e00" <= character <= "\u9fff"
        for character in lost.elements()
    )


def _content_multiset(value: str) -> Counter[str]:
    """Character content of a table, order free: cell boundaries may move without losing content."""
    return Counter(
        character
        for character in _private_use_free(_table_cell_text_norm(value))
        if not character.isspace()
    )


def _rebuild_cell_texts(cells: list[dict[str, Any]], lines: list[dict[str, Any]]) -> dict[tuple[int, int, int, int], str]:
    """Cell text for a rebuilt grid: every measured PDF line goes to the cell that contains it best."""
    owners: dict[tuple[int, int, int, int], list[dict[str, Any]]] = {
        (int(cell["row"]), int(cell["col"]), int(cell["rowspan"]), int(cell["colspan"])): [] for cell in cells
    }
    for line in lines:
        best: dict[str, Any] | None = None
        best_score = 0.0
        for cell in cells:
            score = _ioa(line["bbox_image_px"], cell["bbox_image_px"])
            if score > best_score:
                best, best_score = cell, score
        if best is None or best_score < REBUILD_LINE_IOA_MIN:
            continue
        owners[(int(best["row"]), int(best["col"]), int(best["rowspan"]), int(best["colspan"]))].append(line)
    texts: dict[tuple[int, int, int, int], str] = {}
    for key, owned in owners.items():
        owned.sort(key=lambda item: (float(item["bbox_image_px"][1]), float(item["bbox_image_px"][0])))
        # the measured text is written as it was measured: the comparison normalizer folds case and
        # punctuation, so it must never be the value that ends up in the document
        texts[key] = "\n".join(str(item.get("text", "")).strip() for item in owned if str(item.get("text", "")).strip())
    return texts


def _table_html_from_grid(cells: list[dict[str, Any]], texts: dict[tuple[int, int, int, int], str]) -> str:
    rows: dict[int, list[dict[str, Any]]] = {}
    for cell in cells:
        rows.setdefault(int(cell["row"]), []).append(cell)
    parts = ["<table>"]
    for row_index in sorted(rows):
        parts.append("<tr>")
        for cell in sorted(rows[row_index], key=lambda item: int(item["col"])):
            key = (int(cell["row"]), int(cell["col"]), int(cell["rowspan"]), int(cell["colspan"]))
            attributes = ""
            if int(cell["colspan"]) > 1:
                attributes += f' colspan="{int(cell["colspan"])}"'
            if int(cell["rowspan"]) > 1:
                attributes += f' rowspan="{int(cell["rowspan"])}"'
            parts.append(f"<td{attributes}>{html.escape(str(texts.get(key) or ''), quote=False)}</td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _rebuild_table_from_measured_grid(
    page: dict[str, Any],
    block: dict[str, Any],
    reviews: dict[str, Any],
    grid_method: str,
    cells: list[dict[str, Any]],
    drawing_refs: list[str],
    grid_box: list[float],
    lines: list[dict[str, Any]],
    previous_text: str,
    previous_available: bool,
) -> bool:
    """Adopt the measured grid as the table structure when the rebuild keeps the content.

    The model HTML was not confirmed by the PDF grid (different cell topology, or different text).
    The measured grid is then the structure authority and the cell text comes from the measured PDF
    lines. The rebuild is accepted only when the content the previous HTML carried survives at the
    character level, so nothing is silently dropped; otherwise the block keeps its CHECK state.
    """
    texts = _rebuild_cell_texts(cells, lines)
    filled = sum(1 for value in texts.values() if value)
    if not cells or filled < max(1, len(cells) * REBUILD_MIN_TEXT_CELL_RATIO):
        return False
    previous = _content_multiset(previous_text)
    rebuilt = _content_multiset("".join(texts.values()))
    if not rebuilt:
        return False
    lost = sum((previous - rebuilt).values())
    gained = sum((rebuilt - previous).values())
    # The rebuild replaces the model's markup, never its content: losing a word character, a digit
    # or a CJK character is not acceptable, while a couple of decorative symbols is formatting (the
    # PDF layer draws them as private-use glyphs that the comparison already folds away).
    if lost > 0 and not (_only_decorative_characters(previous - rebuilt) and lost <= REBUILD_SYMBOL_DELTA):
        return False
    # Surplus text is only accepted when there was no usable model content to compare against: that is
    # the model failing to produce the table, not content being replaced with something else.
    if gained > REBUILD_CHAR_DELTA and previous_available:
        return False
    block["html"] = _table_html_from_grid(cells, texts)
    block["bbox_image_px"] = [round(value, 3) for value in _union([grid_box, [float(v) for v in block["bbox_image_px"]]])]
    add_coordinate_views(block, page)
    refs = list(dict.fromkeys(block.get("support_refs", []) + block.get("source_refs", []) + drawing_refs))
    fields = reviews["blocks"][block["block_id"]]
    fields["type"] = {"status": "PASS", "method": "model_table_pdf_ruled_cell_matrix_rebuild", "evidence_refs": refs}
    fields["bbox"] = {"status": "PASS", "method": grid_method + "_union_rebuild", "evidence_refs": drawing_refs}
    fields["html"] = {
        "status": "PASS",
        "method": "pdf_ruled_cell_matrix_rebuild_char_conserved",
        "evidence_refs": refs,
        "model_html_chars": len(previous_text),
        "char_delta": {"lost": lost, "gained": gained},
    }
    block["table_cell_geometry"] = [
        {
            "row": int(cell["row"]),
            "col": int(cell["col"]),
            "rowspan": int(cell["rowspan"]),
            "colspan": int(cell["colspan"]),
            "bbox_image_px": [float(value) for value in cell["bbox_image_px"]],
            "text": str(texts.get((int(cell["row"]), int(cell["col"]), int(cell["rowspan"]), int(cell["colspan"]))) or ""),
        }
        for cell in cells
    ]
    block["table_grid_evidence"] = {
        "rows": max(int(cell["row"]) + int(cell["rowspan"]) for cell in cells),
        "cols": max(int(cell["col"]) + int(cell["colspan"]) for cell in cells),
        "physical_cells": len(cells),
        "method": grid_method + "_rebuild",
    }
    return True


def _confirm_ruled_table_cell_matrix(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> None:
    evidence = {item["evidence_id"]: item for item in page["evidence"]["pdf"]}
    for block in blocks:
        if block.get("type") != "table":
            continue
        html_value = block.get("html")
        html_cells = _html_table_grid(html_value) if isinstance(html_value, str) else None
        html_topology = {(cell["row"], cell["col"], cell["rowspan"], cell["colspan"]) for cell in html_cells or []}
        pdf_lines = [evidence[ref] for ref in block.get("source_refs", []) if evidence.get(ref, {}).get("kind") == "pdf_line"]
        accepted = None
        grid_options = [
            ("pdf_ruled_cell_rectangles", _pdf_ruled_cell_grid(page, block["bbox_image_px"])),
            ("pdf_ruled_line_segments", _pdf_segment_ruled_cell_grid(page, block["bbox_image_px"])),
        ]
        for grid_method, ruled in grid_options:
            if ruled is None or html_cells is None:
                continue
            pdf_cells, drawing_refs, grid_box = ruled
            pdf_topology = {(cell["row"], cell["col"], cell["rowspan"], cell["colspan"]) for cell in pdf_cells}
            if html_topology != pdf_topology or _iou(grid_box, block["bbox_image_px"]) < 0.75:
                continue
            assignments: dict[tuple[int, int, int, int], list[dict[str, Any]]] = {key: [] for key in pdf_topology}
            valid = True
            for line in pdf_lines:
                owners = [cell for cell in pdf_cells if _ioa(line["bbox_image_px"], cell["bbox_image_px"]) >= 0.70]
                if len(owners) != 1:
                    valid = False
                    break
                owner = owners[0]
                key = (owner["row"], owner["col"], owner["rowspan"], owner["colspan"])
                assignments[key].append(line)
            if not valid:
                continue
            for lines in assignments.values():
                lines.sort(key=lambda item: (item["bbox_image_px"][1], item["bbox_image_px"][0]))
            exact = all(cell["text"] == _table_cell_text_norm("".join(str(line.get("text", "")) for line in assignments[(cell["row"], cell["col"], cell["rowspan"], cell["colspan"])])) for cell in html_cells)
            if exact:
                accepted = (grid_method, pdf_cells, drawing_refs, grid_box)
                break
        if accepted is None:
            # The strict path needs the model HTML to match the measured grid exactly. When that
            # fails, the measured grid may still be adopted as the structure as long as rebuilding
            # the HTML keeps the content the model HTML carried (see the helper).
            for grid_method, ruled in grid_options:
                if ruled is None:
                    continue
                pdf_cells, drawing_refs, grid_box = ruled
                # compare against the model's *cell* content, not the markup: tag characters would
                # otherwise read as content loss and no rebuild would ever be accepted
                previous_text = (
                    "".join(str(cell.get("text") or "") for cell in html_cells)
                    if html_cells is not None
                    else str(block.get("text") or "")
                )
                if _rebuild_table_from_measured_grid(
                    page, block, reviews, grid_method, pdf_cells, drawing_refs, grid_box,
                    pdf_lines, previous_text, previous_available=html_cells is not None and bool(previous_text.strip()),
                ):
                    break
            continue
        grid_method, pdf_cells, drawing_refs, grid_box = accepted
        block["bbox_image_px"] = [round(value, 3) for value in grid_box]
        add_coordinate_views(block, page)
        refs = list(dict.fromkeys(block.get("support_refs", []) + block.get("source_refs", []) + drawing_refs))
        fields = reviews["blocks"][block["block_id"]]
        fields["type"] = {"status": "PASS", "method": "model_table_pdf_ruled_cell_matrix", "evidence_refs": refs}
        fields["bbox"] = {"status": "PASS", "method": grid_method + "_union", "evidence_refs": drawing_refs}
        fields["html"] = {"status": "PASS", "method": "model_html_pdf_ruled_cell_matrix_exact", "evidence_refs": refs}
        # Measured cell geometry of the verified grid: the Contract GT projection models table rows
        # and cells as first-class nodes with their own entity, which requires a real box per cell.
        block["table_cell_geometry"] = [
            {
                "row": int(cell["row"]),
                "col": int(cell["col"]),
                "rowspan": int(cell["rowspan"]),
                "colspan": int(cell["colspan"]),
                "bbox_image_px": [float(value) for value in cell["bbox_image_px"]],
                "text": str(cell.get("text") or ""),
            }
            for cell in pdf_cells
        ]
        block["table_grid_evidence"] = {
            "rows": max(cell["row"] + cell["rowspan"] for cell in pdf_cells),
            "cols": max(cell["col"] + cell["colspan"] for cell in pdf_cells),
            "physical_cells": len(pdf_cells),
            "method": grid_method,
        }


def _confirm_simple_ruled_table_html(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> None:
    drawings = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_drawing"]
    pdf_by_id = {item["evidence_id"]: item for item in page["evidence"]["pdf"]}
    for block in blocks:
        html_value = block.get("html")
        if block.get("type") != "table" or not isinstance(html_value, str):
            continue
        parser = _SimpleTableParser()
        try:
            parser.feed(html_value)
            parser.close()
        except Exception:
            continue
        if parser.tables != 1 or parser.depth != 0 or parser.rows < 1 or parser.cells < 2 or parser.has_spans:
            continue
        box = block["bbox_image_px"]
        tolerance = max(8.0, page["page_image"]["width"] * 0.01)
        expanded = [box[0] - tolerance, box[1] - tolerance, box[2] + tolerance, box[3] + tolerance]
        cell_drawings = [
            item for item in drawings
            if item["bbox_image_px"][2] - item["bbox_image_px"][0] > tolerance
            and item["bbox_image_px"][3] - item["bbox_image_px"][1] > tolerance
            and _ioa(item["bbox_image_px"], expanded) >= 0.95
        ]
        unique: dict[tuple[float, float, float, float], dict[str, Any]] = {}
        for item in cell_drawings:
            key = tuple(round(value, 1) for value in item["bbox_image_px"])
            unique.setdefault(key, item)
        cells = list(unique.values())
        if len(cells) != parser.cells:
            continue
        grid_box = _union([item["bbox_image_px"] for item in cells])
        if _iou(grid_box, box) < 0.75:
            continue
        pdf_text = "".join(str(pdf_by_id.get(ref, {}).get("text", "")) for ref in block.get("source_refs", []))
        if normalize_word_text("".join(parser.text_parts)) != normalize_word_text(pdf_text):
            continue
        drawing_refs = [item["evidence_id"] for item in cells]
        block["bbox_image_px"] = grid_box
        add_coordinate_views(block, page)
        fields = reviews["blocks"][block["block_id"]]
        fields["type"] = {"status": "PASS", "method": "model_table_pdf_simple_grid", "evidence_refs": block.get("support_refs", []) + drawing_refs}
        fields["bbox"] = {"status": "PASS", "method": "pdf_ruled_cell_union", "evidence_refs": drawing_refs}
        fields["html"] = {"status": "PASS", "method": "model_html_pdf_simple_grid_exact_text", "evidence_refs": block.get("support_refs", []) + block.get("source_refs", []) + drawing_refs}


def _parse_table_html(html_value: str) -> tuple[Any | None, list[Any]]:
    """Parse table HTML with a tolerant parser so previously embedded void <img> tokens do not break re-parsing."""
    if _lxml_html is not None:
        try:
            root = _lxml_html.fromstring(html_value)
        except Exception:
            return None, []
        return root, [element for element in root.iter() if str(getattr(element, "tag", "")).lower() == "tr"]
    try:
        root = ET.fromstring(html_value)
    except ET.ParseError:
        return None, []
    return root, [element for element in root.iter() if element.tag.lower() == "tr"]


def _serialize_table_html(root: Any) -> str:
    if _lxml_html is not None:
        return _lxml_html.tostring(root, encoding="unicode", method="html")
    return ET.tostring(root, encoding="unicode", method="html")


def _new_image_element(cell: Any) -> Any:
    if _lxml_html is not None:
        image = _lxml_html.Element("img")
        cell.append(image)
        return image
    return ET.SubElement(cell, "img")


def _embed_image_token(html_value: str, image_ref: str, row: int, col: int, rowspan: int, colspan: int) -> str | None:
    root, rows = _parse_table_html(html_value)
    if root is None or row >= len(rows):
        return None
    target_row = rows[row]
    logical_col = 0
    target_cell = None
    for cell in list(target_row):
        if str(getattr(cell, "tag", "")).lower() not in {"td", "th"}:
            continue
        span = max(1, int(cell.attrib.get("colspan", "1")))
        if logical_col <= col < logical_col + span:
            target_cell = cell
            break
        logical_col += span
    if target_cell is None:
        if col != logical_col:
            return None
        if _lxml_html is not None:
            target_cell = _lxml_html.Element("td")
            target_row.append(target_cell)
        else:
            target_cell = ET.SubElement(target_row, "td")
        if rowspan > 1:
            target_cell.set("rowspan", str(rowspan))
        if colspan > 1:
            target_cell.set("colspan", str(colspan))
    existing = next((node for node in target_cell.iter() if str(getattr(node, "tag", "")).lower() == "img"), None)
    image = existing if existing is not None else _new_image_element(target_cell)
    image.attrib.clear()
    image.set("data-image-ref", image_ref)
    image.set("alt", "")
    return _serialize_table_html(root)


def _bind_table_embedded_images(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> None:
    pdf = page["evidence"]["pdf"]
    drawings = [item for item in pdf if item.get("kind") == "pdf_drawing"]
    model_index = {item["evidence_id"]: item for source in ("mineru", "paddle") for item in page["evidence"].get(source, [])}
    image_blocks = [block for block in blocks if block.get("type") == "image" and len(block.get("source_refs", [])) == 1]
    removed: set[str] = set()
    for image_block in image_blocks:
        image_ref = image_block["source_refs"][0]
        image_evidence = next((item for item in pdf if item.get("evidence_id") == image_ref and item.get("kind") == "pdf_image"), None)
        if image_evidence is None:
            continue
        image_box = image_evidence.get("effective_bbox_image_px", image_evidence["bbox_image_px"])
        owners = [block for block in blocks if block.get("type") == "table" and _table_owns_picture(block["bbox_image_px"], image_box)]
        if len(owners) != 1:
            continue
        table = owners[0]
        html_sources = [model_index[ref] for ref in table.get("support_refs", []) if ref in model_index and re.search(r"<img\b", str(model_index[ref].get("html", "")), flags=re.IGNORECASE)]
        image_models = [model_index[ref] for ref in image_block.get("support_refs", []) if ref in model_index and _iou(model_index[ref]["bbox_image_px"], image_box) >= 0.50]
        box = table["bbox_image_px"]
        width, height = box[2] - box[0], box[3] - box[1]
        vertical = [item for item in drawings if item["bbox_image_px"][2] - item["bbox_image_px"][0] <= 5 and item["bbox_image_px"][3] - item["bbox_image_px"][1] >= height * 0.75 and box[0] - 8 <= (item["bbox_image_px"][0] + item["bbox_image_px"][2]) / 2 <= box[2] + 8]
        horizontal = [item for item in drawings if item["bbox_image_px"][3] - item["bbox_image_px"][1] <= 5 and item["bbox_image_px"][2] - item["bbox_image_px"][0] >= width * 0.30 and box[1] - 8 <= (item["bbox_image_px"][1] + item["bbox_image_px"][3]) / 2 <= box[3] + 8]
        # Word draws one border segment per cell and may leave the outer edge implicit: merge
        # collinear segments and add the table's own edges so the grid always closes.
        xs = _merge_positions([(item["bbox_image_px"][0] + item["bbox_image_px"][2]) / 2 for item in vertical] + [box[0], box[2]])
        ys = _merge_positions([(item["bbox_image_px"][1] + item["bbox_image_px"][3]) / 2 for item in horizontal] + [box[1], box[3]])
        cx, cy = (image_box[0] + image_box[2]) / 2, (image_box[1] + image_box[3]) / 2
        col = next((index for index in range(len(xs) - 1) if xs[index] <= cx <= xs[index + 1]), None)
        row = next((index for index in range(len(ys) - 1) if ys[index] <= cy <= ys[index + 1]), None)
        if col is None or row is None:
            continue
        # The row span follows the grid around the picture centre: the table's own edges close the
        # grid, so a picture in the last row does not need a drawn border underneath it.
        top = max((y for y in ys if y <= cy), default=None)
        bottom = min((y for y in ys if y > cy), default=None)
        if top is None or bottom is None:
            continue
        row_start, row_end = ys.index(top), ys.index(bottom)
        current_html = str(table.get("html", ""))
        cell = _html_cell_at(current_html, row_start, col)
        cell_refs = [
            node.attrib.get("data-image-ref")
            for node in (cell.iter() if cell is not None else [])
            if node.tag.lower() == "img" and node.attrib.get("data-image-ref")
        ]
        if not html_sources and (cell is None or _cell_visible_text(cell)):
            continue
        if cell_refs and image_ref not in cell_refs:
            html_value = _append_image_token(current_html, image_ref, row_start, col)
        else:
            html_value = _embed_image_token(current_html, image_ref, row_start, col, row_end - row_start, 1)
        if html_value is None:
            continue
        if any(f'data-image-ref="{item["image_ref"]}"' not in html_value for item in table.get("embedded_images", [])):
            continue
        table["html"] = html_value
        table["source_refs"] = list(dict.fromkeys(table.get("source_refs", []) + [image_ref]))
        table["support_refs"] = list(dict.fromkeys(table.get("support_refs", []) + image_block.get("support_refs", [])))
        table.setdefault("embedded_images", []).append({
            "image_ref": image_ref, "row": row_start, "col": col, "rowspan": row_end - row_start, "colspan": 1,
            "bbox_image_px": image_box, "bbox_pdf_pt": image_evidence.get("bbox_pdf_pt"), "status": "PASS",
            "method": "pdf_grid_model_table_image_binding",
            "evidence_refs": [image_ref] + [item["evidence_id"] for item in html_sources + image_models + vertical + horizontal],
        })
        reviews["blocks"][table["block_id"]]["embedded_images"] = {"status": "PASS", "method": "pdf_grid_model_table_image_binding", "evidence_refs": table["embedded_images"][-1]["evidence_refs"]}
        removed.add(image_block["block_id"])
    if removed:
        blocks[:] = [block for block in blocks if block["block_id"] not in removed]
        for block_id in removed:
            reviews["blocks"].pop(block_id, None)
        blocks.sort(key=lambda block: (block["bbox_image_px"][1], block["bbox_image_px"][0]))
        for order, block in enumerate(blocks):
            block["reading_order"] = order


def _graphic_region_refs(page: dict[str, Any], box: list[float]) -> list[str]:
    """Evidence ids grounding an image region that is drawn from several pictures and vector shapes."""
    if not _region_graphic_structure(page, box):
        return []
    items = page.get("evidence", {}).get("pdf", [])
    inside = [
        item for item in items
        if item.get("kind") in {"pdf_image", "pdf_drawing"} and _ioa(item["bbox_image_px"], box) >= 0.5
    ]
    if not inside:
        return []
    if _box_area(_union([item["bbox_image_px"] for item in inside])) < _box_area(box) * 0.5:
        return []
    return [str(item["evidence_id"]) for item in inside][:12]


def _absorb_blocks_inside_graphic_images(pages: list[dict[str, Any]]) -> None:
    """Blocks drawn inside a diagram belong to that diagram, not to the page flow.

    Text labels become part of the image (their required PDF lines release coverage); nested
    pictures transfer their evidence to the enclosing diagram so nothing is claimed twice.
    """
    for page in pages:
        blocks = page["current_candidate"]["blocks"]
        reviews = page["field_reviews"]
        evidence = {
            str(item["evidence_id"]): item
            for item in page.get("evidence", {}).get("pdf", [])
        }
        graphic = [
            block for block in blocks
            if block.get("type") in {"image", "chart", "seal"} and _region_graphic_structure(page, block["bbox_image_px"])
        ]
        # Keep only the outermost containers: an icon inside a diagram is itself a graphic region.
        graphic = [
            block for block in graphic
            if not any(
                other is not block
                and _box_area(other["bbox_image_px"]) > _box_area(block["bbox_image_px"])
                and _contains_box(other["bbox_image_px"], block["bbox_image_px"])
                for other in graphic
            )
        ]
        if not graphic:
            continue
        # A diagram is drawn from many pictures and vector shapes: its true region is the union of the
        # model box and the graphic evidence that lies mostly inside it. Without this the region box is
        # smaller than the parts it owns, which both blocks absorption and breaks evidence containment.
        graphic_items = [item for item in evidence.values() if item.get("kind") in {"pdf_image", "pdf_drawing"}]
        for owner in graphic:
            box = owner["bbox_image_px"]
            parts = [
                [float(value) for value in item["bbox_image_px"]]
                for item in graphic_items
                if isinstance(item.get("bbox_image_px"), list) and len(item["bbox_image_px"]) == 4 and _ioa(item["bbox_image_px"], box) >= 0.5
            ]
            if not parts:
                continue
            region = _union([box] + parts)
            if region == box or _box_area(region) > _box_area(box) * 1.6:
                continue
            owner["bbox_image_px"] = region
            add_coordinate_views(owner, page)
        absorbed: set[str] = set()
        nested: dict[str, list[str]] = {}
        for block in blocks:
            if block in graphic:
                continue
            owner = next(
                (image for image in graphic if _ioa(block["bbox_image_px"], image["bbox_image_px"]) >= 0.85),
                None,
            )
            if owner is None:
                continue
            if block.get("type") in {"text", "title", "unknown"} and str(block.get("text", "")).strip():
                pass
            elif block.get("type") in {"image", "chart", "seal"}:
                pass
            else:
                continue
            absorbed.add(block["block_id"])
            nested.setdefault(owner["block_id"], []).extend(str(ref) for ref in block.get("source_refs", []))
        if not absorbed:
            continue
        blocks[:] = [block for block in blocks if block["block_id"] not in absorbed]
        for order, block in enumerate(blocks):
            block["reading_order"] = order
        for block_id in absorbed:
            reviews.get("blocks", {}).pop(block_id, None)
        claimed = {str(ref) for block in blocks for ref in block.get("source_refs", [])}
        for block_id, refs in nested.items():
            owner = next((block for block in blocks if block["block_id"] == block_id), None)
            if owner is None:
                continue
            unique = list(dict.fromkeys(refs))
            gained = [ref for ref in unique if evidence.get(ref, {}).get("kind") == "pdf_image" and ref not in claimed]
            inside_pictures = [
                str(item["evidence_id"])
                for item in evidence.values()
                if item.get("kind") == "pdf_image"
                and item.get("required_coverage")
                and item["evidence_id"] not in claimed
                and _contains_box(owner["bbox_image_px"], item["bbox_image_px"])
            ]
            gained = list(dict.fromkeys(gained + inside_pictures))
            if gained:
                owner["source_refs"] = sorted(set(str(ref) for ref in owner.get("source_refs", [])) | set(gained))
                claimed.update(gained)
            fields = reviews.setdefault("blocks", {}).setdefault(block_id, {})
            fields["nested_native_text"] = {
                "status": "PASS",
                "method": "diagram_labels_belong_to_image",
                "evidence_refs": unique[:24],
            }
            if gained:
                fields.setdefault("bbox", {})["evidence_refs"] = list(owner["source_refs"])[:12]
                fields["bbox"]["status"] = "PASS"
                fields["bbox"]["method"] = "pdf_graphic_region_bbox"
            for ref in unique:
                item = evidence.get(ref)
                if item is not None and item.get("kind") == "pdf_line":
                    item["required_coverage"] = False
        _enforce_evidence_containment(page, blocks, reviews)


def _merge_positions(values: list[float], tolerance: float = 3.0) -> list[float]:
    """Collapse near-duplicate rule coordinates so a cell grid is not split by sub-pixel noise."""
    merged: list[float] = []
    for value in sorted(float(item) for item in values):
        if merged and value - merged[-1] <= tolerance:
            merged[-1] = (merged[-1] + value) / 2
            continue
        merged.append(value)
    return merged


def _table_owns_picture(table_box: list[float], image_box: list[float], ratio: float = 0.70) -> bool:
    """True when a picture belongs to a table cell.

    The model table box is regularly a few percent short of the ruled table, so containment is judged
    by how much of the picture lies on the table together with the picture centre.
    """
    ioa = _ioa(image_box, table_box)
    if ioa >= 0.95:
        return True
    if ioa < ratio:
        return False
    centre_x = (image_box[0] + image_box[2]) / 2
    centre_y = (image_box[1] + image_box[3]) / 2
    slack = max(6.0, (image_box[2] - image_box[0]) * 0.35)
    return (
        table_box[0] - slack <= centre_x <= table_box[2] + slack
        and table_box[1] - slack <= centre_y <= table_box[3] + slack
    )


def _enforce_evidence_containment(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> None:
    """A block must geometrically contain every PDF evidence item it claims.

    Several passes legitimately rewrite a block box (table cell grids, embedded pictures, absorbed
    diagram parts). The revision contract still requires containment, so the box is extended to the
    union of its claimed evidence instead of leaving an unverifiable block behind.
    """
    evidence = {str(item["evidence_id"]): item for item in page.get("evidence", {}).get("pdf", [])}
    for block in blocks:
        boxes: list[list[float]] = []
        for ref in block.get("source_refs", []):
            item = evidence.get(str(ref))
            if not item:
                continue
            for key in ("bbox_image_px", "effective_bbox_image_px"):
                value = item.get(key)
                if isinstance(value, list) and len(value) == 4:
                    boxes.append([float(value[0]), float(value[1]), float(value[2]), float(value[3])])
        if not boxes:
            continue
        needed = _union(boxes)
        if _contains_box(block["bbox_image_px"], needed):
            continue
        block["bbox_image_px"] = _union([block["bbox_image_px"], needed])
        add_coordinate_views(block, page)
        fields = reviews.setdefault("blocks", {}).setdefault(block["block_id"], {})
        fields["bbox"] = {
            "status": "PASS",
            "method": "pdf_evidence_containment_union",
            "evidence_refs": [str(ref) for ref in block.get("source_refs", [])][:12],
        }


def _contains_box(outer: list[float], inner: list[float], tolerance: float = 2.0) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and outer[2] + tolerance >= inner[2]
        and outer[3] + tolerance >= inner[3]
    )


def _region_graphic_structure(page: dict[str, Any], box: list[float]) -> bool:
    """True when a region carries real graphics (several raster pictures or vector shapes).

    A text layer rendered as one picture has no rasters of its own and at most a background rule;
    a diagram (flow chart, topology, chart) carries many drawings and often several embedded images.
    """
    items = page.get("evidence", {}).get("pdf", [])
    drawings = [item for item in items if item.get("kind") == "pdf_drawing" and _ioa(item["bbox_image_px"], box) >= 0.5]
    rasters = [item for item in items if item.get("kind") == "pdf_image" and _ioa(item["bbox_image_px"], box) >= 0.5]
    return len(rasters) >= 2 or len(drawings) >= 4 or any(int(item.get("item_count") or 1) >= 4 for item in drawings)


def _drop_vector_container_images(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], pdf_lines: list[dict[str, Any]]) -> None:
    """A model-detected image whose region is carried by native PDF text lines is a vector diagram container: keep the text, drop the image."""
    text_kinds = {"text", "title", "header", "footer", "page_number", "table_caption", "image_caption", "unknown"}
    removed: set[str] = set()
    for block in [item for item in blocks if item.get("type") == "image" and not item.get("source_refs")]:
        box = block["bbox_image_px"]
        lines = [line for line in pdf_lines if str(line.get("text", "")).strip() and _ioa(line["bbox_image_px"], box) >= 0.5]
        covered = [
            item for item in blocks
            if item is not block and item.get("type") in text_kinds and _ioa(item["bbox_image_px"], box) >= 0.5
        ]
        if len(lines) < 6 or len(covered) < 3:
            continue
        if _region_graphic_structure(page, box):
            # the labels belong to a drawing, not a text container: keep the figure and its native labels
            continue
        removed.add(block["block_id"])
        reviews.get("blocks", {}).pop(block["block_id"], None)
        anchor = max(covered, key=lambda item: _box_area(item["bbox_image_px"]))
        reviews.setdefault("blocks", {}).setdefault(anchor["block_id"], {})["vector_container_image_dropped"] = {
            "status": "PASS",
            "method": "vector_container_text_native",
            "evidence_refs": [line["evidence_id"] for line in lines],
        }
    if removed:
        blocks[:] = [item for item in blocks if item["block_id"] not in removed]
        for order, item in enumerate(blocks):
            item["reading_order"] = order



def _consolidate_nested_text_blocks(
    page: dict[str, Any],
    blocks: list[dict[str, Any]],
    reviews: dict[str, Any],
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """One visual line must be one block: overlapping text blocks are an invariant violation.

    Three shapes reach here, and each has exactly one sensible resolution:

    * **duplicate** - the parser emitted a fragment and the full line over the same pixels (``感谢您的观看``
      beside ``感谢您的观看\nTHANKS FOR YOUR TIME``): keep the verifiable, more complete block.
    * **container + content** - one block spans a region (a paragraph, an algorithm listing) and other
      blocks are its lines, extracted slightly differently. The container is what the model meant, so
      the inner blocks are merged into it.
    * **page-sized container** - a block covering (almost) the whole page holding a couple of
      characters, with real blocks inside it. That box is spurious: the inner blocks survive.

    The survivor keeps every evidence reference of the loser, so nothing loses its grounding.
    """
    page_width = float((page.get("page_image") or {}).get("width") or 0)
    page_height = float((page.get("page_image") or {}).get("height") or 0)
    page_area = page_width * page_height
    textual = [block for block in blocks if block.get("type") in TEXT_TYPES | {"unknown", "formula"}]
    dropped: set[str] = set()
    merged: list[tuple[str, str, str]] = []

    def keep_score(block: dict[str, Any]) -> tuple:
        refs = block.get("source_refs") or []
        line_refs = sum(1 for ref in refs if str(ref).startswith("pdf.line."))
        return (
            1 if refs else 0,                     # verified against the PDF text layer wins
            line_refs,                            # ... and a line reference beats an image region
            0 if block.get("type") == "unknown" else 1,
            len(_duplicate_norm(str(block.get("text", "")))),
            _box_area(block["bbox_image_px"]),
            -block.get("reading_order", 0),
        )

    def merge_into(survivor: dict[str, Any], loser: dict[str, Any], relation: str, absorb_refs: bool = True) -> None:
        # Form/field lines are never containers: "合同编号：____" above "签订地点：____" looks like a
        # 3-line block containing 2 one-line blocks, but the annotators keep every field separate.
        def _is_form_line(block: dict[str, Any]) -> bool:
            text = str(block.get("text") or "").strip()
            if not text:
                return False
            lines = text.split("\n")
            return len(lines) <= 3 and any(
                "___" in line or line.rstrip().endswith(("：", ":")) or "版本" in line and len(line) <= 14
                for line in lines
            )
        if _is_form_line(survivor) or _is_form_line(loser):
            return
        # A spurious page-sized container lends no evidence: its references may sit anywhere on the page
        # and would leave the surviving block unable to cover them (``bbox_does_not_cover_evidence``).
        if absorb_refs:
            survivor["source_refs"] = list(dict.fromkeys(list(survivor.get("source_refs", [])) + list(loser.get("source_refs", []))))
            survivor["support_refs"] = list(dict.fromkeys(list(survivor.get("support_refs", [])) + list(loser.get("support_refs", []))))
        survivor["type_candidates"] = sorted(set(survivor.get("type_candidates", [])) | set(loser.get("type_candidates", [])))
        if survivor.get("type") == "unknown" and loser.get("type") not in (None, "unknown"):
            survivor["type"] = loser["type"]
            if isinstance(loser.get("level"), int):
                survivor["level"] = loser["level"]
        if loser.get("text") and len(str(loser["text"])) > len(str(survivor.get("text", ""))) and survivor.get("source_refs"):
            survivor["text"] = loser["text"]
        # The survivor must cover every reference it now owns, so the box grows to the union - except for
        # the spurious page-sized container, whose references are dropped instead (absorb_refs=False).
        # A grown box can overlap a third block; the pass is repeatable and converges.
        if absorb_refs:
            survivor["bbox_image_px"] = _union([survivor["bbox_image_px"], loser["bbox_image_px"]])
            add_coordinate_views(survivor, page)
        reviews.get("blocks", {}).pop(loser["block_id"], None)
        dropped.add(loser["block_id"])
        merged.append((survivor["block_id"], loser["block_id"], relation))

    for index, left in enumerate(textual):
        if left["block_id"] in dropped:
            continue
        for right in textual[index + 1:]:
            if right["block_id"] in dropped:
                continue
            left_box, right_box = left["bbox_image_px"], right["bbox_image_px"]
            smaller, larger = (left, right) if _box_area(left_box) <= _box_area(right_box) else (right, left)
            slack = max(3.0, min(smaller["bbox_image_px"][2] - smaller["bbox_image_px"][0], smaller["bbox_image_px"][3] - smaller["bbox_image_px"][1]) * 0.25)
            covered = _ioa(smaller["bbox_image_px"], [
                larger["bbox_image_px"][0] - slack, larger["bbox_image_px"][1] - slack,
                larger["bbox_image_px"][2] + slack, larger["bbox_image_px"][3] + slack,
            ])
            if covered < 0.80:
                continue
            left_text = _duplicate_norm(str(left.get("text", "")))
            right_text = _duplicate_norm(str(right.get("text", "")))
            if not left_text or not right_text:
                continue
            if left_text == right_text:
                relation = "same_text"
            elif len(left_text) >= 4 and left_text in right_text:
                relation = "left_inside_right"
            elif len(right_text) >= 4 and right_text in left_text:
                relation = "right_inside_left"
            else:
                ratio = difflib.SequenceMatcher(None, left_text, right_text).ratio()
                relation = f"similar_text_{int(ratio * 100)}" if ratio >= 0.6 else ""
            survivor = loser = None
            if relation:
                survivor, loser = (left, right) if keep_score(left) >= keep_score(right) else (right, left)
            else:
                # Container and content: the inner box sits inside the outer one, with wording that is
                # not a copy of it (a paragraph versus its lines).
                # "The small box sits inside the big one" is containment whatever the area ratio: a
                # line inside a paragraph, a caption inside a figure, a formula fragment inside a
                # formula. Both cannot survive the no-overlap invariant, so the container wins.
                # 0.80 is the validator's own threshold: matching it is what makes the repair converge.
                nested = _ioa(smaller["bbox_image_px"], larger["bbox_image_px"]) >= 0.80
                if not nested:
                    continue
                spurious_container = (
                    page_area > 0
                    and _box_area(larger["bbox_image_px"]) >= 0.80 * page_area
                    and len(_duplicate_norm(str(larger.get("text") or ""))) <= 24
                )
                if spurious_container:
                    survivor, loser, relation = smaller, larger, "page_sized_container_dropped"
                    merge_into(survivor, loser, relation, absorb_refs=False)
                    if survivor is left:
                        break
                    continue
                survivor, loser, relation = larger, smaller, "nested_content_merged"
            merge_into(survivor, loser, relation)
            if left["block_id"] in dropped:
                # the candidate itself is gone: its remaining pairs are the survivor's problem now
                break

    if not dropped:
        return blocks, issues, set()
    for survivor_id, loser_id, relation in merged:
        issues.append({
            "issue_id": f"issue.consolidated.{survivor_id}.{loser_id}",
            "type": "nested_text_block_consolidated",
            "block_ids": [survivor_id, loser_id],
            "note": relation,
        })
    kept = [block for block in blocks if block["block_id"] not in dropped]
    return kept, issues, dropped


def _enforce_single_image_owner(
    page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> None:
    """A raster belongs to exactly one block (validator: ``duplicate_image_claim``).

    A table that absorbed pictures into its cells and a separate picture block over the same region can
    both list the same ``pdf.image`` references. The block that actually *draws* the picture wins - a
    block whose HTML references it (``data-image-ref``), else a typed picture block, else the tightest
    box - and the others drop the reference from their sources.
    """
    owners: dict[str, list[dict[str, Any]]] = {}
    for block in blocks:
        for ref in block.get("source_refs", []) or []:
            if str(ref).startswith("pdf.image."):
                owners.setdefault(str(ref), []).append(block)
    for ref, claimants in owners.items():
        if len(claimants) < 2:
            continue

        def rank(block: dict[str, Any]) -> tuple:
            html = str(block.get("html") or "")
            return (
                1 if f'data-image-ref="{ref}"' in html else 0,
                1 if block.get("type") in {"image", "chart", "seal"} else 0,
                -_box_area(block["bbox_image_px"]),
            )

        winner = max(claimants, key=rank)
        for block in claimants:
            if block is winner:
                continue
            block["source_refs"] = [value for value in block.get("source_refs", []) if str(value) != ref]
            issues.append({
                "issue_id": f"issue.{block['block_id']}.image_owner",
                "type": "duplicate_image_claim_resolved",
                "block_ids": [winner["block_id"], block["block_id"]],
                "note": ref,
            })


def _model_page_orders(blocks: list[dict[str, Any]], models: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Each parser's own page order (its evidence sequence), mapped onto our blocks."""
    owner: dict[str, list[str]] = {}
    for block in blocks:
        for ref in block.get("support_refs", []) or []:
            owner.setdefault(str(ref), []).append(block["block_id"])
    orders: dict[str, list[str]] = {}
    for model in ("mineru", "paddle"):
        items = sorted(
            (item for item in models if item.get("model") == model),
            key=lambda item: str(item.get("evidence_id") or ""),
        )
        order: list[str] = []
        for item in items:
            for block_id in owner.get(str(item.get("evidence_id")), []):
                if block_id not in order:
                    order.append(block_id)
        if len(order) >= 3:
            orders[model] = order
    return orders


def _apply_reading_order(
    page: dict[str, Any],
    blocks: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    models: list[dict[str, Any]],
) -> None:
    """Column-aware order for the page, with the measured structure kept as evidence."""
    if len(blocks) < 4:
        for order, block in enumerate(blocks):
            block["reading_order"] = order
        return
    width = float(page["page_image"]["width"])
    height = float(page["page_image"]["height"])
    result = build_page_order(blocks, width, height, model_orders=_model_page_orders(blocks, models))
    position = {block_id: index for index, block_id in enumerate(result["order"])}
    blocks.sort(key=lambda block: position.get(block["block_id"], len(blocks)))
    for order, block in enumerate(blocks):
        block["reading_order"] = order
    layout = result["layout"]
    page["reading_order"] = layout
    if layout["confidence"] != "PASS":
        issues.append({
            "issue_id": f"issue.p{page['page']}.reading_order",
            "type": "reading_order_review",
            "block_ids": [],
            "note": f"columns={layout['columns']} method={layout['method']} warnings={layout['warnings']}",
        })


def _merge_duplicate_image_blocks(blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> set[str]:
    """Two blocks that claim the same required PDF image with overlapping boxes are one object.

    The parsers may disagree about the label (MinerU `image` vs Paddle `chart`), which used to leave
    two blocks owning the same raster - something the validator rejects as ``duplicate_image_claim``.
    The block with the strongest model support wins; the loser's type candidate is folded into it.
    """
    family = {"image", "chart", "seal"}

    def required_images(block: dict[str, Any]) -> set[str]:
        return {ref for ref in block.get("source_refs", []) if str(ref).startswith("pdf.image.")}

    ordered = sorted(
        (block for block in blocks if block.get("type") in family and required_images(block)),
        key=lambda block: (-len(block.get("support_refs", [])), -_box_area(block["bbox_image_px"]), block["block_id"]),
    )
    merged: set[str] = set()
    for index, owner in enumerate(ordered):
        if owner["block_id"] in merged:
            continue
        owner_refs = required_images(owner)
        for other in ordered[index + 1:]:
            if other["block_id"] in merged or not (owner_refs & required_images(other)):
                continue
            if _iou(owner["bbox_image_px"], other["bbox_image_px"]) < 0.85:
                continue
            merged.add(other["block_id"])
            candidates = list(dict.fromkeys(list(owner.get("type_candidates", [])) + list(other.get("type_candidates", []))))
            owner["type_candidates"] = candidates
            owner["support_refs"] = list(dict.fromkeys(list(owner.get("support_refs", [])) + list(other.get("support_refs", []))))
            # block-level issues stay plain strings (that is the established contract); the merge is
            # reported as a page level conflict by the caller instead
            reviews.get("blocks", {}).pop(other["block_id"], None)
    if merged:
        blocks[:] = [block for block in blocks if block["block_id"] not in merged]
        for order, block in enumerate(blocks):
            block["reading_order"] = order
    return merged


def _absorb_nested_pdf_images(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> None:
    """Redundant pdf images (near-identical repeats, or sub-images of a larger image block) collapse into one visible image."""
    images = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_image"]
    by_ref = {item["evidence_id"]: item for item in images}
    image_blocks = [block for block in blocks if block.get("type") == "image"]

    def box_of(block: dict[str, Any]) -> list[float]:
        if not block.get("source_refs"):
            return block["bbox_image_px"]
        item = by_ref.get(block["source_refs"][0], {})
        return item.get("effective_bbox_image_px", item.get("bbox_image_px", block["bbox_image_px"]))

    released: set[str] = set()

    def release(block: dict[str, Any]) -> None:
        if block.get("source_refs"):
            item = by_ref.get(block["source_refs"][0])
            if item is not None:
                item["required_coverage"] = False
        released.add(block["block_id"])
        reviews.get("blocks", {}).pop(block["block_id"], None)

    ordered = sorted(image_blocks, key=lambda block: -_box_area(box_of(block)))
    for index, outer in enumerate(ordered):
        if outer["block_id"] in released:
            continue
        outer_box = box_of(outer)
        for inner in ordered[index + 1:]:
            if inner["block_id"] in released:
                continue
            inner_box = box_of(inner)
            same_region = _iou(outer_box, inner_box) >= 0.9
            nested_region = _box_area(outer_box) >= _box_area(inner_box) * 4 and _ioa(inner_box, outer_box) >= 0.9
            if same_region or nested_region:
                release(inner)
    if released:
        blocks[:] = [block for block in blocks if block["block_id"] not in released]
        for order, block in enumerate(blocks):
            block["reading_order"] = order


def _html_cell_at(html_value: str, row: int, col: int) -> Any | None:
    """Return the td/th covering (row, col), honouring rowspan/colspan from earlier rows."""
    root, rows = _parse_table_html(html_value)
    return _cell_in_rows(rows, row, col)


def _cell_in_rows(rows: list[Any], row: int, col: int) -> Any | None:
    if not rows or row >= len(rows):
        return None
    occupancy: dict[tuple[int, int], Any] = {}
    for row_index, tr in enumerate(rows):
        logical = 0
        for cell in list(tr):
            if str(getattr(cell, "tag", "")).lower() not in {"td", "th"}:
                continue
            colspan = max(1, int(cell.attrib.get("colspan", "1")))
            rowspan = max(1, int(cell.attrib.get("rowspan", "1")))
            while (row_index, logical) in occupancy:
                logical += 1
            for r in range(row_index, row_index + rowspan):
                for c in range(logical, logical + colspan):
                    occupancy.setdefault((r, c), cell)
            logical += colspan
    return occupancy.get((row, col))


def _cell_visible_text(cell: Any | None) -> str:
    if cell is None:
        return ""
    return "".join(cell.itertext()).strip()


def _append_image_token(html_value: str, image_ref: str, row: int, col: int) -> str | None:
    root, rows = _parse_table_html(html_value)
    if root is None:
        return None
    cell = _cell_in_rows(rows, row, col)
    if cell is None:
        return None
    image = _new_image_element(cell)
    image.set("data-image-ref", image_ref)
    image.set("alt", "")
    return _serialize_table_html(root)


def _nearest_empty_cell(html_value: str, row: int, col: int) -> tuple[int, int] | None:
    """Closest empty cell to (row, col): same column first, then the nearest row overall."""
    root, rows = _parse_table_html(html_value)
    if root is None or not rows:
        return None
    best: tuple[int, int, int] | None = None
    for row_index, tr in enumerate(rows):
        logical = 0
        for cell in list(tr):
            if str(getattr(cell, "tag", "")).lower() not in {"td", "th"}:
                continue
            span = max(1, int(cell.attrib.get("colspan", "1")))
            has_picture = any(str(getattr(node, "tag", "")).lower() == "img" for node in cell.iter())
            if not _cell_visible_text(cell) and not has_picture:
                for offset in range(span):
                    logical_col = logical + offset
                    candidate = (abs(logical_col - col), abs(row_index - row), row_index)
                    candidate_key = (candidate[0], candidate[1])
                    if best is None or candidate_key < (best[0], best[1]):
                        best = candidate + (logical_col,)
            logical += span
    if best is None:
        return None
    return best[2], best[3]


def _table_cell_for_image(table: dict[str, Any], drawings: list[dict[str, Any]], image_box: list[float]) -> tuple[int, int] | None:
    box = table["bbox_image_px"]
    width, height = box[2] - box[0], box[3] - box[1]
    vertical = [item for item in drawings if item["bbox_image_px"][2] - item["bbox_image_px"][0] <= 5 and item["bbox_image_px"][3] - item["bbox_image_px"][1] >= height * 0.5 and box[0] - 8 <= (item["bbox_image_px"][0] + item["bbox_image_px"][2]) / 2 <= box[2] + 8]
    horizontal = [item for item in drawings if item["bbox_image_px"][3] - item["bbox_image_px"][1] <= 5 and item["bbox_image_px"][2] - item["bbox_image_px"][0] >= width * 0.30 and box[1] - 8 <= (item["bbox_image_px"][1] + item["bbox_image_px"][3]) / 2 <= box[3] + 8]
    # the table's own edges close the grid, so a picture in the last row still maps to a cell
    xs = _merge_positions([(item["bbox_image_px"][0] + item["bbox_image_px"][2]) / 2 for item in vertical] + [box[0], box[2]])
    ys = _merge_positions([(item["bbox_image_px"][1] + item["bbox_image_px"][3]) / 2 for item in horizontal] + [box[1], box[3]])
    cx, cy = (image_box[0] + image_box[2]) / 2, (image_box[1] + image_box[3]) / 2
    col = next((index for index in range(len(xs) - 1) if xs[index] <= cx <= xs[index + 1]), None)
    row = next((index for index in range(len(ys) - 1) if ys[index] <= cy <= ys[index + 1]), None)
    if col is None or row is None:
        return None
    return row, col


def _drop_textual_table_images(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any]) -> None:
    """A logo image inside a table whose cell text already transcribes it is redundant: keep the table text, drop the image block."""
    pdf = page["evidence"]["pdf"]
    drawings = [item for item in pdf if item.get("kind") == "pdf_drawing"]
    by_ref = {item["evidence_id"]: item for item in pdf if item.get("kind") == "pdf_image"}
    removed: set[str] = set()
    for block in [item for item in blocks if item.get("type") == "image" and len(item.get("source_refs", [])) == 1]:
        evidence = by_ref.get(block["source_refs"][0])
        if evidence is None:
            continue
        image_box = evidence.get("effective_bbox_image_px", evidence["bbox_image_px"])
        tables = [item for item in blocks if item.get("type") == "table" and _table_owns_picture(item["bbox_image_px"], image_box)]
        if len(tables) != 1:
            continue
        table = tables[0]
        html_value_current = str(table.get("html", ""))
        position = _table_cell_for_image(table, drawings, image_box)
        cell = _html_cell_at(html_value_current, position[0], position[1]) if position else None
        target: tuple[int, int] | None = None
        if cell is not None and not _cell_visible_text(cell):
            target = position
        else:
            # the mapped cell is missing or already carries text: place the picture in the nearest
            # empty cell before giving up on the token
            target = _nearest_empty_cell(html_value_current, position[0] if position else 0, position[1] if position else 0)
        html_value = None
        if target:
            target_cell = _html_cell_at(html_value_current, target[0], target[1])
            has_token = any(str(getattr(node, "tag", "")).lower() == "img" for node in target_cell.iter()) if target_cell is not None else False
            html_value = (
                _append_image_token(html_value_current, block["source_refs"][0], target[0], target[1])
                if has_token
                else _embed_image_token(html_value_current, block["source_refs"][0], target[0], target[1], 1, 1)
            )
        fields = reviews.setdefault("blocks", {}).setdefault(table["block_id"], {})
        table["source_refs"] = list(dict.fromkeys(table.get("source_refs", []) + [block["source_refs"][0]]))
        table["support_refs"] = list(dict.fromkeys(table.get("support_refs", []) + block.get("support_refs", [])))
        if html_value is not None and target is not None:
            table["html"] = html_value
            table.setdefault("embedded_images", []).append({
                "image_ref": block["source_refs"][0], "row": target[0], "col": target[1], "rowspan": 1, "colspan": 1,
                "bbox_image_px": image_box, "bbox_pdf_pt": evidence.get("bbox_pdf_pt"), "status": "PASS",
                "method": "pdf_grid_model_table_image_binding_nearest_empty_cell",
                "evidence_refs": [block["source_refs"][0]],
            })
            fields["embedded_images"] = {
                "status": "PASS", "method": "pdf_grid_model_table_image_binding_nearest_empty_cell", "evidence_refs": [block["source_refs"][0]],
            }
        else:
            # the cell text already transcribes the picture: keep the text, retire the picture block
            fields["image_text_transcribed_in_cell"] = {
                "status": "PASS", "method": "table_cell_text_covers_picture", "evidence_refs": [block["source_refs"][0]],
            }
        by_ref[block["source_refs"][0]]["required_coverage"] = False
        removed.add(block["block_id"])
    if removed:
        blocks[:] = [item for item in blocks if item["block_id"] not in removed]
        for block_id in removed:
            reviews.get("blocks", {}).pop(block_id, None)
        for order, item in enumerate(blocks):
            item["reading_order"] = order


def _promote_textual_content_images(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], nested_by_image: dict[str, list[str]], all_models: list[dict[str, Any]], pdf_drawings: list[dict[str, Any]]) -> None:
    """A content image whose nested model records are all text-like becomes one text block."""
    text_kinds = {"header", "footer", "page_number", "text", "title", "header_image", "footer_image"}
    model_index = {m["evidence_id"]: m for m in all_models}
    for image in [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_image"]:
        image_ref = image["evidence_id"]
        image_box = image.get("effective_bbox_image_px", image["bbox_image_px"])
        nested = [model_index[ref] for ref in nested_by_image.get(image_ref, []) if ref in model_index]
        if len({str(model["evidence_id"]).split(".")[0] for model in nested if str(model.get("text", "")).strip()}) < 2:
            loose_kinds = text_kinds | {"doc_title", "paragraph_title", "figure_title"}
            known = {model["evidence_id"] for model in nested}
            nested = nested + [
                model for model in all_models
                if model["evidence_id"] not in known
                and model.get("source_type") in loose_kinds
                and str(model.get("text", "")).strip()
                and _ioa(model["bbox_image_px"], image_box) >= 0.7
            ]
        if not nested:
            continue
        allowed_kinds = text_kinds | {"doc_title", "paragraph_title", "figure_title"}
        if any(m.get("source_type") not in allowed_kinds for m in nested):
            continue
        if any("[non-text]" in str(m.get("text", "")).lower() for m in nested):
            continue
        box = image_box
        ruled = [
            d for d in pdf_drawings
            if _intersection(d["bbox_image_px"], box) / max(1.0, _box_area(d["bbox_image_px"])) >= 0.5
            and ((d["bbox_image_px"][2] - d["bbox_image_px"][0] >= (box[2] - box[0]) * 0.5 and d["bbox_image_px"][3] - d["bbox_image_px"][1] <= 5)
                 or (d["bbox_image_px"][3] - d["bbox_image_px"][1] >= (box[3] - box[1]) * 0.5 and d["bbox_image_px"][2] - d["bbox_image_px"][0] <= 5))
        ]
        if len(ruled) >= 2:
            continue
        image_block = next((block for block in blocks if block.get("source_refs") == [image_ref]), None)
        if image_block is None or image_block.get("content_role"):
            continue
        votes = {}
        for m in nested:
            key = m.get("source_type") if m.get("source_type") in {"header", "footer", "page_number"} else ("title" if m.get("type_candidate") == "title" else "text")
            votes[key] = votes.get(key, 0) + 1
        chosen = max(votes.items(), key=lambda kv: kv[1])[0]
        anchored_blocks = [
            block for block in blocks
            if block.get("source_refs") and str(block.get("text", "")).strip()
        ]
        anchored_norms = {_norm(str(block.get("text", ""))) for block in anchored_blocks}

        def _already_carried(value: str) -> bool:
            """True when the surrounding text layer already spells out this value.

            A logo or letterhead picture whose wording is already present in the PDF text layer adds
            no information: promoting it would create a text block that cannot be verified.
            """
            squeezed = re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()
            if len(squeezed) < 3:
                return False
            for anchor in anchored_blocks:
                box = anchor["bbox_image_px"]
                if min(box[3], image_box[3]) <= max(box[1], image_box[1]):
                    continue
                anchor_text = re.sub(r"[\W_]+", "", str(anchor.get("text", "")), flags=re.UNICODE).lower()
                if squeezed in anchor_text:
                    return True
            return False
        entries: list[dict[str, Any]] = []
        for m in nested:
            prefix = str(m["evidence_id"]).split(".")[0]
            for line in str(m.get("text", "")).splitlines():
                value = line.strip()
                if not value or _norm(value) in anchored_norms or _already_carried(value):
                    continue
                norm = _norm(value)
                entry = next((item for item in entries if item["norm"] == norm), None)
                if entry is None:
                    entry = next((item for item in entries if _text_similarity(item["norm"], norm) >= 0.85), None)
                if entry is None:
                    entries.append({"value": value, "norm": norm, "prefixes": {prefix}})
                else:
                    entry["prefixes"].add(prefix)
                    if len(value) > len(entry["value"]):
                        entry["value"], entry["norm"] = value, norm
        seen_texts = [entry["value"] for entry in entries]
        text_value = "\n".join(seen_texts)
        if not text_value:
            continue
        seen_norms = {_norm(line) for line in seen_texts}
        absorbed = [
            block for block in blocks
            if block is not image_block
            and not block.get("source_refs")
            and block.get("type") in {"text", "title", "header", "footer", "page_number"}
            and str(block.get("text", "")).strip()
            and _ioa(block["bbox_image_px"], image_box) >= 0.5
            and any(
                _norm(str(block.get("text", ""))) == norm
                or norm in _norm(str(block.get("text", "")))
                or _norm(str(block.get("text", ""))) in norm
                for norm in seen_norms
            )
        ]
        if absorbed:
            for block in absorbed:
                for line in str(block.get("text", "")).splitlines():
                    value = line.strip()
                    if value and not _already_carried(value) and all(_norm(value) != _norm(prev) for prev in seen_texts):
                        seen_texts.append(value)
            text_value = "\n".join(seen_texts)
        if not (entries and all(len(entry["prefixes"]) >= 2 for entry in entries)):
            # the models do not agree on the wording inside this picture: keep the picture instead of
            # claiming top-level text that no evidence can verify.
            continue
        union_box = list(image_box)
        refs = list(dict.fromkeys([image_ref] + [m["evidence_id"] for m in nested]))
        image_block["type"] = "text" if chosen in {"text", "title"} else chosen
        if chosen == "title":
            image_block["level"] = 1
            image_block.setdefault("issues", []).append("title_level_review")
        image_block["text"] = text_value
        image_block["bbox_image_px"] = union_box
        image_block["source_refs"] = [image_ref]
        image_block["support_refs"] = [ref for ref in refs if ref != image_ref]
        image_block["type_candidates"] = ["text", "title"] if chosen in {"text", "title"} else [chosen]
        image_block["issues"] = [issue for issue in image_block.get("issues", []) if issue != "model_missed_pdf_image"]
        if absorbed:
            image_block["support_refs"] = list(dict.fromkeys(
                image_block.get("support_refs", []) + [ref for block in absorbed for ref in block.get("support_refs", [])]
            ))
            absorbed_ids = {block["block_id"] for block in absorbed}
            blocks[:] = [block for block in blocks if block["block_id"] not in absorbed_ids]
            for block_id in absorbed_ids:
                reviews.get("blocks", {}).pop(block_id, None)
            for order, block in enumerate(blocks):
                block["reading_order"] = order
        add_coordinate_views(image_block, page)
        refs = list(dict.fromkeys(refs + image_block.get("support_refs", [])))
        status = "PASS"
        method = "textual_content_image_promoted_cross_model"
        reviews["blocks"][image_block["block_id"]] = {
            "type": {"status": status, "method": method, "evidence_refs": refs},
            "bbox": {"status": status, "method": method, "evidence_refs": refs},
            "text": {"status": status, "method": method, "evidence_refs": refs},
            **({"level": {"status": "CHECK", "method": method, "evidence_refs": refs}} if chosen == "title" else {}),
        }


def _demote_image_description_text_blocks(
    page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], all_models: list[dict[str, Any]], pdf_lines: list[dict[str, Any]], pdf_images: list[dict[str, Any]]
) -> None:
    """Long text seen by one model only, inside a graphic region that the other model reports as an empty image: the region is an image."""
    model_index = {m["evidence_id"]: m for m in all_models}
    for block in blocks:
        if block.get("type") not in {"text", "header", "footer", "page_number", "title"}:
            continue
        text = re.sub(r"\s+", "", str(block.get("text", "")))
        if len(text) < 40:
            continue
        support = [model_index[ref] for ref in block.get("support_refs", []) if ref in model_index]
        readable = [m for m in support if str(m.get("text", "")).strip()]
        if len(readable) != 1:
            continue
        others = [m for m in support if m not in readable]
        if not others or not all(str(m.get("source_type", "")).endswith("image") for m in others):
            continue
        box = block["bbox_image_px"]
        match = next(
            (
                image for image in pdf_images
                if _ioa(box, image.get("effective_bbox_image_px", image["bbox_image_px"])) >= 0.9
                and not any(
                    str(line.get("text", "")).strip()
                    and _intersection(line["bbox_image_px"], image["bbox_image_px"]) / max(1.0, _box_area(line["bbox_image_px"])) >= 0.5
                    for line in pdf_lines
                )
            ),
            None,
        )
        if match is None:
            continue
        image_ref = match["evidence_id"]
        block["type"] = "image"
        block["source_refs"] = [image_ref]
        block["bbox_image_px"] = list(match.get("effective_bbox_image_px", match["bbox_image_px"]))
        add_coordinate_views(block, page)
        block.pop("text", None)
        block.pop("level", None)
        fields = reviews.setdefault("blocks", {}).setdefault(block["block_id"], {})
        fields.pop("text", None)
        fields.pop("level", None)
        fields["type"] = {"status": "PASS", "method": "image_description_text_demoted", "evidence_refs": [image_ref] + block.get("support_refs", [])}
        fields["bbox"] = {"status": "PASS", "method": "pdf_image_object", "evidence_refs": [image_ref]}


def _attach_logo_glyphs_to_image(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], pdf_lines: list[dict[str, Any]], pdf_images: list[dict[str, Any]]) -> None:
    """Short logo glyph runs (e.g. brand marks) fully inside a pdf image with no pdf lines anchor them: merge them onto that image."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for block in blocks:
        if block.get("source_refs") or block.get("type") not in {"header", "footer", "text"}:
            continue
        text = re.sub(r"\s+", "", str(block.get("text", "")))
        if not text or len(text) > 4:
            continue
        box = block["bbox_image_px"]
        match = next(
            (
                image for image in pdf_images
                if not any(
                    str(line.get("text", "")).strip()
                    and _intersection(line["bbox_image_px"], image["bbox_image_px"]) / max(1.0, _box_area(line["bbox_image_px"])) >= 0.5
                    for line in pdf_lines
                )
                and _intersection(box, image["bbox_image_px"]) / max(1.0, _box_area(box)) >= 0.6
            ),
            None,
        )
        if match is not None:
            grouped.setdefault(match["evidence_id"], []).append(block)
    removed: set[str] = set()
    for image_ref, group in grouped.items():
        if any(image_ref in block.get("source_refs", []) for block in blocks):
            continue
        image = next((item for item in pdf_images if item["evidence_id"] == image_ref), None)
        if image is None:
            continue
        carrier, *extras = group
        carrier["text"] = "\n".join(str(block.get("text", "")).strip() for block in group if str(block.get("text", "")).strip())
        carrier["bbox_image_px"] = list(image.get("effective_bbox_image_px", image["bbox_image_px"]))
        carrier["source_refs"] = [image_ref]
        carrier["support_refs"] = list(dict.fromkeys([ref for block in group for ref in block.get("support_refs", [])]))
        add_coordinate_views(carrier, page)
        fields = reviews.setdefault("blocks", {}).setdefault(carrier["block_id"], {})
        fields["type"] = {"status": "PASS", "method": "logo_glyph_image_bind", "evidence_refs": [image_ref] + carrier["support_refs"]}
        fields["bbox"] = {"status": "PASS", "method": "pdf_image_object", "evidence_refs": [image_ref]}
        fields["text"] = {"status": "PASS", "method": "logo_glyph_image_bind", "evidence_refs": [image_ref] + carrier["support_refs"]}
        for extra in extras:
            removed.add(extra["block_id"])
    if removed:
        blocks[:] = [block for block in blocks if block["block_id"] not in removed]
        for block_id in removed:
            reviews.get("blocks", {}).pop(block_id, None)
        for order, block in enumerate(blocks):
            block["reading_order"] = order


def _restore_letterhead_image_blocks(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], pdf_lines: list[dict[str, Any]]) -> None:
    """An unanchored [Non-Text] header/footer covered by a same-box pdf image is a graphic letterhead: restore it as image."""
    images = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_image"]
    for block in blocks:
        if block.get("type") not in {"header", "footer"} or block.get("source_refs"):
            continue
        if "[Non-Text]" not in str(block.get("text", "")):
            continue
        box = block["bbox_image_px"]
        match = next(
            (
                image for image in images
                if _iou(image.get("effective_bbox_image_px", image["bbox_image_px"]), box) >= 0.85
                and not any(
                    str(line.get("text", "")).strip()
                    and _intersection(line["bbox_image_px"], image.get("effective_bbox_image_px", image["bbox_image_px"])) / max(1.0, _box_area(line["bbox_image_px"])) >= 0.5
                    for line in pdf_lines
                )
            ),
            None,
        )
        if match is None:
            continue
        image_ref = match["evidence_id"]
        block["type"] = "image"
        block["source_refs"] = [image_ref]
        block.pop("text", None)
        fields = reviews.setdefault("blocks", {}).setdefault(block["block_id"], {})
        fields["type"] = {"status": "PASS", "method": "letterhead_pdf_image_restore", "evidence_refs": [image_ref] + block.get("support_refs", [])}
        fields["bbox"] = {"status": "PASS", "method": "pdf_image_object", "evidence_refs": [image_ref]}
        fields.pop("text", None)


def _drop_textual_pdf_image_blocks(
    page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], pdf_lines: list[dict[str, Any]]
) -> None:
    """Remove top-level image blocks that are textual pictures or background text layers."""
    images = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_image"]
    text_kinds = {"text", "title", "header", "footer", "page_number", "unknown"}
    for image in images:
        image_ref = image["evidence_id"]
        if image.get("embedded_into_table"):
            continue
        image_box = image.get("effective_bbox_image_px", image["bbox_image_px"])
        if not any(block.get("source_refs") == [image_ref] for block in blocks):
            continue
        if any(block.get("content_role") for block in blocks if block.get("source_refs") == [image_ref]):
            continue
        overlapping = [
            block for block in blocks
            if block.get("type") in text_kinds and _ioa(block["bbox_image_px"], image_box) >= 0.55
        ]
        if not overlapping:
            continue
        lines_inside = [
            line for line in pdf_lines
            if str(line.get("text", "")).strip() and _intersection(line["bbox_image_px"], image_box) / max(1.0, _box_area(line["bbox_image_px"])) >= 0.5
        ]
        unanchored = all(not block.get("source_refs") for block in overlapping)
        graphic_marks = any("[non-text]" in str(block.get("text", "")).lower() for block in overlapping)
        textual_image = (
            not graphic_marks
            and 1 <= len(overlapping) <= 2
            and unanchored
            and not lines_inside
            and (
                _iou(overlapping[0]["bbox_image_px"], image_box) >= 0.6
                or _intersection(overlapping[0]["bbox_image_px"], image_box) / max(1.0, _box_area(overlapping[0]["bbox_image_px"])) >= 0.6
            )
        )
        background_layer = len(lines_inside) >= 6 and len(overlapping) >= 3 and not _region_graphic_structure(page, image_box)
        if not (textual_image or background_layer):
            continue
        anchor = overlapping[0]
        dropped = [block for block in blocks if block.get("source_refs") == [image_ref]]
        if not dropped:
            continue
        for block in dropped:
            anchor["support_refs"] = list(dict.fromkeys(anchor.get("support_refs", []) + block.get("support_refs", [])))
            anchor["type_candidates"] = sorted(set(anchor.get("type_candidates", [])) | set(block.get("type_candidates", [])))
        blocks[:] = [block for block in blocks if block.get("source_refs") != [image_ref]]
        image["required_coverage"] = False
        for block in dropped:
            reviews.get("blocks", {}).pop(block["block_id"], None)
        fields = reviews["blocks"].setdefault(anchor["block_id"], {})
        fields["image_replaced_by_text"] = {"status": "PASS", "method": "textual_pdf_image_replaced_by_model_text", "evidence_refs": [image_ref] + anchor.get("support_refs", [])}


def _attach_word_tables(page: dict[str, Any], blocks: list[dict[str, Any]], reviews: dict[str, Any], tables: list[dict[str, Any]], binding_score: float | None) -> None:
    """Bind native Word tables to PDF table blocks, per table rather than per document.

    ``word_table_ref`` / ``word_table_shape`` / ``word_row_range`` are attached on table-local
    evidence only, so a document-level score below ``WORD_BINDING_STRONG`` still keeps the exact
    row coverage used by the cross-page contract. Replacing the model HTML with the native rows
    stays gated on the document score, because that changes what the sample reports as answer.
    """
    if not tables:
        return
    strong = binding_score is not None and binding_score >= WORD_BINDING_STRONG
    coverage_min = WORD_TABLE_COVERAGE_MIN if strong else WORD_COVERAGE_MIN_WEAK
    row_min = WORD_ROW_MATCH_MIN_STRONG if strong else WORD_ROW_MATCH_MIN_WEAK
    evidence = {item["evidence_id"]: item for item in page["evidence"]["pdf"]}
    for block in blocks:
        if block.get("type") != "table":
            continue
        fragment = normalize_word_text("".join(str(evidence.get(ref, {}).get("text", "")) for ref in block.get("source_refs", [])))
        if not fragment:
            continue
        scored = []
        for table in tables:
            matcher = difflib.SequenceMatcher(None, table["normalized_text"], fragment)
            covered = sum(match.size for match in matcher.get_matching_blocks()) / max(1, len(fragment))
            scored.append((covered, table))
        score, table = max(scored, key=lambda item: item[0])
        if score < coverage_min:
            continue
        block["word_table_ref"] = table["evidence_id"]
        block["word_table_match_score"] = round(score, 4)
        block["word_table_shape"] = [table["rows"], table["cols"]]
        block["word_table_binding_scope"] = "document" if strong else "table_fragment"
        row_match = match_table_rows(table, fragment, minimum_score=row_min)
        if row_match:
            block["word_row_range"] = [row_match["row_start"], row_match["row_end"] - 1]
            block["word_row_match_score"] = row_match["score"]
        block["support_refs"] = list(dict.fromkeys(block.get("support_refs", []) + [table["evidence_id"]]))
        # A native Word table cannot express a picture embedded inside a PDF table cell. Replacing the
        # HTML would drop the data-image-ref markers the embedded image binding relies on, so those
        # tables keep the model HTML while still contributing exact row coverage.
        if strong and row_match and not block.get("embedded_images"):
            previous_html = block.get("html")
            block["html"] = table_fragment_html(table, row_match["row_start"], row_match["row_end"])
            # Keep the model HTML plus its review so a later failed fragment check can revert the
            # replacement: native rows are only an answer when they themselves verified.
            block["_word_html_backup"] = {"html": previous_html, "review": reviews["blocks"][block["block_id"]].get("html")}
            reviews["blocks"][block["block_id"]]["html"] = {
                "status": "PASS",
                "method": "word_native_rows_pdf_fragment_binding",
                "evidence_refs": [table["evidence_id"]] + block.get("source_refs", []),
                "document_binding_score": round(binding_score, 4),
                "row_match_score": row_match["score"],
            }


def build_page(*, sample_id: str, full_id: str, short_id: str, page_number: int, source_pdf: Path, mineru_root: Path, paddle_root: Path, ovis_root: Path, anydoc_markdown: str = "", source_docx: Path | None = None, word_binding_score: float | None = None, word_tables: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    image_path = paddle_root / "images" / short_id / f"page_{page_number:04d}.png"
    paddle_path = paddle_root / "json" / short_id / f"page_{page_number:04d}_res.json"
    ovis_path = ovis_root / "markdown" / short_id / f"page_{page_number:04d}.md"
    pdf_path = source_pdf
    mineru_path = mineru_root / full_id / "extracted" / full_id / "vlm" / f"{full_id}_content_list.json"
    for path in (image_path, paddle_path, ovis_path, pdf_path, mineru_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    with Image.open(image_path) as image:
        image_width, image_height = image.size
    pdf_page, pdf_lines, pdf_images, pdf_drawings = _pdf_facts(pdf_path, page_number, image_width, image_height)
    page: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sample_id": sample_id,
        "page": page_number,
        "page_image": {"path": str(image_path), "sha256": _sha256(image_path), "width": image_width, "height": image_height},
        "pdf_path": str(pdf_path),
        "pdf_page": pdf_page,
        "evidence": {"pdf": [], "mineru": [], "paddle": [], "ovis": [], "word": [], "anydoc": [], "tools": []},
        "current_candidate": {"blocks": []},
        "field_reviews": {},
        "detected_conflicts": [],
        "document_context": {"confirmed_titles": [], "candidate_titles": []},
    }
    for index, line in enumerate(pdf_lines):
        line["evidence_id"] = f"pdf.line.p{page_number}.{index:04d}"
    # A broken font map yields line text that is pure glyph noise. Such lines carry no verifiable
    # text: blanking them keeps the model's reading authoritative and keeps them out of required
    # coverage, so they can neither overwrite block text nor spawn phantom blocks. When most of a
    # page's text layer is noise the whole layer is untrustworthy - the few printable survivors
    # ("#", "%", "$") are fragments of the same broken encoding, not text.
    unusable = [line for line in pdf_lines if _text_is_unusable(line.get("text"))]
    whole_layer_broken = len(unusable) >= max(3, len(pdf_lines) // 2)
    targets = pdf_lines if whole_layer_broken else unusable
    for line in targets:
        line["raw_text"] = line.get("text")
        line["text"] = ""
        line["text_usable"] = False
        line["required_coverage"] = False
    for line in pdf_lines:
        if line.get("text_usable") is False:
            continue
        cleaned = _sanitize_text(line.get("text"))
        if cleaned != line.get("text"):
            line["text"] = cleaned
        line["text_usable"] = True
    if targets:
        page["pdf_text_layer"] = {
            "unusable_lines": len(targets),
            "total_lines": len(pdf_lines),
            "verdict": "glyph_noise" if whole_layer_broken else "partial",
        }
    _mark_visually_absent_lines(page, pdf_lines)
    for index, image in enumerate(pdf_images):
        image["evidence_id"] = f"pdf.image.p{page_number}.{index:04d}"
    for index, drawing in enumerate(pdf_drawings):
        drawing["evidence_id"] = f"pdf.drawing.p{page_number}.{index:04d}"
    mineru = _expand_mineru_lists(_load_mineru(mineru_path, page_number, image_width, image_height), pdf_lines)
    paddle = _load_paddle(paddle_path, page_number, image_width, image_height)
    page["evidence"]["pdf"] = pdf_lines + pdf_images + pdf_drawings
    page["evidence"]["mineru"] = mineru
    page["evidence"]["paddle"] = paddle
    page["evidence"]["ovis"] = [{"evidence_id": f"ovis.page.p{page_number}.0000", "kind": "page_markdown", "text": ovis_path.read_text(encoding="utf-8"), "required_coverage": False}]
    if anydoc_markdown:
        page["evidence"]["anydoc"] = [{"evidence_id": "anydoc.document.0000", "kind": "document_outline", "text": _anydoc_outline(anydoc_markdown), "required_coverage": False}]
    if source_docx:
        if word_tables is None:
            word_tables = read_word_tables(source_docx)
        page["evidence"]["word"] = word_tables
    else:
        word_tables = []
    blocks, reviews, issues = _build_candidates(page, mineru + paddle, pdf_lines, pdf_images)
    _confirm_cross_model_table_html(page, blocks, reviews)
    _confirm_ruled_table_cell_matrix(page, blocks, reviews)
    _confirm_simple_ruled_table_html(page, blocks, reviews)
    _bind_table_embedded_images(page, blocks, reviews)
    _drop_textual_table_images(page, blocks, reviews)
    _absorb_nested_pdf_images(page, blocks, reviews)
    for merged_id in sorted(_merge_duplicate_image_blocks(blocks, reviews)):
        issues.append({"issue_id": f"issue.p{page_number}.merged.{merged_id}", "type": "duplicate_image_block_merged", "block_ids": [merged_id]})
    _drop_vector_container_images(page, blocks, reviews, pdf_lines)
    _drop_textual_pdf_image_blocks(page, blocks, reviews, pdf_lines)
    _restore_letterhead_image_blocks(page, blocks, reviews, pdf_lines)
    _attach_logo_glyphs_to_image(page, blocks, reviews, pdf_lines, [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_image"])
    _demote_image_description_text_blocks(page, blocks, reviews, mineru + paddle, pdf_lines, [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_image"])
    for block in blocks:
        _repair_trailing_empty_table_column(page, block)
        if block.get("type") != "title" and "level" in block:
            block.pop("level", None)
            reviews["blocks"].get(block["block_id"], {}).pop("level", None)
    _attach_word_tables(page, blocks, reviews, word_tables, word_binding_score)
    _drop_placeholder_blocks(blocks, reviews, issues)
    _merge_decorative_marker_blocks(blocks, reviews, page)
    _drop_invisible_text_blocks(page, blocks, reviews, issues, pdf_lines)
    _drop_nonprintable_text_blocks(blocks, reviews, issues)
    _drop_glyph_noise_text_blocks(blocks, reviews, issues)
    _enforce_single_image_owner(page, blocks, issues)
    _enforce_evidence_containment(page, blocks, reviews)
    # Evidence containment expands boxes to cover their evidence, which can create new overlaps, so
    # the consolidation runs once more on the final geometry - and the reading order last of all.
    # The body font size is the page's own reference point: "bigger than the body" is what makes a
    # heading a heading, and it has to be measured on the page, not assumed.
    typography = page.get("typography") or {}
    text_sizes = sorted(
        float(entry["font_size"])
        for block in blocks
        if block.get("type") in {"text", "title", "formula"} and (entry := typography.get(block["block_id"])) and entry.get("font_size")
    )
    page["typography_summary"] = {
        "body_font_size": round(text_sizes[len(text_sizes) // 2], 2) if text_sizes else None,
        "max_font_size": round(max(text_sizes), 2) if text_sizes else None,
        "blocks": len(typography),
    }
    blocks, issues, _ = _consolidate_nested_text_blocks(page, blocks, reviews, issues)
    # Inventory rules: the annotations showed that almost every human correction is about *which blocks
    # exist* (fragmented figures, reference lists cut into pieces), so the page ends with the
    # deterministic inventory stage before the order is finalised.
    blocks = apply_inventory_rules(page, blocks, issues, rules=("figures", "paragraphs"))
    _apply_reading_order(page, blocks, issues, mineru + paddle)
    page["current_candidate"] = {"blocks": blocks}
    page["field_reviews"] = reviews

    def _informational_issue(value: Any) -> bool:
        """Block issues are either a plain kind string or a dict carrying details."""
        kind = value.get("kind") if isinstance(value, dict) else value
        return kind in {"model_missed_pdf_text", "model_missed_pdf_image"}

    page["detected_conflicts"] = issues + [
        {"issue_id": f"issue.p{page_number}.{len(issues) + index:04d}", "type": issue, "block_ids": [block["block_id"]]}
        for index, block in enumerate(blocks)
        for issue in block.get("issues", [])
        if not _informational_issue(issue)
    ]
    page["document_context"]["candidate_titles"] = [
        {"block_id": block["block_id"], "text": block.get("text", ""), "level": block.get("level"), "page": page_number}
        for block in blocks if block["type"] == "title" or "title" in block.get("type_candidates", [])
    ]
    return page


def _ruled_regions(page: dict[str, Any]) -> list[dict[str, Any]]:
    width, height = page["page_image"]["width"], page["page_image"]["height"]
    drawings = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_drawing"]
    horizontal = [item for item in drawings if item["bbox_image_px"][2] - item["bbox_image_px"][0] >= width * 0.20 and item["bbox_image_px"][3] - item["bbox_image_px"][1] <= 5]
    vertical = [item for item in drawings if item["bbox_image_px"][3] - item["bbox_image_px"][1] >= height * 0.08 and item["bbox_image_px"][2] - item["bbox_image_px"][0] <= 5]
    regions = []
    tolerance = max(8.0, width * 0.01)
    for top in horizontal:
        for bottom in horizontal:
            y1 = (top["bbox_image_px"][1] + top["bbox_image_px"][3]) / 2
            y2 = (bottom["bbox_image_px"][1] + bottom["bbox_image_px"][3]) / 2
            if y2 - y1 < height * 0.08:
                continue
            for left in vertical:
                for right in vertical:
                    x1 = (left["bbox_image_px"][0] + left["bbox_image_px"][2]) / 2
                    x2 = (right["bbox_image_px"][0] + right["bbox_image_px"][2]) / 2
                    if x2 - x1 < width * 0.20:
                        continue
                    lb, rb, tb, bb = left["bbox_image_px"], right["bbox_image_px"], top["bbox_image_px"], bottom["bbox_image_px"]
                    if not (lb[1] <= y1 + tolerance and lb[3] >= y2 - tolerance and rb[1] <= y1 + tolerance and rb[3] >= y2 - tolerance and tb[0] <= x1 + tolerance and tb[2] >= x2 - tolerance and bb[0] <= x1 + tolerance and bb[2] >= x2 - tolerance):
                        continue
                    regions.append({"bbox_image_px": [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)], "evidence_refs": [top["evidence_id"], bottom["evidence_id"], left["evidence_id"], right["evidence_id"]]})
    regions.sort(key=lambda item: (-(item["bbox_image_px"][2] - item["bbox_image_px"][0]) * (item["bbox_image_px"][3] - item["bbox_image_px"][1]), item["bbox_image_px"][1]))
    unique = []
    for region in regions:
        if not any(_iou(region["bbox_image_px"], item["bbox_image_px"]) >= 0.98 for item in unique):
            unique.append(region)
    return unique


def _add_word_continuation_fragments(pages: list[dict[str, Any]], binding_score: float | None) -> None:
    """Rebuild the following fragment of a table that continues onto the next page.

    Admission is table-local: the fragment must match the same native table's rows exactly where the
    previous page stopped (``match_table_rows`` >= 0.95 and ``row_start == cursor``). The document
    level score is only recorded, never used to refuse the evidence.
    """
    if not any(block.get("word_row_range") for page in pages for block in page["current_candidate"]["blocks"]):
        return
    cursors: dict[str, int] = {}
    last_pages: dict[str, int] = {}
    for page in sorted(pages, key=lambda item: item["page"]):
        blocks = page["current_candidate"]["blocks"]
        tables = {item["evidence_id"]: item for item in page["evidence"].get("word", [])}
        for block in blocks:
            ref, row_range = block.get("word_table_ref"), block.get("word_row_range")
            if ref and row_range:
                cursors[ref] = max(cursors.get(ref, 0), row_range[1] + 1)
                last_pages[ref] = page["page"]
        for region in _ruled_regions(page):
            if any(_iou(region["bbox_image_px"], block["bbox_image_px"]) >= 0.20 for block in blocks if block.get("type") == "table"):
                continue
            lines = [item for item in page["evidence"]["pdf"] if item.get("kind") == "pdf_line" and _ioa(item["bbox_image_px"], region["bbox_image_px"]) >= 0.80]
            if not lines:
                continue
            fragment_text = "".join(str(line.get("text", "")) for line in lines)
            matches = []
            for ref, cursor in cursors.items():
                table = tables.get(ref)
                if table is None or cursor >= table["rows"] or last_pages.get(ref) != page["page"] - 1:
                    continue
                match = match_table_rows(table, fragment_text, minimum_score=0.95)
                if match and match["row_start"] == cursor:
                    matches.append((match["score"], ref, table, match))
            if len(matches) != 1:
                continue
            score, ref, table, match = matches[0]
            absorbed = [block for block in blocks if block.get("type") not in {"header", "footer", "page_number", "image", "chart", "seal"} and _ioa(block["bbox_image_px"], region["bbox_image_px"]) >= 0.85]
            line_refs = {line["evidence_id"] for line in lines}
            if len(absorbed) < 2 or any(set(block.get("source_refs", [])) - line_refs for block in absorbed):
                continue
            if any(block.get("embedded_images") for block in absorbed):
                # the native rows cannot carry the pictures that bound into this fragment
                continue
            old_ids = {block["block_id"] for block in absorbed}
            source_refs = list(dict.fromkeys(ref_id for block in absorbed for ref_id in block.get("source_refs", [])))
            support_refs = list(dict.fromkeys(ref_id for block in absorbed for ref_id in block.get("support_refs", [])))
            block_id = f"candidate.block.p{page['page']}.word_table_continuation"
            replacement = {
                "block_id": block_id,
                "type": "table",
                "bbox_image_px": region["bbox_image_px"],
                "reading_order": min(block["reading_order"] for block in absorbed),
                "source_refs": source_refs,
                "support_refs": support_refs + [ref] + region["evidence_refs"],
                "type_candidates": ["table"],
                "issues": [],
                "html": table_fragment_html(table, match["row_start"], match["row_end"]),
                "word_table_ref": ref,
                "word_table_shape": [table["rows"], table["cols"]],
                "word_row_range": [match["row_start"], match["row_end"] - 1],
                "word_row_match_score": score,
            }
            add_coordinate_views(replacement, page)
            blocks[:] = [block for block in blocks if block["block_id"] not in old_ids] + [replacement]
            reviews = page["field_reviews"]["blocks"]
            for old_id in old_ids:
                reviews.pop(old_id, None)
            refs = [ref] + region["evidence_refs"] + source_refs
            reviews[block_id] = {
                "type": {"status": "PASS", "method": "word_native_pdf_ruled_continuation", "evidence_refs": refs},
                "bbox": {"status": "PASS", "method": "pdf_ruled_region", "evidence_refs": region["evidence_refs"]},
                "html": {"status": "PASS", "method": "word_native_rows_pdf_fragment_binding", "evidence_refs": refs, "document_binding_score": round(binding_score, 4) if binding_score is not None else None, "binding_scope": "table_fragment", "row_match_score": score},
            }
            page["detected_conflicts"] = [issue for issue in page.get("detected_conflicts", []) if not old_ids.intersection(issue.get("block_ids", []))]
            blocks.sort(key=lambda block: (block["bbox_image_px"][1], block["bbox_image_px"][0]))
            for order, block in enumerate(blocks):
                block["reading_order"] = order
            cursors[ref] = match["row_end"]
            last_pages[ref] = page["page"]


def _withdraw_unverified_word_rows(pages: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop row coverage from tables whose fragments did not line up with the native table.

    The cross-page contract treats ``word_row_range`` as exact, so it must not survive a coverage
    check that says the rows are gapped, overlapping or incomplete. A fragment that disagrees with
    its neighbours is withdrawn on its own, while a table-level failure (no fragment at all, rows
    never covered) withdraws the whole table. Withdrawn evidence is recorded, never silently ignored.
    """
    table_offenders = {str(issue.get("word_table_ref")) for issue in issues if issue.get("word_table_ref") and not issue.get("block_id")}
    block_offenders = {str(issue.get("block_id")) for issue in issues if issue.get("block_id")}
    if not table_offenders and not block_offenders:
        return []
    withdrawn: list[dict[str, Any]] = []
    for page in pages:
        reviews_by_block = (page.get("field_reviews") or {}).get("blocks") or {}
        for block in page["current_candidate"]["blocks"]:
            reference = str(block.get("word_table_ref") or "")
            if reference not in table_offenders and str(block["block_id"]) not in block_offenders:
                continue
            if not block.get("word_row_range"):
                continue
            withdrawn.append({"page": page["page"], "block_id": block["block_id"], "word_table_ref": reference,
                              "word_row_range": block.get("word_row_range"), "word_table_row_evidence": "withdrawn"})
            block.pop("word_row_range", None)
            block.pop("word_row_match_score", None)
            block["word_table_row_evidence"] = "withdrawn"
            entry = reviews_by_block.get(block["block_id"])
            if isinstance(entry, dict):
                backup = block.pop("_word_html_backup", None)
                if isinstance(backup, dict):
                    # a replaced HTML is only valid together with the row coverage it came from
                    if backup.get("html") is None:
                        block.pop("html", None)
                    else:
                        block["html"] = backup["html"]
                    original = backup.get("review")
                    if isinstance(original, dict):
                        entry["html"] = original
                    else:
                        entry.pop("html", None)
                elif (entry.get("html") or {}).get("method") == "word_native_rows_pdf_fragment_binding":
                    # Word-native continuation: there is no model HTML to fall back to, so the
                    # replacement is downgraded to CHECK instead of being reported as verified.
                    entry["html"] = {"status": "CHECK", "method": "word_table_fragment_mismatch", "evidence_refs": []}
    return withdrawn


def _validate_word_fragment_coverage(pages: list[dict[str, Any]], *, require_complete: bool) -> list[dict[str, Any]]:
    if not pages or not pages[0]["evidence"].get("word"):
        return []
    tables = {item["evidence_id"]: item for item in pages[0]["evidence"]["word"]}
    fragments: dict[str, list[tuple[int, int, int, str]]] = {}
    for page in pages:
        for block in page["current_candidate"]["blocks"]:
            ref, row_range = block.get("word_table_ref"), block.get("word_row_range")
            if ref and isinstance(row_range, list) and len(row_range) == 2:
                fragments.setdefault(ref, []).append((page["page"], int(row_range[0]), int(row_range[1]), block["block_id"]))
    issues = []
    for ref, table in tables.items():
        items = sorted(fragments.get(ref, []))
        if not items:
            if require_complete:
                issues.append({"word_table_ref": ref, "issue": "word_table_has_no_pdf_fragment"})
            continue
        expected = 0 if require_complete else items[0][1]
        for page_number, start, end, block_id in items:
            if start != expected or end < start or end >= table["rows"]:
                issues.append({"word_table_ref": ref, "block_id": block_id, "page": page_number, "row_range": [start, end], "expected_start": expected, "issue": "word_fragment_row_gap_or_overlap"})
            expected = max(expected, end + 1)
        if require_complete and expected != table["rows"]:
            issues.append({"word_table_ref": ref, "covered_rows": expected, "expected_rows": table["rows"], "issue": "word_table_rows_incomplete"})
    return issues


def _heading_sequence_key(text: str) -> tuple[str, str] | None:
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = re.sub(r"[*_~`]+", "", value)
    match = re.match(r"^(?:#+\s*)?第\s*([一二三四五六七八九十百零〇两0-9]+)\s*章(?:\s|$)", value)
    if match:
        return "zh_chapter", match.group(1)
    match = re.match(r"^(?:#+\s*)?chapter\s+(\d+)(?:\s|$|[:.])", value, re.IGNORECASE)
    if match:
        return "en_chapter", match.group(1)
    match = re.match(r"^(?:#+\s*)?(?:步骤|step)\s*(\d+)(?:\s|$|[:：.、])", value, re.IGNORECASE)
    if match:
        return "numbered_step", match.group(1)
    return None


def _split_heading_sequence_items(items: list[tuple[dict[str, Any], dict[str, Any], str]]) -> list[list[tuple[dict[str, Any], dict[str, Any], str]]]:
    groups: list[list[tuple[dict[str, Any], dict[str, Any], str]]] = []
    current: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    previous = -1
    for item in items:
        try:
            number = int(item[2])
        except (TypeError, ValueError):
            number = previous + 1
        if current and number <= previous:
            groups.append(current)
            current = []
        current.append(item)
        previous = number
    if current:
        groups.append(current)
    return groups


def _promote_document_heading_sequences(pages: list[dict[str, Any]]) -> None:
    sequences: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = {}
    for page in pages:
        for block in sorted(page["current_candidate"]["blocks"], key=lambda item: item.get("reading_order", 0)):
            key = _heading_sequence_key(str(block.get("text", "")))
            if key:
                sequences.setdefault(key[0], []).append((page, block, key[1]))
    for style, items in sequences.items():
        for group_index, group in enumerate(_split_heading_sequence_items(items), start=1):
            if len({token for _, _, token in group}) < 2:
                continue
            context = [{"page": page["page"], "block_id": block["block_id"], "text": str(block.get("text", ""))[:160], "current_type": block.get("type")} for page, block, _ in group]
            for page, block, _ in group:
                block["document_heading_sequence_evidence"] = {"style": style, "local_group": group_index, "items": context}
                if block.get("type") == "title":
                    continue
                block["type"] = "unknown"
                block["type_candidates"] = sorted(set(block.get("type_candidates", [])) | {"text", "title"})
                block.setdefault("issues", []).append("document_heading_sequence_review")
                fields = page["field_reviews"]["blocks"][block["block_id"]]
                fields["type"] = {"status": "CHECK", "method": "document_heading_sequence_candidate", "evidence_refs": block.get("source_refs", []) + block.get("support_refs", [])}
                fields["level"] = {"status": "CHECK", "method": "document_heading_sequence_candidate", "evidence_refs": block.get("source_refs", []) + block.get("support_refs", [])}


def _heading_peer_shape(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = re.sub(r"[*_~`]+", "", value).strip()
    if re.match(r"^(?:步骤|step)\s*\d+", value, re.IGNORECASE):
        return "step"
    if re.match(r"^第\s*[一二三四五六七八九十百零〇两0-9]+\s*章", value):
        return "chapter"
    if re.match(r"^\d+(?:\.\d+)+[.、]?", value):
        return "decimal"
    if re.match(r"^[\u2022\u25cf\u25aa\uf06c\-]", value):
        return "bullet"
    return "plain"


def _promote_geometric_heading_peers(pages: list[dict[str, Any]]) -> None:
    page_seeds = [
        [block for block in page["current_candidate"]["blocks"] if block.get("type") == "title" or "title" in block.get("type_candidates", [])]
        for page in pages
    ]
    for page_index, page in enumerate(pages):
        blocks = sorted(page["current_candidate"]["blocks"], key=lambda block: block.get("reading_order", 0))
        width = float(page["page_image"]["width"])
        height = float(page["page_image"]["height"])
        seeds = [
            block
            for nearby_index in range(max(0, page_index - 2), min(len(pages), page_index + 3))
            for block in page_seeds[nearby_index]
        ]
        for index, block in enumerate(blocks[:-1]):
            if block.get("type") != "text" or "title" in block.get("type_candidates", []):
                continue
            text = _duplicate_norm(str(block.get("text", "")))
            box = block["bbox_image_px"]
            line_height = box[3] - box[1]
            if not 2 <= len(text) <= 40 or "\n" in str(block.get("text", "")) or line_height > height * 0.035 or box[2] - box[0] > width * 0.55:
                continue
            following = blocks[index + 1]
            following_text = _duplicate_norm(str(following.get("text", "")))
            gap = following["bbox_image_px"][1] - box[3]
            if following.get("type") != "text" or len(following_text) < max(16, len(text)) or not 0 <= gap <= max(18.0, line_height * 1.8):
                continue
            peers = []
            target_shape = _heading_peer_shape(str(block.get("text", "")))
            for seed in seeds:
                if _heading_peer_shape(str(seed.get("text", ""))) != target_shape:
                    continue
                seed_box = seed["bbox_image_px"]
                seed_height = seed_box[3] - seed_box[1]
                if abs(seed_box[0] - box[0]) <= width * 0.012 and abs(seed_height - line_height) <= max(3.0, line_height * 0.20):
                    peers.append(seed)
            if len(peers) < 2:
                continue
            block["type"] = "unknown"
            block["type_candidates"] = sorted(set(block.get("type_candidates", [])) | {"text", "title"})
            block["geometric_heading_peer_evidence"] = {
                "peer_block_ids": [peer["block_id"] for peer in peers[:8]],
                "following_block_id": following["block_id"],
                "same_left_edge_tolerance": round(width * 0.012, 3),
                "peer_shape": target_shape,
            }
            block.setdefault("issues", []).append("geometric_heading_peer_review")
            fields = page["field_reviews"]["blocks"][block["block_id"]]
            refs = block.get("source_refs", []) + block.get("support_refs", [])
            fields["type"] = {"status": "CHECK", "method": "geometric_heading_peer_candidate", "evidence_refs": refs}
            fields["level"] = {"status": "CHECK", "method": "geometric_heading_peer_candidate", "evidence_refs": refs}


def _confirm_stable_repeated_marginal_text(pages: list[dict[str, Any]]) -> None:
    occurrences: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for page in pages:
        width = float(page["page_image"]["width"])
        height = float(page["page_image"]["height"])
        for block in page["current_candidate"]["blocks"]:
            if block.get("type") not in {"text", "title", "unknown", "header", "footer"}:
                continue
            normalized = _duplicate_norm(str(block.get("text", "")))
            if not normalized or len(normalized) > 120:
                continue
            box = block["bbox_image_px"]
            role = "header" if box[1] <= height * 0.12 else "footer" if box[3] >= height * 0.88 else None
            if role:
                occurrences.setdefault((role, normalized), []).append((page, block))
    threshold = min(3, len(pages))
    for (role, _), items in occurrences.items():
        if len(items) < threshold:
            continue
        for page, block in items:
            width = float(page["page_image"]["width"])
            height = float(page["page_image"]["height"])
            box = block["bbox_image_px"]
            stable = [
                (other_page, other)
                for other_page, other in items
                if abs(other["bbox_image_px"][0] / other_page["page_image"]["width"] - box[0] / width) <= 0.015
                and abs(other["bbox_image_px"][1] / other_page["page_image"]["height"] - box[1] / height) <= 0.015
                and abs((other["bbox_image_px"][2] - other["bbox_image_px"][0]) / other_page["page_image"]["width"] - (box[2] - box[0]) / width) <= 0.03
            ]
            if len(stable) < threshold:
                continue
            block["type"] = role
            block["type_candidates"] = [role]
            block.pop("level", None)
            block.pop("parent_id", None)
            block["issues"] = [issue for issue in block.get("issues", []) if issue not in {"type_conflict", "title_level_review", "document_heading_sequence_review", "marginal_role_review"}]
            refs = list(dict.fromkeys(block.get("source_refs", []) + block.get("support_refs", [])))
            block["cross_page_role_evidence"] = {"candidate_role": role, "repeat_count": len(stable), "document_page_count": len(pages), "at_expected_edge": True, "stable_geometry": True}
            fields = page["field_reviews"]["blocks"][block["block_id"]]
            fields["type"] = {"status": "PASS", "method": "cross_page_repeated_marginal_text_geometry", "evidence_refs": refs}
            fields.pop("level", None)


def _table_first_row_signature(html_value: str) -> str:
    match = re.search(r"<tr[^>]*>(.*?)</tr>", html_value or "", flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    return _duplicate_norm(re.sub(r"<[^>]+>", "", match.group(1)))


def _confirm_repeated_page_header_tables(pages: list[dict[str, Any]]) -> None:
    """A table repeating at the same page edge with the same first row is page furniture, not a table continuation."""
    occurrences: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for page in pages:
        # idempotent: the pass can be re-run with new thresholds without keeping stale marks
        for block in page["current_candidate"]["blocks"]:
            block.pop("repeated_page_header", None)
    for page in pages:
        height = float(page["page_image"]["height"])
        for block in page["current_candidate"]["blocks"]:
            if block.get("type") != "table":
                continue
            box = block.get("bbox_image_px")
            if not isinstance(box, list) or len(box) != 4:
                continue
            if box[1] > height * 0.30 and box[3] < height * 0.70:
                continue
            # Page furniture is small: a table that fills a third of the page is a continuation
            # fragment of a long table (its first row repeats as a Word header row, which is not
            # page furniture). Only short edge tables can repeat as page headers.
            if (box[3] - box[1]) > height * 0.20:
                continue
            signature = _table_first_row_signature(str(block.get("html", "")))
            if not signature or len(signature) > 160:
                continue
            occurrences.setdefault(signature, []).append((page, block))
    threshold = min(3, len(pages))
    clusters: list[list[str]] = []
    for signature in occurrences:
        for cluster in clusters:
            if _text_similarity(cluster[0], signature) >= 0.85:
                cluster.append(signature)
                break
        else:
            clusters.append([signature])
    for cluster in clusters:
        items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()
        for signature in cluster:
            for page, block in occurrences[signature]:
                if block["block_id"] in seen:
                    continue
                seen.add(block["block_id"])
                items.append((page, block))
        if len({int(page["page"]) for page, _ in items}) < threshold:
            continue
        for page, block in items:
            width = float(page["page_image"]["width"])
            height = float(page["page_image"]["height"])
            box = block["bbox_image_px"]
            stable = [
                other
                for other_page, other in items
                if abs(other["bbox_image_px"][0] / other_page["page_image"]["width"] - box[0] / width) <= 0.02
                and abs(other["bbox_image_px"][1] / other_page["page_image"]["height"] - box[1] / height) <= 0.02
                and abs((other["bbox_image_px"][2] - other["bbox_image_px"][0]) / other_page["page_image"]["width"] - (box[2] - box[0]) / width) <= 0.03
            ]
            if len(stable) < threshold:
                continue
            block["repeated_page_header"] = True
            block["cross_page_role_evidence"] = {
                "candidate_role": "repeated_page_table",
                "repeat_count": len(stable),
                "document_page_count": len(pages),
                "at_expected_edge": True,
                "stable_geometry": True,
            }


def _review_nonrepeated_marginal_roles(pages: list[dict[str, Any]]) -> None:
    occurrences: dict[tuple[str, str], int] = {}
    for page in pages:
        for block in page["current_candidate"]["blocks"]:
            role = block.get("type")
            if role not in {"header", "footer"}:
                continue
            key = (role, _duplicate_norm(str(block.get("text", ""))))
            if key[1]:
                occurrences[key] = occurrences.get(key, 0) + 1

    document_page_count = len(pages)
    for page in pages:
        height = float(page["page_image"]["height"])
        reviews = page["field_reviews"]["blocks"]
        for block in page["current_candidate"]["blocks"]:
            role = block.get("type")
            if role not in {"header", "footer"}:
                continue
            normalized = _duplicate_norm(str(block.get("text", "")))
            repeat_count = occurrences.get((role, normalized), 0)
            box = block["bbox_image_px"]
            at_expected_edge = box[1] <= height * 0.12 if role == "header" else box[3] >= height * 0.88
            block["cross_page_role_evidence"] = {
                "candidate_role": role,
                "repeat_count": repeat_count,
                "document_page_count": document_page_count,
                "at_expected_edge": at_expected_edge,
            }
            if repeat_count >= 2 and at_expected_edge:
                reviews[block["block_id"]]["type"] = {
                    "status": "PASS",
                    "method": "cross_page_repeated_marginal_role",
                    "evidence_refs": block.get("support_refs", []),
                }
                continue
            alternatives = {role, "text"}
            alternatives.add("title" if role == "header" else "footnote")
            block["type_candidates"] = sorted(alternatives)
            if "marginal_role_review" not in block["issues"]:
                block["issues"].append("marginal_role_review")
            reviews[block["block_id"]]["type"] = {
                "status": "CHECK",
                "method": "nonrepeated_marginal_role",
                "evidence_refs": block.get("support_refs", []),
            }


def build_document_evidence(
    output_root: Path,
    *,
    sample_id: str,
    full_id: str,
    short_id: str,
    source_pdf: Path,
    mineru_root: Path,
    paddle_root: Path,
    ovis_root: Path,
    pages: tuple[int, ...] | None = None,
    anydoc_markdown: str = "",
    source_docx: Path | None = None,
) -> list[Path]:
    pdf_path = source_pdf
    word_binding_score = None
    word_binding_status = "no_source_docx"
    word_tables: list[dict[str, Any]] = []
    if source_docx:
        try:
            import pymupdf
            with pymupdf.open(pdf_path) as document:
                pdf_text = normalize_word_text("".join(page.get_text() for page in document))
            word_text = normalize_word_text(read_word_text(source_docx))
            word_binding_score = difflib.SequenceMatcher(None, word_text, pdf_text).ratio()
            word_tables = read_word_tables(source_docx)
        except Exception as exc:  # noqa: BLE001 - a corrupt DOCX degrades the build to PDF-only
            word_binding_score = None
            word_tables = []
            word_binding_status = f"docx_unreadable:{type(exc).__name__}"
            print(f"Word evidence unavailable ({type(exc).__name__}): {source_docx.name}", file=sys.stderr)
        # A weak document score no longer discards the native tables: the tables keep supplying exact
        # row coverage per table (see _attach_word_tables). Only the model-HTML replacement stays
        # gated on the document level score. A DOCX that could not be read keeps its own status so the
        # manifest shows why no Word evidence is present.
        if word_binding_status == "no_source_docx":
            if not word_tables:
                word_binding_status = "no_tables_in_docx"
            elif word_binding_score >= WORD_BINDING_STRONG:
                word_binding_status = "strong"
            else:
                word_binding_status = "weak"
    if pages is None:
        import pymupdf
        with pymupdf.open(pdf_path) as document:
            pages = tuple(range(1, len(document) + 1))
    outputs = []
    page_objects = []
    build_rows = []
    build_started = time.monotonic()
    for page_number in pages:
        page_started = time.monotonic()
        page = build_page(sample_id=sample_id, full_id=full_id, short_id=short_id, page_number=page_number, source_pdf=source_pdf, mineru_root=mineru_root, paddle_root=paddle_root, ovis_root=ovis_root, anydoc_markdown=anydoc_markdown, source_docx=source_docx, word_binding_score=word_binding_score, word_tables=word_tables)
        target = output_root / "evidence" / sample_id / f"page_{page_number:04d}.json"
        write_json_atomic(target, page)
        outputs.append(target)
        page_objects.append(page)
        build_rows.append({"page": page_number, "wall_seconds": round(time.monotonic() - page_started, 3), "blocks": len(page["current_candidate"]["blocks"])})
    _add_word_continuation_fragments(page_objects, word_binding_score)
    _confirm_stable_repeated_marginal_text(page_objects)
    _confirm_repeated_page_header_tables(page_objects)
    _absorb_blocks_inside_graphic_images(page_objects)
    _promote_document_heading_sequences(page_objects)
    _promote_geometric_heading_peers(page_objects)
    _review_nonrepeated_marginal_roles(page_objects)
    document_complete = list(pages) == list(range(1, max(pages) + 1))
    word_fragment_issues = _validate_word_fragment_coverage(page_objects, require_complete=document_complete)
    # Inconsistent row coverage is withdrawn per table instead of aborting the whole document build
    # (an aborted build used to fall back to PDF-only and lose every native table in the document).
    withdrawn_word_rows = _withdraw_unverified_word_rows(page_objects, word_fragment_issues)
    for index, (page, target) in enumerate(zip(page_objects, outputs)):
        page["document_context"]["candidate_titles"] = [
            {"block_id": block["block_id"], "text": block.get("text", ""), "level": block.get("level"), "page": page["page"]}
            for block in page["current_candidate"]["blocks"] if block["type"] == "title" or "title" in block.get("type_candidates", [])
        ]
        build_rows[index]["blocks"] = len(page["current_candidate"]["blocks"])
        for block in page["current_candidate"]["blocks"]:
            block.pop("_word_html_backup", None)
        write_json_atomic(target, page)
    build_timing = {
        "wall_seconds": round(time.monotonic() - build_started, 3),
        "average_page_seconds": round(sum(row["wall_seconds"] for row in build_rows) / len(build_rows), 3) if build_rows else 0.0,
        "pages": build_rows,
    }
    write_json_atomic(output_root / "build_summary.json", build_timing)
    manifest = {"schema_version": SCHEMA_VERSION, "sample_id": sample_id, "full_id": full_id, "short_id": short_id, "source_pdf": str(source_pdf), "source_pdf_sha256": _sha256(source_pdf), "source_docx": str(source_docx) if source_docx else None, "source_docx_sha256": _sha256(source_docx) if source_docx else None, "word_pdf_text_binding_score": round(word_binding_score, 4) if word_binding_score is not None else None, "word_binding": {"score": round(word_binding_score, 4) if word_binding_score is not None else None, "status": word_binding_status, "policy": {"strong_binding": WORD_BINDING_STRONG, "coverage_min": WORD_TABLE_COVERAGE_MIN, "coverage_min_weak": WORD_COVERAGE_MIN_WEAK, "row_match_min": WORD_ROW_MATCH_MIN_STRONG, "row_match_min_weak": WORD_ROW_MATCH_MIN_WEAK}, "native_tables": len(word_tables), "rows_withdrawn": len(withdrawn_word_rows)}, "evidence_roots": {"mineru": str(mineru_root), "paddle": str(paddle_root), "ovis": str(ovis_root)}, "anydoc_available": bool(anydoc_markdown), "pages": list(pages), "page_count": len(pages), "document_complete": document_complete, "word_fragment_issues": word_fragment_issues, "word_rows_withdrawn": withdrawn_word_rows, "evidence_files": [str(path) for path in outputs], "build_timing": build_timing}
    write_json_atomic(output_root / "document_manifest.json", manifest)
    write_json_atomic(output_root / "pilot_manifest.json", manifest)
    write_json_atomic(output_root / "dataset_manifest.json", {
        "schema_version": "ocr-agent-dataset-manifest.v1",
        "source_line": "annotation",
        "dataset": output_root.name,
        "version": "v1",
        "source_asset": str(source_pdf),
        "source_sha256": manifest["source_pdf_sha256"],
        "gt_kind": "OCR-Agent document revision with MinerU/Paddle/Ovis/PDF evidence",
        "sample_count": 1,
        "page_count": len(pages),
        "status": "built",
        "physical_at": str(output_root),
        "evidence_roots": manifest["evidence_roots"],
        "anydoc_available": bool(anydoc_markdown),
    })
    return outputs
