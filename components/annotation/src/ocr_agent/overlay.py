from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

COLORS = {
    "title": "#d62728",
    "text": "#1f77b4",
    "table": "#2ca02c",
    "table_caption": "#8c564b",
    "image": "#9467bd",
    "image_caption": "#bcbd22",
    "chart": "#e377c2",
    "seal": "#d62728",
    "formula": "#e377c2",
    "header": "#8c564b",
    "footer": "#7f7f7f",
    "page_number": "#7f7f7f",
    "footnote": "#bcbd22",
    "unknown": "#dc143c",
}


def render_overlay(page: dict[str, Any], blocks: list[dict[str, Any]], output: str | Path) -> None:
    image_path = Path(page["page_image"]["path"])
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for index, block in enumerate(blocks):
        box = block.get("bbox_image_px")
        if not isinstance(box, list) or len(box) != 4:
            continue
        color = COLORS.get(block.get("type", "unknown"), "#000000")
        width = 4 if block.get("type") == "unknown" else 2
        draw.rectangle(tuple(round(value) for value in box), outline=color, width=width)
        order = block.get("reading_order")
        label = f"{order if order is not None else '-'} {block.get('type', 'unknown')} {block.get('block_id', '')[-4:]}"
        left, top = round(box[0]), max(0, round(box[1]) - 15)
        text_box = draw.textbbox((left, top), label, font=font)
        draw.rectangle(text_box, fill="white")
        draw.text((left, top), label, fill=color, font=font)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, quality=92)
