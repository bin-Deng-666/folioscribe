from __future__ import annotations

import difflib
import html
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag in {W + "br", W + "cr"}:
            parts.append("\n")
        elif node.tag == W + "tab":
            parts.append("\t")
    return "".join(parts).strip()


def _positive_int(element: ET.Element | None, default: int = 1) -> int:
    if element is None:
        return default
    try:
        value = int(element.get(W + "val", str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _parse_table(table: ET.Element, index: int) -> dict:
    rows = table.findall(W + "tr")
    grid_cols = len(table.findall(f"{W}tblGrid/{W}gridCol"))
    cells: list[dict] = []
    active_merge: dict[int, dict] = {}
    max_col = 0
    for row_index, row in enumerate(rows):
        col = 0
        for cell in row.findall(W + "tc"):
            props = cell.find(W + "tcPr")
            colspan = _positive_int(props.find(W + "gridSpan") if props is not None else None)
            merge = props.find(W + "vMerge") if props is not None else None
            merge_value = merge.get(W + "val", "continue") if merge is not None else None
            value = "\n".join(filter(None, (_text(paragraph) for paragraph in cell.findall(W + "p"))))
            if merge is not None and merge_value != "restart" and col in active_merge:
                active_merge[col]["rowspan"] += 1
                if value:
                    active_merge[col].setdefault("continuation_text", []).append(value)
                col += colspan
                max_col = max(max_col, col)
                continue
            item = {"row": row_index, "col": col, "rowspan": 1, "colspan": colspan, "text": value}
            cells.append(item)
            if merge is not None:
                active_merge[col] = item
            else:
                for covered in range(col, col + colspan):
                    active_merge.pop(covered, None)
            col += colspan
            max_col = max(max_col, col)
    row_count = len(rows)
    col_count = max(grid_cols, max_col)
    by_row: dict[int, list[dict]] = {}
    for cell in cells:
        by_row.setdefault(cell["row"], []).append(cell)
    html_rows = []
    for row in range(row_count):
        html_cells = []
        for cell in sorted(by_row.get(row, []), key=lambda item: item["col"]):
            attrs = ""
            if cell["rowspan"] > 1:
                attrs += f' rowspan="{cell["rowspan"]}"'
            if cell["colspan"] > 1:
                attrs += f' colspan="{cell["colspan"]}"'
            value = html.escape(cell["text"]).replace("\n", "<br>")
            html_cells.append(f"<td{attrs}>{value}</td>")
        html_rows.append("<tr>" + "".join(html_cells) + "</tr>")
    table_text = "\n".join(cell["text"] for cell in cells if cell["text"])
    return {
        "evidence_id": f"word.table.{index:04d}",
        "kind": "word_native_table",
        "table_index": index,
        "rows": row_count,
        "cols": col_count,
        "cells": cells,
        "text": table_text,
        "normalized_text": normalize(table_text),
        "html": "<table>" + "".join(html_rows) + "</table>",
        "required_coverage": False,
    }


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).casefold()


def match_table_rows(table: dict, fragment_text: str, *, minimum_score: float = 0.70) -> dict | None:
    fragment = normalize(fragment_text)
    if not fragment:
        return None
    rows = [normalize("".join(cell["text"] for cell in table["cells"] if cell["row"] == row)) for row in range(table["rows"])]
    best: tuple[float, int, int] | None = None
    for start in range(len(rows)):
        joined = ""
        for end in range(start + 1, len(rows) + 1):
            joined += rows[end - 1]
            matcher = difflib.SequenceMatcher(None, fragment, joined)
            matched = sum(match.size for match in matcher.get_matching_blocks())
            coverage = matched / max(1, len(fragment))
            precision = matched / max(1, len(joined))
            score = 0.65 * coverage + 0.35 * precision
            candidate = (score, start, end)
            if best is None or candidate > best:
                best = candidate
            if len(joined) > len(fragment) * 1.5 + 50:
                break
    if best is None or best[0] < minimum_score:
        return None
    return {"row_start": best[1], "row_end": best[2], "score": round(best[0], 4)}


def table_fragment_html(table: dict, row_start: int, row_end: int) -> str:
    by_row: dict[int, list[dict]] = {}
    for cell in table["cells"]:
        cell_end = cell["row"] + cell["rowspan"]
        if cell_end <= row_start or cell["row"] >= row_end:
            continue
        physical_row = max(cell["row"], row_start)
        clipped = dict(cell)
        clipped["rowspan"] = min(cell_end, row_end) - physical_row
        if cell["row"] < row_start:
            clipped["text"] = ""
        by_row.setdefault(physical_row, []).append(clipped)
    rows = []
    for row in range(row_start, row_end):
        cells = []
        for cell in sorted(by_row.get(row, []), key=lambda item: item["col"]):
            attrs = ""
            if cell["rowspan"] > 1:
                attrs += f' rowspan="{cell["rowspan"]}"'
            if cell["colspan"] > 1:
                attrs += f' colspan="{cell["colspan"]}"'
            value = html.escape(cell["text"]).replace("\n", "<br>")
            cells.append(f"<td{attrs}>{value}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def read_word_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(W + "body")
    return _text(body) if body is not None else ""


def read_word_tables(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(W + "body")
    if body is None:
        return []
    return [_parse_table(child, index) for index, child in enumerate(item for item in body if item.tag == W + "tbl")]
