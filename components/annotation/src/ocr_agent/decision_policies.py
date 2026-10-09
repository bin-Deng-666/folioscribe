from __future__ import annotations

from typing import Any

GLOBAL_FIELD_RULES = {
    "type": {
        "method": "Compare MinerU and Paddle labels, Ovis/AnyDoc structure, PDF geometry and document context. Agreement is supporting evidence, not proof.",
        "review_when": "The sources conflict and context cannot distinguish the alternatives.",
    },
    "bbox": {
        "method": "Start from one MinerU or Paddle block. Match its content to PDF spans. Text-like boxes are snapped to the tight union of assigned PDF spans; media/table boxes use PDF image or drawing geometry when available.",
        "forbidden": "Do not average model boxes, union unrelated model boxes, or write freehand coordinates.",
    },
    "text": {
        "method": "Use exact visible PDF text when reliable. Use OCR evidence only for image text or a damaged PDF text layer.",
        "review_when": "Model text cannot be grounded to PDF spans or a visual OCR result.",
    },
    "reading_order": {
        "method": "Use page geometry for single-column pages. Use a verified column structure for multi-column pages. Header, footer and page_number are outside body order.",
        "review_when": "Column assignment or object placement is ambiguous without visual inspection.",
    },
}

TYPE_RULES = {
    "text": {
        "positive": ["ordinary prose", "abstract", "reference entry", "code or algorithm block", "aside text", "numbered or bulleted list item", "procedural instruction", "table-of-contents entry"],
        "negative": ["heading that names and governs following content", "caption bound to a target", "header/footer", "table cell content outside a table block"],
        "required_fields": ["text"],
        "hint": "A line starting with a CJK enumerated marker (1、 2、 一、) or a contract clause marker (第一条/第二条/第 3.1 条) is a list item or clause line (text), not a title, unless its measured font ratio is >= 1.15 - body-size enumerated lines are list items even when they look like headings (annotators kept 14 of 16 enumerated and 15 of 15 clause lines as text).",
    },
    "title": {
        "positive": ["document or section heading that names and governs following content", "source-model title vote supported by local context", "heading numbering supported by document context", "parallel heading supported by its own surrounding prose"],
        "negative": ["table-of-contents entry", "repeated page header", "table-internal heading", "numbered operation that carries the instruction itself", "numbering without governing context", "literal Markdown # marker without independent governing context"],
        "required_fields": ["text", "level"],
        "level": "Levels express nesting within this document and are limited to L1-L4. If an independent document title exists, it is level 1 and top-level sections are level 2; their subsections are level 3. Level 4 is the deepest accepted subdivision. If there is no independent document title, top-level sections start at level 1. Numbering depth and parallel sequences are evidence for relative nesting, not a reason to flatten deeper leaves into L4 titles.",
    },
    "table": {
        "positive": ["MinerU/Paddle table region", "PDF ruled grid with uniquely assigned spans"],
        "negative": ["aligned prose without a table structure", "form decoration without cell semantics"],
        "required_fields": ["html"],
        "html": "Prefer a strictly verified PDF grid. Otherwise use a table reconstruction tool over MinerU/Paddle candidates. HTML must preserve rowspan and colspan.",
    },
    "table_caption": {
        "positive": ["caption-like text adjacent to a table", "prefix such as 表 or Table when context supports it"],
        "negative": ["table note", "unit label", "chapter heading", "caption with no unique table"],
        "required_fields": ["text", "parent_id"],
        "parent": "parent_id must identify exactly one nearby table.",
    },
    "image": {
        "positive": ["MinerU/Paddle image", "PDF image object"],
        "negative": ["chart", "seal", "header/footer decoration when page role is primary"],
        "required_fields": [],
    },
    "image_caption": {
        "positive": ["caption-like text adjacent to an image or chart", "prefix such as 图, Figure or Fig. when context supports it"],
        "negative": ["table caption", "chapter heading", "caption with no unique media target"],
        "required_fields": ["text", "parent_id"],
        "parent": "parent_id must identify exactly one nearby image or chart. Paddle figure_title is only a candidate.",
    },
    "chart": {
        "positive": ["MinerU/Paddle chart", "visible axes, legend or plotted data reported by a visual tool"],
        "negative": ["ordinary screenshot", "table rendered as an image"],
        "required_fields": [],
    },
    "seal": {
        "positive": ["Paddle seal plus PDF image/visual evidence"],
        "negative": ["logo", "circular chart", "decorative watermark"],
        "required_fields": [],
    },
    "formula": {
        "positive": ["MinerU equation", "Paddle display_formula", "independent displayed mathematical expression"],
        "negative": ["formula number", "short inline expression that belongs inside prose"],
        "required_fields": ["text"],
    },
    "header": {
        "positive": ["MinerU/Paddle header", "repeated content at the top page edge", "Paddle header_image"],
        "negative": ["cover-page title", "body heading near the top of a page"],
        "required_fields": ["text"],
    },
    "footer": {
        "positive": ["MinerU/Paddle footer", "repeated content at the bottom page edge", "Paddle footer_image"],
        "negative": ["footnote", "body paragraph continuing near page bottom", "page number alone", "Markdown thematic separator or horizontal rule"],
        "required_fields": ["text"],
    },
    "page_number": {
        "positive": ["MinerU page_number", "Paddle number at a page edge", "Arabic/Roman/page-pattern text"],
        "negative": ["list number", "table cell number", "formula number"],
        "required_fields": ["text"],
    },
    "footnote": {
        "positive": ["MinerU page_footnote", "Paddle footnote/vision_footnote", "small page-bottom note tied to body content"],
        "negative": ["footer", "page number", "table note"],
        "required_fields": ["text"],
    },
}


def policy_context(page: dict[str, Any]) -> dict[str, Any]:
    types = {"text", "title"}
    for block in page.get("current_candidate", {}).get("blocks", []):
        if block.get("type") in TYPE_RULES:
            types.add(block["type"])
        for candidate in block.get("type_candidates", []):
            if candidate in TYPE_RULES:
                types.add(candidate)
    # Figure titles require both caption policies even before either type is chosen.
    if any(
        item.get("source_type") == "figure_title" or item.get("raw_type") == "figure_title"
        for items in page.get("evidence", {}).values()
        for item in items
    ):
        types.update({"table_caption", "image_caption", "table", "image", "chart"})
    return {
        "global_fields": GLOBAL_FIELD_RULES,
        "type_rules": {name: TYPE_RULES[name] for name in sorted(types)},
    }
