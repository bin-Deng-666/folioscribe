"""GT 块 -> 投影原语（markdown 原语 + DSL 序列化辅助）。"""
from __future__ import annotations

import re
from typing import Any

from .output_format import LEVEL_MAX, LEVEL_MIN

FURNITURE = {"header", "footer", "page_number"}
PICTURE = {"image", "chart", "seal"}
CAPTION = {"table_caption", "image_caption"}


def image_placeholder(bbox: list[float] | None, page_width: float, page_height: float) -> str:
    if not bbox or len(bbox) != 4 or page_width <= 0 or page_height <= 0:
        return ""
    left, top, right, bottom = [int(round(value / size * 1000)) for value, size in
                                zip(bbox, (page_width, page_height, page_width, page_height))]
    return f'<img src="images/bbox_{left}_{top}_{right}_{bottom}.jpg" />'


def bbox_0_1000(bbox: list[float] | None, page_width: float, page_height: float) -> list[int] | None:
    if not bbox or len(bbox) != 4 or page_width <= 0 or page_height <= 0:
        return None
    return [int(round(value / size * 1000)) for value, size in
            zip(bbox, (page_width, page_height, page_width, page_height))]


def normalize_text(text: str, *, single_line: bool = False) -> str:
    joined = re.sub(r"\s*\n\s*", "\n", str(text or "")).strip()
    joined = re.sub(r"[ \t]{2,}", " ", joined).strip()
    if single_line:
        joined = re.sub(r"\s+", " ", joined)
    return joined


def _is_cjk(char: str) -> bool:
    return bool(char) and ("\u4e00" <= char <= "\u9fff" or "\u3000" <= char <= "\u303f" or "\uff00" <= char <= "\uffef")


def join_text(previous: str, current: str) -> str:
    left, right = previous.rstrip(), current.lstrip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-") and right[:1].isalpha() and right[:1].islower():
        return left[:-1] + right
    if _is_cjk(left[-1]) and _is_cjk(right[0]):
        return left + right
    if left[-1].isalnum() and right[0].isalnum():
        return left + " " + right
    return left + right
