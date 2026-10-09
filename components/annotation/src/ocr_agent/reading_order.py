"""Column-aware reading order for OCR-Agent pages.

A single "sort by position" pass is wrong for anything but a one-column page: in a two-column paper
the correct order is *down the left column, then down the right one*, and a full-width heading or
table splits the page into segments that each restart at the left column. Getting this wrong silently
poisons every downstream consumer (markdown, reading-flow comparison, Contract GT reading order),
which is why the order carries its own evidence and a confidence verdict instead of being a bare sort.

Model
-----
A page is described as an ordered list of **regions**::

    [margin-band] [region] [region] ... [margin-band]

* a **column region** is a vertical span where content sits in ``columns`` columns, ordered
  left to right, each column top to bottom (same-line blocks by x);
* a **full-span block** (heading, wide table, figure) is its own single-block region and separates
  the column regions above and below it;
* **margin bands** (running head, page number, footer) hug the top/bottom edges and are ordered first
  / last regardless of columns.

Generalisation rules
--------------------
* Column detection uses **geometry only** (block boxes / PDF line boxes), works for 1-3+ columns, and
  refuses to split when the evidence is weak (a wide whitespace gap inside an indented paragraph is
  not a gutter): a gutter needs content on both sides, each side spanning a tall share of the page,
  and few blocks crossing it.
* Ambiguity is reported, never guessed: ``confidence`` drops to CHECK when blocks cross the gutter
  that are not plausible full-span separators, or when side-by-side blocks appear without a clean
  gutter (side bars, text next to a table).
* Rotated text, marginal notes and figure captions are handled by treating every block the same way
  and letting the region model place them, instead of special-casing document families.

The module is deliberately dependency-free (no PIL, no numpy) so it can run inside the page builder
and inside offline audits.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Bands at the very top/bottom of the page that carry running furniture rather than body text.
MARGIN_BAND_RATIO = 0.07
# A gutter must leave at least this share of the page's content area on each side.
MIN_SIDE_CONTENT_SHARE = 0.22
# ... and each column must reach this far down (or up) the page to count as a real column.
MIN_COLUMN_HEIGHT_SHARE = 0.30
# Blocks that span this share of the content width (and cross the gutter) are full-span separators.
FULL_SPAN_WIDTH_SHARE = 0.62
# Types that may act as a separator even when they do not span the full width (headings).
SEPARATOR_TYPES = {"title"}


@dataclass
class Block:
    """The only block facts the orderer needs."""

    block_id: str
    bbox: list[float]
    type: str = "text"

    @property
    def left(self) -> float:
        return float(self.bbox[0])

    @property
    def right(self) -> float:
        return float(self.bbox[2])

    @property
    def top(self) -> float:
        return float(self.bbox[1])

    @property
    def bottom(self) -> float:
        return float(self.bbox[3])

    @property
    def width(self) -> float:
        return max(0.0, self.right - self.left)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2


@dataclass
class Layout:
    """Detected page structure, plus why it was (not) trusted."""

    columns: int = 1
    method: str = "single_column"
    gutter: list[float] | None = None
    column_bands: list[list[float]] = field(default_factory=list)   # x ranges, left to right
    separators: list[str] = field(default_factory=list)             # full-span block ids
    confidence: str = "PASS"
    warnings: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


def _to_blocks(blocks: Iterable[dict[str, Any]]) -> list[Block]:
    out: list[Block] = []
    for block in blocks:
        box = block.get("bbox_image_px")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        out.append(Block(str(block.get("block_id")), [float(value) for value in box], str(block.get("type") or "unknown")))
    return out


def _content_share(blocks: Sequence[Block], total: float) -> float:
    return sum(block.area for block in blocks) / total if total > 0 else 0.0


def _column_bounds(blocks: Sequence[Block]) -> list[float]:
    return [min(block.left for block in blocks), max(block.right for block in blocks)]


def detect_layout(
    blocks: Iterable[dict[str, Any]],
    page_width: float,
    page_height: float,
    *,
    max_columns: int = 3,
) -> Layout:
    """Detect column structure from block geometry.

    Candidates are the block edges themselves: a gutter is a vertical strip no block crosses. The
    best candidate maximizes the content on both sides while penalizing blocks that straddle it, so a
    paragraph indent or a single wide table does not turn a one-column page into a two-column one.
    """
    page_width = float(page_width)
    page_height = float(page_height)
    items = _to_blocks(blocks)
    if len(items) < 4 or page_width <= 0 or page_height <= 0:
        return Layout(notes={"reason": "too_little_content", "blocks": len(items)})

    total_area = sum(block.area for block in items) or 1.0
    # Furniture (running head / footer / page number) is excluded from column detection: it usually
    # spans the page and would hide the gutter.
    body = [block for block in items if block.type not in {"header", "footer", "page_number"}]
    if len(body) < 4:
        body = items

    candidates = sorted({round(block.left, 3) for block in body} | {round(block.right, 3) for block in body})
    best: tuple[float, float] | None = None
    for split in candidates:
        left = [block for block in body if block.right <= split + 1.0]
        right = [block for block in body if block.left >= split - 1.0]
        if len(left) < 2 or len(right) < 2:
            continue
        crossing = [block for block in body if block.left < split - 1.0 and block.right > split + 1.0]
        share = min(_content_share(left, total_area), _content_share(right, total_area))
        if share < MIN_SIDE_CONTENT_SHARE:
            continue
        span = min(
            (max(block.bottom for block in left) - min(block.top for block in left)) / page_height,
            (max(block.bottom for block in right) - min(block.top for block in right)) / page_height,
        )
        if span < MIN_COLUMN_HEIGHT_SHARE:
            continue
        # Keep only blocks wide enough to be plausible separators; narrow straddlers are noise.
        real_crossing = [block for block in crossing if block.width >= page_width * 0.25]
        score = share - 0.15 * len(real_crossing)
        if best is None or score > best[0]:
            best = (score, split)

    if best is None:
        return Layout(
            columns=1,
            method="single_column",
            column_bands=[_column_bounds(body)],
            confidence="PASS",
            notes={"blocks": len(items)},
        )

    split = best[1]
    content_left = min(block.left for block in body)
    content_right = max(block.right for block in body)
    left_band = [content_left, split]
    right_band = [split, content_right]
    separators = [
        block.block_id for block in body
        if block.left < split - 1.0 and block.right > split + 1.0 and block.width >= page_width * FULL_SPAN_WIDTH_SHARE
    ]
    crossing = [block for block in body if block.left < split - 1.0 and block.right > split + 1.0]
    heights = sorted(block.height for block in body) or [1.0]
    median_height = heights[len(heights) // 2]
    # A single line centred over the gutter is a separator line (journal running lines, centred
    # subtitles); only *multi-line* blocks crossing the split make the column structure doubtful.
    ambiguous = [
        block for block in crossing
        if block.block_id not in separators
        and block.type not in SEPARATOR_TYPES
        and block.height > median_height * 2.5
    ]
    warnings: list[str] = []
    confidence = "PASS"
    if ambiguous:
        warnings.append("blocks_cross_the_gutter")
        confidence = "CHECK"
    return Layout(
        columns=2,
        method="gutter_columns",
        gutter=[round(split, 3), round(split, 3)],
        column_bands=[left_band, right_band],
        separators=sorted(separators),
        confidence=confidence,
        warnings=warnings,
        notes={"blocks": len(items), "crossing_blocks": [block.block_id for block in crossing][:8], "max_columns": max_columns},
    )


def _column_index(layout: Layout, block: Block) -> int:
    if layout.columns <= 1 or not layout.column_bands:
        return 0
    for index, (start, end) in enumerate(layout.column_bands):
        if block.center_x <= end:
            return index
    return len(layout.column_bands) - 1


def _is_full_span(layout: Layout, block: Block, page_width: float) -> bool:
    """A block that physically crosses the gutter cannot belong to one column: it separates them.

    Width matters as much as the crossing: a narrow block that happens to straddle the split is a
    bridging artefact, not a separator (it stays in the column region).
    """
    if block.block_id in layout.separators:
        return True
    if layout.columns <= 1:
        return False
    if block.width < page_width * FULL_SPAN_WIDTH_SHARE:
        return False
    if block.type in SEPARATOR_TYPES:
        return True
    bands = layout.column_bands
    if len(bands) >= 2 and block.left <= bands[0][1] and block.right >= bands[1][0]:
        return True
    return False


def order_blocks(
    blocks: Iterable[dict[str, Any]],
    layout: Layout,
    page_width: float,
    page_height: float,
) -> list[str]:
    """Return block ids in reading order for the detected layout.

    Regions are built by walking the page vertically: full-span blocks split it, and inside every
    column region the columns are read left to right, each top to bottom.
    """
    items = _to_blocks(blocks)
    if not items:
        return []
    page_width = float(page_width)
    page_height = float(page_height)

    def in_top_band(block: Block) -> bool:
        return block.center_y <= page_height * MARGIN_BAND_RATIO

    def in_bottom_band(block: Block) -> bool:
        return block.center_y >= page_height * (1 - MARGIN_BAND_RATIO)

    def furniture_kind(block: Block) -> bool:
        """Running head / footer / page number: short blocks in the page margins.

        Type alone is not enough (a model may label body text as header), and position alone is not
        enough either (a figure can sit low on the page), so both signals have to agree.
        """
        short = block.height <= page_height * 0.08
        if not short:
            return False
        if block.type in {"header", "page_number"}:
            return in_top_band(block) or (block.type == "page_number" and in_bottom_band(block))
        if block.type == "footer":
            return in_bottom_band(block)
        # a small graphic in the bottom margin is footer brand art: it reads with the footer, not
        # with the body (annotators place it at the very end of the page)
        if block.type in {"image", "chart", "seal"} and in_bottom_band(block):
            return True
        return False

    # Running head first, running foot last - always, and each group is ordered inside itself. A
    # parser may list furniture anywhere (MinerU appends it at the end of the page list, Paddle puts
    # the head first); neither convention is a reading order, so the policy is fixed here.
    furniture_head = [block for block in items if furniture_kind(block) and block.type in {"header", "page_number"} and (in_top_band(block) or not in_bottom_band(block))]
    furniture_foot = [block for block in items if furniture_kind(block) and block not in furniture_head]
    furniture_ids = {block.block_id for block in furniture_head} | {block.block_id for block in furniture_foot}
    body = [block for block in items if block.block_id not in furniture_ids]
    body.sort(key=lambda block: (block.top, block.left))

    regions: list[list[Block]] = []
    current: list[Block] = []
    for block in body:
        if _is_full_span(layout, block, page_width):
            if current:
                regions.append(current)
                current = []
            regions.append([block])
            continue
        current.append(block)
    if current:
        regions.append(current)

    ordered: list[Block] = []
    ordered.extend(_reading_bands(furniture_head))
    for region in regions:
        if len(region) == 1 and _is_full_span(layout, region[0], page_width):
            ordered.append(region[0])
            continue
        column_count = max(layout.columns, 1)
        buckets: list[list[Block]] = [[] for _ in range(column_count)]
        for block in region:
            buckets[min(_column_index(layout, block), column_count - 1)].append(block)
        grid = _row_band_grid(region, buckets, layout, page_width, page_height)
        if grid is not None:
            # The grid is a structural decision, on par with the column model: mark it so the
            # model-consensus override and the column audit do not undo it (parsers list chart
            # grids column-major, which would resurrect exactly the order the grid fixes).
            layout.notes["row_band_grid"] = True
            ordered.extend(grid)
            continue
        for bucket in buckets:
            ordered.extend(_reading_bands(bucket))
    ordered.extend(_reading_bands(furniture_foot))
    ordered_ids = [block.block_id for block in ordered]
    if not layout.notes.get("row_band_grid"):
        by_id = {block.block_id: block for block in items}
        texts = {str(b.get("block_id")): str(b.get("text") or "") for b in blocks}
        split = _article_boundary_split(ordered_ids, by_id, texts)
        if split is not None:
            return split
    return ordered_ids



def _row_band_grid(region: Sequence[Block], buckets: Sequence[Sequence[Block]], layout: "Layout", page_width: float = 0.0, page_height: float = 0.0) -> list[Block] | None:
    """Row-major order for grid pages (caption+figure cells, multi-panel figures).

    A page of chart cells is read row by row - every chart of the first row before any chart of the
    second - while the column model would read the whole left column first. Text columns never
    trigger this: their blocks are single text lines (short), and their tops do not align across
    columns. The grid needs at least two bands that each reach across columns and cover most of the
    region, and the region must be dominated by cell-like blocks (figures, captions, tall cells).
    """
    if len(region) < 4 or len(buckets) < 2:
        return None
    col_of = {block.block_id: index for index, bucket in enumerate(buckets) for block in bucket}
    # Type decides: a chart/caption cell grid vs. two text columns (whose 300px paragraphs would
    # pass any height test). A grid page is dominated by non-text cells.
    caption_like = {
        "image", "chart", "seal", "image_caption", "table_caption", "table", "formula",
        "title", "unknown", "text", "footnote",
    }
    cell_like = sum(
        1 for block in region
        if block.type in {"image", "chart", "seal", "image_caption", "table_caption", "table", "formula"}
        or (
            block.type in caption_like
            and block.height <= page_height * 0.06
            and (block.right - block.left) <= page_width * 0.45
        )
    )
    if cell_like < 0.6 * len(region):
        return None
    remaining = sorted(region, key=lambda block: (block.top, block.left))
    bands: list[list[Block]] = []
    while remaining:
        first = remaining.pop(0)
        band = [first]
        keep: list[Block] = []
        for block in remaining:
            tolerance = max(3.0, min(first.height, block.height) * 0.3)
            if abs(block.top - first.top) <= tolerance:
                band.append(block)
            else:
                keep.append(block)
        bands.append(band)
        remaining = keep
    multi = [band for band in bands if len({col_of.get(block.block_id, 0) for block in band}) >= 2]
    if len(multi) < 2 or sum(len(band) for band in multi) < 0.7 * len(region):
        return None
    ordered: list[Block] = []
    for band in bands:
        ordered.extend(sorted(band, key=lambda block: (block.left, block.top)))
    return ordered



def _article_boundary_split(order: list[str], by_id: dict[str, Block], texts: dict[str, str]) -> list[str] | None:
    """A mid-page article boundary reads as: the first article's tail, then the new article.

    Journal pages often stack the end of one article (its reference list, acknowledgements,
    supplementary note, received-date line) above the start of the next. The column model reads
    straight down and interleaves the two articles; the annotators finish the first article across
    columns, then start the new one. The boundary is a short, mostly-uppercase title whose column
    contains numbered reference lines right above it - a section heading on a normal page has no
    reference list above it, so this does not fire there.
    """
    boundary = None
    for index, block_id in enumerate(order):
        block = by_id.get(block_id)
        if block is None or block.type not in {"title", "text", "unknown"}:
            continue
        text = texts.get(block_id, "").strip()
        if not (8 <= len(text) <= 48):
            continue
        letters = [ch for ch in text if ch.isalpha()]
        if not letters or sum(ch.isupper() for ch in letters) / len(letters) < 0.6:
            continue
        # numbered reference lines above, same column
        refs_above = 0
        for other_id in order[:index]:
            other = by_id.get(other_id)
            if other is None or other.type != "text":
                continue
            if not (other.left < block.right and other.right > block.left):
                continue
            if other.top >= block.top:
                continue
            if re.match(r"^\s*\d{1,3}[\.\)]\s", texts.get(other_id, "")):
                refs_above += 1
        if refs_above >= 1:
            boundary = block
            break
    if boundary is None:
        return None
    # blocks whose top sits within a line of the boundary and share its column belong to the new
    # article (the boundary title itself often arrives 1px below the first body line)
    cut = boundary.top + boundary.height
    before, after = [], []
    for bid in order:
        block = by_id[bid]
        same_column = block.left < boundary.right and block.right > boundary.left
        if block.top < boundary.top - 2.0 and not (same_column and block.top >= boundary.top - 30.0):
            before.append(bid)
        else:
            after.append(bid)
    if not before or not after:
        return None
    return before + after

def _reading_bands(blocks: Sequence[Block]) -> list[Block]:
    """Vertical sort that keeps same-line blocks together and reads them left to right.

    Two blocks are on one line when their tops are close relative to their height and they overlap
    vertically; without this, a figure sitting 2px lower than the caption beside it would be read
    after it.
    """
    remaining = sorted(blocks, key=lambda block: (block.top, block.left))
    ordered: list[Block] = []
    while remaining:
        first = remaining.pop(0)
        band = [first]
        keep: list[Block] = []
        for block in remaining:
            heights = [max(1.0, item.height) for item in band] + [max(1.0, block.height)]
            tolerance = max(3.0, min(heights) * 0.25)
            overlap = max(0.0, min(first.bottom, block.bottom) - max(first.top, block.top)) / max(1.0, min(max(1.0, first.height), max(1.0, block.height)))
            if abs(block.top - first.top) <= tolerance and overlap >= 0.70:
                band.append(block)
            else:
                keep.append(block)
        ordered.extend(sorted(band, key=lambda block: (block.left, block.top)))
        remaining = keep
    return ordered


def audit_order(
    blocks: Iterable[dict[str, Any]],
    order: Sequence[str],
    layout: Layout,
    page_width: float,
) -> list[str]:
    """Warnings about an existing order, independent of who produced it.

    ``column_interleave`` is the signature failure of position-only sorting on a column page: a block
    from a right column is followed by a block from a left column that starts clearly below it.
    """
    by_id = {block.block_id: block for block in _to_blocks(blocks)}
    warnings: list[str] = []
    if layout.notes.get("row_band_grid"):
        return []  # a chart-cell grid is read row by row; the column interleave test does not apply
    if layout.columns > 1:
        separator_ids = set(layout.separators)
        interleaved = 0
        for left_id, right_id in zip(order, order[1:]):
            first, second = by_id.get(left_id), by_id.get(right_id)
            if first is None or second is None:
                continue
            if first.block_id in separator_ids or second.block_id in separator_ids:
                continue
            if first.type in {"header", "footer", "page_number"} or second.type in {"header", "footer", "page_number"}:
                continue
            if _column_index(layout, first) > _column_index(layout, second) and second.top > first.top + max(6.0, first.height * 0.5):
                interleaved += 1
        if interleaved:
            warnings.append(f"column_interleave:{interleaved}")
    return warnings


def pairwise_concordance(first: Sequence[str], second: Sequence[str]) -> float:
    """Share of block pairs the two orders agree on (1.0 = identical order).

    Used to compare the geometry order against each parser's own page order: agreement is evidence,
    disagreement is a signal that a human should look at the page rather than a reason to guess.
    """
    common = [block_id for block_id in first if block_id in set(second)]
    if len(common) < 3:
        return 1.0
    position = {block_id: index for index, block_id in enumerate(second)}
    concordant = discordant = 0
    for index, left in enumerate(common):
        for right in common[index + 1:]:
            if position[left] < position[right]:
                concordant += 1
            else:
                discordant += 1
    return concordant / max(1, concordant + discordant)


def _consensus_order(orders: "list[list[str]]") -> list[str]:
    """Merge several orders into one by median rank: the order two independent parsers agree on."""
    ranks: dict[str, list[int]] = {}
    for order in orders:
        for position, block_id in enumerate(order):
            ranks.setdefault(block_id, []).append(position)
    def median(values: list[int]) -> float:
        ordered = sorted(values)
        middle = len(ordered) // 2
        return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return sorted(ranks, key=lambda block_id: (median(ranks[block_id]), block_id))


def build_page_order(
    blocks: Iterable[dict[str, Any]],
    page_width: float,
    page_height: float,
    *,
    existing_order: Sequence[str] | None = None,
    model_orders: dict[str, Sequence[str]] | None = None,
    agreement_floor: float = 0.80,
) -> dict[str, Any]:
    """One call for the page builder: detect the layout, order the blocks, and report the evidence.

    ``model_orders`` are each parser's own page order (MinerU / Paddle block sequence, mapped to block
    ids). They never override the structure - columns are measured from geometry - but they are an
    independent second opinion: high agreement promotes confidence, disagreement downgrades the page
    to CHECK so an annotator settles it instead of the pipeline silently shipping a wrong order.
    """
    block_list = list(blocks)
    layout = detect_layout(block_list, page_width, page_height)
    order = order_blocks(block_list, layout, page_width, page_height)
    warnings = audit_order(block_list, existing_order or order, layout, page_width)
    body_position = {block_id: index for index, block_id in enumerate(order)}
    agreements: dict[str, float] = {}
    for name, model_order in (model_orders or {}).items():
        # Compare on body blocks only: parsers place running furniture differently by convention.
        geometry = [block_id for block_id in order if block_id in set(model_order)]
        agreements[name] = round(pairwise_concordance(geometry, list(model_order)), 3)
    del body_position
    # Geometry is a model, not the truth: when two independent parsers agree with each other and both
    # disagree with the measured columns, their consensus wins (unusual layouts - side bars, mixed
    # column widths, tables beside text - are exactly where a gutter heuristic breaks down). The page
    # is then declared non-columnar so the column audit stops applying.
    overridden = False
    if warnings and len(model_orders or {}) >= 2:
        keys = sorted(model_orders)
        agreement_between_models = pairwise_concordance(list(model_orders[keys[0]]), list(model_orders[keys[1]]))
        if agreement_between_models >= 0.90 and all(value < agreement_floor for value in agreements.values()):
            order = _consensus_order([list(model_orders[key]) for key in keys])
            layout.columns = 1
            layout.gutter = None
            layout.column_bands = []
            layout.separators = []
            layout.method = "model_consensus_override"
            warnings = []
            overridden = True
    confidence = layout.confidence
    if warnings and confidence == "PASS":
        confidence = "CHECK"
    disagreements = {name: value for name, value in agreements.items() if value < agreement_floor}
    if disagreements and confidence == "PASS":
        confidence = "CHECK"
        warnings = warnings + [f"model_order_disagreement:{name}:{value}" for name, value in sorted(disagreements.items())]
    return {
        "order": order,
        "layout": {
            "method": layout.method,
            "columns": layout.columns,
            "gutter_px": layout.gutter,
            "column_bands": layout.column_bands,
            "separators": layout.separators,
            "confidence": confidence,
            "warnings": layout.warnings + warnings,
            "notes": layout.notes,
            "model_agreement": agreements,
            "model_consensus_override": overridden,
        },
    }
