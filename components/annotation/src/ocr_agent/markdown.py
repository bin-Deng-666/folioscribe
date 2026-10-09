from __future__ import annotations

from typing import Any

from .constants import MAX_TITLE_LEVEL


def blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in sorted(blocks, key=lambda item: item.get("reading_order", 0)):
        block_type = block.get("type")
        text = str(block.get("text", "")).strip()
        if block_type == "title" and text:
            level = min(MAX_TITLE_LEVEL, max(1, int(block.get("level", 1))))
            parts.append(f"{'#' * level} {text}")
        elif block_type == "table" and block.get("html"):
            parts.append(str(block["html"]).strip())
        elif text:
            parts.append(text)
    return "\n\n".join(parts).strip() + "\n"
