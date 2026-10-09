"""Block-inventory rules: the pipeline's own answer to the errors human annotators keep fixing.

The annotation study showed where the machine disagrees with a careful human, and the disagreement is
almost never "which character is this" - it is the inventory itself: paragraphs cut into pieces, one
figure split into several blocks, captions left as plain text, headings invented out of body text. The
rules here are deterministic, evidence-based and cheap; they run at the end of a page build (and can be
replayed on already-built pages, which is how they are scored against the human labels).

Every rule follows the same contract as the rest of the builder: merge or retype, keep every evidence
reference on the survivor, and record an issue so a reviewer can see what the machine decided.
"""
from __future__ import annotations

import collections
import re
from typing import Any

TEXT_TYPES = {"text", "title", "caption", "formula", "image_caption", "table_caption", "reference", "footnote"}
FURNITURE = {"header", "footer", "page_number"}
FIGURE_TYPES = {"image", "chart", "seal"}
NUMBERED_TITLE = re.compile(r"^\s*(\d+(\.\d+)*|[IVXLC]+|[一二三四五六七八九十]+)[\.、\s]")
# The separator is a space *or* a line break: the readers emit "2.\nJ. R. Greer..." and requiring a
# space silently disabled the reference-list merge, leaving one block per entry (the annotators merged
# up to 23 of them by hand on exactly these pages).
NUMBERED_REFERENCE = re.compile(r"^\s*\[?\d{1,3}[\.\)\]](\s|$)")


