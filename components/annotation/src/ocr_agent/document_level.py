"""Document-level passes: decisions no single page can make on its own.

A graphic that repeats at the same position on many pages is page furniture (a template band, a
brand watermark, a background); the annotators never label it as content. A single page cannot know
this - the same figure legitimately appears twice on one page - so the judgement has to happen at
the document level, after every page is revised. Graphics that share a horizontal band with a
running head / footer text line are exempt: they are brand art the annotators keep alongside the
page furniture.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _bbox_key(block: dict[str, Any]) -> tuple[int, int, int, int]:
    values = [float(v) for v in (block.get("bbox_image_px") or [0, 0, 0, 0])]
    return tuple(int(round(v / 8.0)) for v in values)  # 8px tolerance


def _band_overlap_y(box_a: tuple[float, float], box_b: tuple[float, float]) -> bool:
    """True when two y-intervals overlap by more than half of the smaller one."""
    a1, a2 = box_a
    b1, b2 = box_b
    overlap = min(a2, b2) - max(a1, b1)
    return overlap >= 0.5 * min(a2 - a1, b2 - b1)


def drop_template_graphics(page_count: int, results: dict[int, dict[str, Any]], *, min_pages: int = 3) -> set[int]:
    """Remove image blocks whose exact box repeats across >= min_pages of the document.

    Returns the set of pages whose revision changed. Real figures are never pixel-identical across
    pages; template art is (same header band, same footer brand, same background strip).
    """
    if page_count < min_pages:
        return set()
    occurrences: defaultdict[tuple, list[tuple[int, str]]] = defaultdict(list)
    bands: list[tuple[float, float]] = []
    for page_number, result in results.items():
        for block in result.get("revision", {}).get("blocks", []):
            kind = str(block.get("type"))
            if kind in {"image", "chart", "seal"}:
                occurrences[_bbox_key(block)].append((page_number, str(block.get("block_id"))))
            elif kind in {"header", "footer"} and str(block.get("text") or "").strip():
                values = [float(v) for v in (block.get("bbox_image_px") or [0, 0, 0, 0])]
                bands.append((values[1], values[3]))
    touched: set[int] = set()
    for key, places in occurrences.items():
        seen_pages = {page for page, _ in places}
        if len(seen_pages) < min_pages:
            continue
        values = [float(v) * 8.0 for v in key]
        graphic_band = (values[1], values[3])
        if any(_band_overlap_y(graphic_band, band) for band in bands):
            continue  # brand art inside the running-head/footer band stays
        for page_number, block_id in places:
            touched_by_key = True
            if touched_by_key:
                touched.add(page_number)
    # second pass with per-page block ids
    drops: defaultdict[int, list[str]] = defaultdict(list)
    for key, places in occurrences.items():
        seen_pages = {page for page, _ in places}
        if len(seen_pages) < min_pages:
            continue
        values = [float(v) * 8.0 for v in key]
        graphic_band = (values[1], values[3])
        if any(_band_overlap_y(graphic_band, band) for band in bands):
            continue
        for page_number, block_id in places:
            drops[page_number].append(block_id)
    for page_number, block_ids in drops.items():
        result = results[page_number]
        revision = result["revision"]
        drop = set(block_ids)
        before = len(revision["blocks"])
        revision["blocks"] = [b for b in revision["blocks"] if str(b.get("block_id")) not in drop]
        if len(revision["blocks"]) == before:
            continue
        revision["operation_log"].append({"kind": "template_graphic_drop", "block_ids": sorted(drop)})
        touched.add(page_number)
    return touched