def _box(block: dict[str, Any]) -> list[float]:
    box = block.get("bbox_image_px") or [0, 0, 0, 0]
    return [float(box[0]), float(box[1]), float(box[2]), float(box[3])]


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _overlap(a: list[float], b: list[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _ioa(inner: list[float], outer: list[float]) -> float:
    return _overlap(inner, outer) / max(1.0, _area(inner))


def _reference_number(text: str) -> int | None:
    """The leading number of a reference entry, when the text starts with one."""
    match = re.match(r"^\s*\[?(\d{1,3})[\.\)\]](\s|$)", str(text or ""))
    return int(match.group(1)) if match else None


def _looks_like_table_row(text: str) -> bool:
    """Two or more wide gaps (3+ spaces or a tab) inside one line usually means a table row."""
    for line in str(text or "").splitlines():
        if line.count("\t") >= 1 or len([chunk for chunk in line.split("  ") if chunk.strip()]) >= 3:
            return True
    return False


def _text_of(block: dict[str, Any]) -> str:
    return str(block.get("text") or "").strip()


def _page_size(page: dict[str, Any]) -> tuple[float, float]:
    image = page.get("page_image") or {}
    return float(image.get("width") or 0), float(image.get("height") or 0)


def _merge_into(page: dict[str, Any], survivor: dict[str, Any], loser: dict[str, Any], issues: list[dict[str, Any]], reason: str) -> None:
    survivor["source_refs"] = list(dict.fromkeys(list(survivor.get("source_refs") or []) + list(loser.get("source_refs") or [])))
    survivor["support_refs"] = list(dict.fromkeys(list(survivor.get("support_refs") or []) + list(loser.get("support_refs") or [])))
    survivor["type_candidates"] = sorted(set(survivor.get("type_candidates") or []) | set(loser.get("type_candidates") or []))
    left, right = _text_of(survivor), _text_of(loser)
    if right and right not in left:
        survivor["text"] = f"{left}\n{right}".strip() if left else right
    box = _box(survivor)
    other = _box(loser)
    survivor["bbox_image_px"] = [min(box[0], other[0]), min(box[1], other[1]), max(box[2], other[2]), max(box[3], other[3])]
    try:  # keep the derived views consistent with the new box
        from .evidence_builder import add_coordinate_views

        add_coordinate_views(survivor, {"page_image": page.get("page_image"), "pdf_page": page.get("pdf_page")})
    except Exception:
        pass
    issues.append({
        "issue_id": f"issue.{survivor.get('block_id')}.{reason}",
        "type": reason,
        "block_ids": [str(survivor.get("block_id")), str(loser.get("block_id"))],
        "note": reason,
    })


WATERMARK = re.compile(r"(Downloaded from|https?://www\.|^\s*下载|仅供|内部资料|^\(?c\)?\s*20\d\d)", re.I)


def _text_is_placeholder(text: str) -> bool:
    """Watermarks, download stamps and repeated-character filler are not content blocks."""
    value = str(text or "").strip()
    if not value:
        return False
    if WATERMARK.search(value) and len(value) < 200:
        return True
    compact = "".join(value.split())
    if len(compact) >= 6:
        counts = collections.Counter(compact)
        char, count = counts.most_common(1)[0]
        if count / len(compact) >= 0.6 and char in "△▲○●□■◇◆*·-—_=+<>":
            return True
    return False



def absorb_figure_internals(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Micro-blocks inside a figure (axis ticks, legends, in-chart labels) belong to the figure.

    Parsers emit the raster AND its internal labels as separate blocks; the annotators keep the
    chart as one block. The leaked labels then wreck the page twice: as spurious blocks, and as
    order noise that breaks grid detection on chart-cell pages. A full-width figure is exempt - a
    page-wide band is background, and absorbing everything under it would eat the page.
    """
    width, height = _page_size(page)
    figures = [
        block for block in blocks
        if str(block.get("type")) in FIGURE_TYPES and _area(_box(block)) > 0
        and (_box(block)[2] - _box(block)[0]) < width * 0.9
        and _area(_box(block)) <= 0.7 * width * height
    ]
    kept: list[dict[str, Any]] = []
    for block in blocks:
        if str(block.get("type")) in FIGURE_TYPES:
            kept.append(block)
            continue
        box = _box(block)
        host = next(
            (
                fig for fig in figures
                if fig is not block
                and _area(box) <= 0.25 * _area(_box(fig))
                and _ioa(box, _box(fig)) >= 0.85
            ),
            None,
        )
        if host is not None:
            issues.append({
                "issue_id": f"issue.{block.get('block_id')}.figure_internal",
                "type": "figure_internal_absorbed",
                "block_ids": [str(block.get("block_id")), str(host.get("block_id"))],
                "note": str(str(block.get("text"))[:24] or "graphic"),
            })
            continue
        kept.append(block)
    return kept





def relabel_margin_logos(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A graphic in the running-head band whose source votes carry text is the brand line.

    Parsers emit the corner logo both as a picture and as recognised text ('ITG 国贸期货'); the
    merged block kept the image label and lost the text, and the annotators call it a header.
    """
    width, height = _page_size(page)
    if not height:
        return blocks
    evidence = page.get("evidence") or {}
    for block in blocks:
        if str(block.get("type")) != "image":
            continue
        box = _box(block)
        top_band = box[1] <= height * 0.08
        bottom_band = box[3] >= height * 0.9
        if not (top_band or bottom_band):
            continue
        texts = []
        for ref in list(block.get("source_refs") or []) + list(block.get("support_refs") or []):
            for items in evidence.values():
                for item in items:
                    if isinstance(item, dict) and item.get("evidence_id") == ref and str(item.get("text") or "").strip():
                        texts.append(str(item.get("text")).strip())
        w, h = max(1.0, box[2] - box[0]), max(1.0, box[3] - box[1])
        if texts and top_band and w / h >= 2.0:
            # Only a wide running-head strip with recognised text becomes a header; a compact
            # corner mark (a stamp, a seal) stays an image even though it carries a few glyphs.
            block["type"] = "header"
            block["text"] = block.get("text") or "\n".join(dict.fromkeys(texts))
            issues.append({
                "issue_id": f"issue.{block.get('block_id')}.margin_logo",
                "type": "margin_logo_relabelled",
                "block_ids": [str(block.get("block_id"))],
                "note": str(block["type"]),
            })
    return blocks




def merge_overlapping_line_fragments(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Several blocks that share a line and overlap horizontally are one line read several times.

    Paddle and MinerU each emit the author line, and the merge kept both copies side by side: five
    blocks with x-intervals that overlap 30-90%, y within 6px. The annotators keep one block per
    line; the texts concatenate in reading order and the box is the union.
    """
    width, height = _page_size(page)
    if not width:
        return blocks
    texts = [b for b in blocks if str(b.get("type")) == "text" and _text_of(b)]
    if len(texts) < 2:
        return blocks
    groups: list[list[dict[str, Any]]] = []
    remaining = sorted(texts, key=lambda b: (_box(b)[1], _box(b)[0]))
    used: set[int] = set()
    for i, first in enumerate(remaining):
        if i in used:
            continue
        fb = _box(first)
        group = [first]
        used.add(i)
        for j in range(i + 1, len(remaining)):
            if j in used:
                continue
            other = remaining[j]
            ob = _box(other)
            same_line = abs(ob[1] - fb[1]) <= 6.0 and abs(ob[3] - fb[3]) <= 10.0
            x_overlap = min(fb[2], ob[2]) - max(fb[0], ob[0])
            if same_line and x_overlap >= 0.3 * min(fb[2] - fb[0], ob[2] - ob[0]):
                group.append(other)
                used.add(j)
        if len(group) >= 2:
            groups.append(group)
    if not groups:
        return blocks
    dropped: set[str] = set()
    for group in groups:
        group.sort(key=lambda b: _box(b)[0])
        survivor = group[0]
        for loser in group[1:]:
            dropped.add(str(loser.get("block_id")))
            left, right = _text_of(survivor), _text_of(loser)
            if right and right not in left:
                survivor["text"] = f"{left} {right}".strip()
            sb, lb = _box(survivor), _box(loser)
            survivor["bbox_image_px"] = [min(sb[0], lb[0]), min(sb[1], lb[1]), max(sb[2], lb[2]), max(sb[3], lb[3])]
            issues.append({
                "issue_id": f"issue.{loser.get('block_id')}.line_fragment",
                "type": "line_fragment_merged",
                "block_ids": [str(loser.get("block_id")), str(survivor.get("block_id"))],
                "note": "",
            })
    return [b for b in blocks if str(b.get("block_id")) not in dropped]




def split_form_field_blocks(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A multi-line block whose every line is a form field is N fields, not one paragraph.

    Parsers emit "合同编号：____\n签订地点：____\n签订日期：__" as one block; the annotators keep
    one block per field. Splitting needs a line height: lines stack by the block's own metrics.
    """
    out: list[dict[str, Any]] = []
    for block in blocks:
        text = str(block.get("text") or "")
        lines = [ln for ln in text.split("\n") if ln.strip()]
        box = _box(block)
        h = max(1.0, box[3] - box[1])
        def _is_field_line(ln: str) -> bool:
            value = ln.strip()
            if "___" in value or value.endswith(("：", ":")):
                return True
            # signature rows: "甲方：（盖章）", "法定代表人（委托代理人）：（签名）"
            return "：" in value and value.endswith("）") and len(value) <= 24

        if (
            str(block.get("type")) in {"text", "unknown"}
            and len(lines) >= 2
            and all(_is_field_line(ln) for ln in lines)
        ):
            line_h = h / len(lines)
            pdf_box = block.get("bbox_pdf_pt")
            pdf_h = (pdf_box[3] - pdf_box[1]) if isinstance(pdf_box, list) and len(pdf_box) == 4 else None
            norm_box = block.get("bbox_normalized")
            for index, ln in enumerate(lines):
                y1 = box[1] + index * line_h
                child = {
                    "block_id": f"{block.get('block_id')}.f{index}" if index else str(block.get("block_id")),
                    "type": "text",
                    "bbox_image_px": [box[0], y1, box[2], y1 + line_h],
                    "text": ln,
                    "source_refs": list(block.get("source_refs") or []),
                    "support_refs": list(block.get("support_refs") or []),
                    "reading_order": block.get("reading_order"),
                    "type_candidates": list(block.get("type_candidates") or []),
                }
                if pdf_box is not None and pdf_h:
                    child["bbox_pdf_pt"] = [
                        pdf_box[0], pdf_box[1] + index * pdf_h / len(lines),
                        pdf_box[2], pdf_box[1] + (index + 1) * pdf_h / len(lines),
                    ]
                if isinstance(norm_box, list) and len(norm_box) == 4:
                    nh = norm_box[3] - norm_box[1]
                    child["bbox_normalized"] = [
                        norm_box[0], norm_box[1] + index * nh / len(lines),
                        norm_box[2], norm_box[1] + (index + 1) * nh / len(lines),
                    ]
                out.append(child)
            issues.append({
                "issue_id": f"issue.{block.get('block_id')}.form_split",
                "type": "form_block_split",
                "block_ids": [str(block.get("block_id"))],
                "note": str(len(lines)),
            })
            continue
        out.append(block)
    return out


def dedupe_repeated_watermarks(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same watermark string repeated down a page is one legal line plus echoes.

    Annotators keep the first occurrence (topmost) and drop the repeats - they are print artifacts,
    not content. Only exact repeats at distinct positions qualify, and a block that overlaps any
    kept annotation-like neighbour stays (this runs before matching, so keep it conservative).
    """
    counts: dict[str, list[dict[str, Any]]] = {}
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if len(text) >= 8:
            counts.setdefault(text, []).append(block)
    drop: set[str] = set()
    for text, group in counts.items():
        if len(group) < 3:
            continue
        ordered = sorted(group, key=lambda block: (_box(block)[1], _box(block)[0]))
        for block in ordered[1:]:
            drop.add(str(block.get("block_id")))
    if not drop:
        return blocks
    kept = [block for block in blocks if str(block.get("block_id")) not in drop]
    for block_id in sorted(drop):
        issues.append({
            "issue_id": f"issue.{block_id}.watermark_repeat",
            "type": "watermark_repeat_dropped",
            "block_ids": [block_id],
            "note": "",
        })
    return kept


def drop_non_blocks(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove things that are not blocks at all: rules, watermarks, and empty "titles".

    Measured on the annotated corpus: 30 of the machine's "figures" are hairlines/rules (a 1086x26
    strip, a 25x1390 vertical bar), 18 text blocks are download stamps or "△△△△" filler, and most of the
    48 title->text corrections are title blocks with an empty text field.
    """
    width, height = _page_size(page)
    kept: list[dict[str, Any]] = []
    for block in blocks:
        box = _box(block)
        block_type = str(block.get("type"))
        spans_text = str(block.get("text") or "").strip()
        has_caption = any(str(other.get("parent_id") or "") == str(block.get("block_id")) for other in blocks if other is not block)
        if block_type in FIGURE_TYPES and not spans_text and not has_caption:
            block_width, block_height = max(0.0, box[2] - box[0]), max(0.0, box[3] - box[1])
            if block_width > 0 and block_height > 0:
                ratio = block_width / block_height
                # a rule or a divider: extremely elongated and thin in absolute terms
                if (ratio >= 8 or ratio <= 0.125) and min(block_width, block_height) <= max(8.0, height * 0.03):
                    issues.append({"issue_id": f"issue.{block.get('block_id')}.rule", "type": "rule_not_figure_dropped",
                                   "block_ids": [str(block.get("block_id"))], "note": f"{round(block_width)}x{round(block_height)}"})
                    continue
        # NB: watermarks and "△△△" filler are NOT dropped - the annotators keep them as text blocks
        # (dropping them scored 24 blocks worse), so the rule above stays limited to real graphics.
        # NB: an "empty title" is NOT demoted - the annotators keep those (headings whose text the
        # readers never extracted), and demoting them scored 17 type mismatches worse than leaving them.
        kept.append(block)
    return kept


def merge_adjacent_paragraph_blocks(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A paragraph is one block: join text blocks that continue each other down a column.

    Over-merging is as wrong as under-merging (the annotators split back what we glue together), so a
    join needs a real continuation: the same column, a gap smaller than a line, and the previous block
    neither ending a sentence nor the next one starting a new one. Numbered reference entries are the
    one exception - a reference list is always one block.
    """
    width, height = _page_size(page)
    if not width or not height:
        return blocks
    ordered = sorted(blocks, key=lambda block: (block.get("reading_order") or 0))
    merged: list[dict[str, Any]] = []
    for block in ordered:
        # unknown blocks with text are prose the semantic stage has not named yet; they wrap and
        # merge exactly like text does. Footnotes wrap too (a "Note:" line and its numbered items
        # are one footnote block in the ground truth).
        if str(block.get("type")) not in {"text", "unknown", "footnote"} or not _text_of(block):
            merged.append(block)
            continue
        target = None
        # The reading order interleaves columns, so "the previous block in document order" is often a
        # block from the other column - stopping there broke every merge after the first interleave
        # (measured: 32 reference entries collapsed into 6 blocks instead of 2). The neighbour that
        # matters is the previous block in the SAME column: skip other-column blocks while scanning
        # upward, and treat a full-width block (a heading, a table, a rule) as a hard barrier.
        scanned = 0
        for candidate in reversed(merged):
            if str(candidate.get("type")) not in {"text", "unknown", "footnote"} or not _text_of(candidate):
                continue
            box, other = _box(block), _box(candidate)
            span = _overlap([box[0], 0, box[2], 1], [other[0], 0, other[2], 1]) / max(1.0, min(box[2] - box[0], other[2] - other[0]))
            if span >= 0.85:
                gap = box[1] - other[3]
                line_height = max(4.0, min(box[3] - box[1], other[3] - other[1]))
                if -line_height * 0.6 <= gap <= line_height * 0.9:
                    target = candidate
                break
            if other[2] - other[0] >= width * 0.8 or scanned >= 10:
                break
            scanned += 1
        if target is None:
            merged.append(block)
            continue
        previous, current = _text_of(target), _text_of(block)
        # Both entries numbered and in the same column: one reference list (measured: this merges the
        # 20+ entry lists the annotators merged by hand, and the few over-merges cost far less than the
        # 100+ split entries it removes - an explicit consecutive-number test was tried and scored worse).
        # a lead-in line ("Note:", "注意：", "说明：") owns the numbered list that follows: the
        # annotators keep the note header and its items as one block
        first_line = previous.strip().split("\n")[0]
        lead_in = bool(re.fullmatch(r"(Note|NOTE|Notes?|注|注意|说明|备注)\s*[:：]?", first_line))
        if lead_in and NUMBERED_REFERENCE.match(current) and span >= 0.7:
            _merge_into(page, target, block, issues, "note_merge")
            continue
        if NUMBERED_REFERENCE.match(previous) and NUMBERED_REFERENCE.match(current):
            # A *clean* list keeps its entries separate: when the source text prefixes every entry
            # with a tab-marked number on the same line (\t14.\tAuthor), the layout is regular and
            # the annotators keep one block per entry. Only fragmented lists - where OCR scattered
            # the numbers across wrapped lines - are joined into one block (Science-style pages,
            # where 32 stray entries were merged by hand precisely because they cannot be split).
            # Entries that wrap to two+ lines keep their own block (each entry is a readable
            # unit - Nature-style lists, every annotated entry stayed separate). Entries that are
            # single OCR fragments (one line each, numbers drifting across lines) cannot be read
            # as units, so the column joins into one block (Science-style lists, where the
            # annotators merged whole columns).
            clean = True
            for peer in (target, block):
                if re.match(r"^\t?\d{1,3}\.\n", str(peer.get("text", ""))):
                    clean = False  # a newline-numbered entry marks a fragmented list: join it
                    break
            else:
                clean = bool(re.match(r"^\t?\d{1,3}\.\t[^\n]", str(target.get("text", ""))) and re.match(r"^\t?\d{1,3}\.\t[^\n]", str(block.get("text", ""))))
            if span >= 0.7 and not clean:
                _merge_into(page, target, block, issues, "paragraph_merge")
                continue
            merged.append(block)
            continue
        # A wrapped line continues where the sentence was cut mid-way. Anything else - a sentence that
        # ended, a heading, an indented block, a wide left-edge difference - is a separate block in the
        # ground truth, and gluing it back costs more than the merge saves.
        box, other = _box(block), _box(target)
        aligned = abs(other[0] - box[0]) <= width * 0.02
        gap = box[1] - other[3]
        line_height = max(4.0, min(box[3] - box[1], other[3] - other[1]))
        touches = -line_height * 0.6 <= gap <= line_height * 0.35
        combined = max(box[3], other[3]) - min(box[1], other[1])
        # Wrapped prose continues with the same strong-but-guarded join: same column, a gap
        # smaller than a line, equal sizes, and the next line picking up mid-sentence. The guards
        # exist because over-merging costs more than under-merging (measured on annotated pages).
        if not (NUMBERED_REFERENCE.match(previous) and NUMBERED_REFERENCE.match(current)):
            # A single wrapped line joining an already-merged multi-line paragraph is fine: the
            # size guard is only meaningful between two fragments of similar shape.
            heights2 = [box[3] - box[1], other[3] - other[1]]
            single_line = max(4.0, min(box[3] - box[1], other[3] - other[1]) * 3.0)
            same_size2 = (max(heights2) <= single_line) or (min(heights2) / max(1.0, max(heights2)) >= 0.4)
            row_like2 = _looks_like_table_row(previous) or _looks_like_table_row(current)
            starts2 = bool(current) and (
                current[0].islower() or current[0].isdigit() or "\u4e00" <= current[0] <= "\u9fff" or current[0] in "(["
            )
            # Two escape hatches the annotations support:
            #   1. an indented first line that sits tight above the paragraph body (x0 differs by
            #      8-28px, gap < 0.3 line) belongs to that paragraph - the annotators merge it;
            #   2. the standard mid-sentence continuation (guarded as below).
            line_h2 = max(4.0, min(box[3] - box[1], other[3] - other[1]))
            indent = 8.0 <= abs(other[0] - box[0]) <= width * 0.025
            form_line = previous.rstrip().endswith(("：", ":", "）", ")")) or "___" in previous
            # a sentence that ended is a boundary even in the indented case: bullets and list
            # items end with 。 and start a new block in the ground truth
            if indent and not form_line and not previous.rstrip().endswith((".", "。", "!", "?", "！", "？", ";", "；")) and -line_h2 * 0.3 <= gap <= line_h2 * 0.3 and not row_like2 and combined <= height * 0.6:
                _merge_into(page, target, block, issues, "paragraph_merge")
                continue
            form2 = (
                "___" in previous or "___" in current
                or previous.rstrip().endswith(("：", ":")) or current.rstrip().endswith(("：", ":"))
            )
            clause_heading = bool(re.match(r"^第[一二三四五六七八九十百千\d]+条\s*\S{1,10}$", previous.strip()))
            if (
                not form2
                and not clause_heading
                and aligned
                and touches
                and same_size2
                and not row_like2
                and starts2
                and combined <= height * 0.6
                and not previous.endswith((".", "。", "!", "?", "！", "？", ":", "：", ";", "；"))
                and not NUMBERED_TITLE.match(previous)
                and not NUMBERED_TITLE.match(current)
                and not block.get("parent_id")
                and not target.get("parent_id")
            ):
                _merge_into(page, target, block, issues, "paragraph_merge")
                continue
            merged.append(block)
            continue
        heights = [box[3] - box[1], other[3] - other[1]]
        same_size = min(heights) / max(1.0, max(heights)) >= 0.72
        row_like = _looks_like_table_row(previous) or _looks_like_table_row(current)
        starts_continuation = bool(current) and (
            current[0].islower() or current[0].isdigit() or "\u4e00" <= current[0] <= "\u9fff" or current[0] in "(["
        )
        if (
            aligned
            and touches
            and same_size
            and not row_like
            and starts_continuation
            and combined <= height * 0.6
            and not previous.endswith((".", "。", "!", "?", "！", "？", ":", "：", ";", "；"))
            and not NUMBERED_TITLE.match(previous)
            and not NUMBERED_TITLE.match(current)
            and not block.get("parent_id")
            and not target.get("parent_id")
        ):
            _merge_into(page, target, block, issues, "paragraph_merge")
            continue
        merged.append(block)
    # Second pass: an indented paragraph opening that sits directly above a same-column block was
    # processed before that block existed in the scan order; absorb it now (annotators keep the
    # opening line inside the paragraph block).
    # Geometry lookup instead of scan-order neighbour: the opening line may sit anywhere in the
    # processed list, its paragraph body is the nearest block that starts right below it in the
    # same column with a small first-line indent.
    changed = True
    while changed:
        changed = False
        for i, block in enumerate(merged):
            if str(block.get("type")) not in {"text", "unknown"} or not _text_of(block):
                continue
            if NUMBERED_REFERENCE.match(_text_of(block)):
                continue
            if re.match(r"^第[一二三四五六七八九十百千\d]+条\s*\S{1,10}$", _text_of(block).strip()):
                continue  # a clause heading keeps its body in a separate block
            _tb = _text_of(block).strip()
            if "___" in _tb or _tb.endswith(("：", ":")) or ("：" in _tb and _tb.endswith("）") and len(_tb) <= 24):
                continue  # a field/signature line is never a paragraph opener
            bb = _box(block)
            if (bb[3] - bb[1]) > max(40.0, height * 0.03):
                continue  # only a short opening line
            best = None
            for j, other in enumerate(merged):
                if j == i:
                    continue
                if str(other.get("type")) not in {"text", "unknown"} or not _text_of(other):
                    continue
                nb = _box(other)
                if nb[1] < bb[3] - 2.0:
                    continue  # must start below
                if not (8.0 <= abs(nb[0] - bb[0]) <= width * 0.025):
                    continue
                line_h = max(4.0, bb[3] - bb[1])
                gap = nb[1] - bb[3]
                if not (0 <= gap <= line_h * 0.35):
                    continue
                # form lines ("甲方（采购方）：", "名称：______") are independent in the ground
                # truth; a colon-terminated or underscore-filled line never absorbs its neighbour
                if previous.rstrip().endswith(("：", ":", "）", ")")) or "___" in previous:
                    continue
                if (nb[3] - nb[1]) < line_h:
                    continue
                if best is None or nb[1] < _box(merged[best])[1]:
                    best = j
            if best is not None:
                _merge_into(page, merged[best], block, issues, "paragraph_merge")
                merged.pop(i)
                changed = True
                break
    return merged


def cluster_figure_blocks(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One figure is one block: fold image fragments into their figure and drop table-internal rasters.

    Image blocks were the single biggest source of annotations being deleted (86 of them): one visual
    figure arrives as several overlapping image blocks, and pictures inside tables come back as images
    even though the table already owns them.
    """
    width, height = _page_size(page)
    page_area = max(1.0, width * height)
    keep: list[dict[str, Any]] = []
    for block in sorted(blocks, key=lambda item: -_area(_box(item))):
        box = _box(block)
        if block.get("type") in FIGURE_TYPES:
            # inside a table: the table is the block
            if any(
                other.get("type") == "table" and _ioa(box, _box(other)) >= 0.8
                for other in blocks
                if other is not block
            ):
                issues.append({"issue_id": f"issue.{block.get('block_id')}.image_in_table", "type": "image_inside_table_dropped",
                               "block_ids": [str(block.get("block_id"))], "note": "image is inside a table"})
                continue
            # a decorative fragment
            if _area(box) < 0.004 * page_area and not _text_of(block):
                issues.append({"issue_id": f"issue.{block.get('block_id')}.image_fragment", "type": "image_fragment_dropped",
                               "block_ids": [str(block.get("block_id"))], "note": "tiny image fragment"})
                continue
            owner = next(
                (
                    other
                    for other in keep
                    if other.get("type") in FIGURE_TYPES
                    # Only a genuinely smaller graphic inside a bigger one is the same figure: two
                    # side-by-side figures of similar size are two figures. Without the area test this
                    # merged 43 real image blocks the annotator kept (the biggest loss in the last round).
                    and min(_area(box), _area(_box(other))) <= 0.5 * max(_area(box), _area(_box(other)))
                    and (_ioa(box, _box(other)) >= 0.90 or _ioa(_box(other), box) >= 0.90)
                ),
                None,
            )
            if owner is not None:
                _merge_into(page, owner, block, issues, "figure_merge")
                continue
        keep.append(block)
    order = {id(block): index for index, block in enumerate(sorted(blocks, key=lambda item: item.get("reading_order") or 0))}
    keep.sort(key=lambda block: order.get(id(block), 0))
    return keep


def verify_titles(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> None:
    """A heading has to earn it: body text that the model called a title becomes text again.

    52 of the 95 type corrections were ``title -> text``: the models label short body lines (and figure
    labels) as ``paragraph_title``. A block stays a title only with typographic support - a numbering
    pattern, a centred narrow line, or both parsers agreeing - otherwise it is what it looks like.
    """
    width, height = _page_size(page)
    for block in blocks:
        if block.get("type") != "title":
            continue
        text = _text_of(block)
        box = _box(block)
        if NUMBERED_TITLE.match(text):
            continue
        centred = abs((box[0] + box[2]) / 2 - width / 2) <= width * 0.08
        narrow = (box[2] - box[0]) <= width * 0.62
        short = len(text) <= 60 or _looks_like_cjk(text)
        # Left-aligned short text is body text, however the model labelled it: only a numbering
        # pattern or a centred narrow line (or CJK headings, which are short by nature) stay titles.
        if NUMBERED_TITLE.match(text) or (centred and narrow and short):
            continue
        if _looks_like_cjk(text) and len(text) <= 30:
            continue
        block["type"] = "text"
        block["type_candidates"] = sorted(set(block.get("type_candidates") or []) | {"text"})
        issues.append({"issue_id": f"issue.{block.get('block_id')}.title_demoted", "type": "title_demoted_to_text",
                       "block_ids": [str(block.get("block_id"))], "note": text[:40]})


def _looks_like_cjk(text: str) -> bool:
    return bool(text) and sum(1 for char in text if "\u4e00" <= char <= "\u9fff") >= max(1, len(text) // 3)


def attach_captions_by_geometry(page: dict[str, Any], blocks: list[dict[str, Any]], issues: list[dict[str, Any]]) -> None:
    """A caption is the line that sits against a figure or table and spans the same width.

    Captions were under-produced (20 corrections): the models only mark a caption when the wording looks
    like one, so "图 3 不同温度下的曲线" next to the plot stayed plain text. Position and width are the
    reliable signal, and the caption also records which block it belongs to.
    """
    width, height = _page_size(page)
    if not width or not height:
        return
    anchors = [block for block in blocks if block.get("type") in FIGURE_TYPES | {"table", "formula"}]
    for block in blocks:
        if block.get("type") != "text" or not _text_of(block):
            continue
        box = _box(block)
        for anchor in anchors:
            anchor_box = _box(anchor)
            horizontal = _overlap([box[0], 0, box[2], 1], [anchor_box[0], 0, anchor_box[2], 1]) / max(1.0, min(box[2] - box[0], anchor_box[2] - anchor_box[0]))
            if horizontal < 0.7:
                continue
            below = box[1] - anchor_box[3]
            above = anchor_box[1] - box[3]
            limit = height * 0.025
            if not (-limit * 0.5 <= below <= limit or -limit * 0.5 <= above <= limit):
                continue
            if _area(box) > _area(anchor_box) * 0.25:
                continue  # too big to be a caption
            if len(_text_of(block)) > 200 or _text_of(block).count("\n") > 2:
                continue  # a caption is one or two lines
            block["type"] = "table_caption" if anchor.get("type") == "table" else "image_caption"
            block["parent_id"] = str(anchor.get("block_id"))
            issues.append({"issue_id": f"issue.{block.get('block_id')}.caption_attached", "type": "caption_attached_by_geometry",
                           "block_ids": [str(block.get("block_id")), str(anchor.get("block_id"))], "note": "position + width"})
            break


DEFAULT_RULES = ("figures", "paragraphs", "titles", "captions")


def apply_inventory_rules(
    page: dict[str, Any],
    blocks: list[dict[str, Any]],
    issues: list[dict[str, Any]] | None = None,
    rules: tuple[str, ...] = DEFAULT_RULES,
) -> list[dict[str, Any]]:
    """Run the inventory stage; each rule can be switched off for ablation or rollback."""
    issues = issues if issues is not None else []
    if "figures" in rules:
        blocks = absorb_figure_internals(page, blocks, issues)
        blocks = drop_non_blocks(page, blocks, issues)
        blocks = dedupe_repeated_watermarks(page, blocks, issues)
        blocks = split_form_field_blocks(page, blocks, issues)
        blocks = merge_overlapping_line_fragments(page, blocks, issues)
        blocks = relabel_margin_logos(page, blocks, issues)
        blocks = cluster_figure_blocks(page, blocks, issues)
    if "paragraphs" in rules:
        blocks = merge_adjacent_paragraph_blocks(page, blocks, issues)
        # a merge can assemble a multi-line form block; re-run the split so field lines stay separate
        blocks = split_form_field_blocks(page, blocks, issues)
    if "titles" in rules:
        verify_titles(page, blocks, issues)
    if "captions" in rules:
        attach_captions_by_geometry(page, blocks, issues)
    for index, block in enumerate(blocks):
        block["reading_order"] = index
    return blocks


def refresh_layout(page: dict[str, Any]) -> dict[str, Any]:
    """Re-apply the inventory rules and the reading order to an already-built page.

    Evidence files are built once and then consumed by every later run; the deterministic layers
    (figure absorption, rule/watermark cleanup, paragraph merging, reading order) have moved on, so
    a stale candidate silently carried yesterday's layout into today's model calls. This rebuilds
    those layers in place - no model output is touched - so a run always reasons about the current
    deterministic shape.
    """
    blocks = (page.get("current_candidate") or {}).get("blocks") or []
    if not blocks:
        return page
    issues: list[dict[str, Any]] = []
    blocks = apply_inventory_rules(page, blocks, issues, rules=("figures", "paragraphs"))
    from .reading_order import build_page_order

    model_orders: dict[str, list[str]] = {}
    owner: dict[str, list[str]] = {}
    for block in blocks:
        for ref in block.get("support_refs", []) or []:
            owner.setdefault(str(ref), []).append(block["block_id"])
    for source in ("mineru", "paddle"):
        items = sorted(
            (item for item in (page.get("evidence", {}).get(source) or []) if isinstance(item, dict)),
            key=lambda item: str(item.get("evidence_id") or ""),
        )
        order: list[str] = []
        for item in items:
            for block_id in owner.get(str(item.get("evidence_id")), []):
                if block_id not in order:
                    order.append(block_id)
        if len(order) >= 3:
            model_orders[source] = order
    image = page.get("page_image") or {}
    result = build_page_order(
        blocks,
        float(image.get("width") or 0),
        float(image.get("height") or 0),
        model_orders=model_orders,
    )
    position = {block_id: index for index, block_id in enumerate(result["order"])}
    blocks.sort(key=lambda block: position.get(block["block_id"], len(blocks)))
    for order_index, block in enumerate(blocks):
        block["reading_order"] = order_index
    page["current_candidate"] = {"blocks": blocks}
    page["reading_order"] = result.get("layout", page.get("reading_order"))
    return page
