"""Check parser assets against the actual PDF before building page evidence."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image


def audit_parser_assets(
    pdf: Path, *, full_id: str, short_id: str,
    mineru_root: Path, paddle_root: Path, ovis_root: Path,
) -> dict[str, Any]:
    import pymupdf

    for identifier in (full_id, short_id):
        if not identifier or identifier in {".", ".."} or any(c in identifier for c in "/\\"):
            raise ValueError("document identifiers must be single path components")
    with pymupdf.open(pdf) as document:
        page_count = len(document)
    problems: list[str] = []
    warnings: list[str] = []
    expected_pages = set(range(1, page_count + 1))
    dimensions: dict[int, tuple[int, int]] = {}

    def read_json(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"invalid_json:{path}:{type(exc).__name__}")
            return None

    def check_names(folder: Path, suffix: str) -> None:
        expected = {f"page_{p:04d}{suffix}" for p in expected_pages}
        actual = {p.name for p in folder.glob(f"page_*{suffix}")}
        for name in sorted(expected - actual):
            problems.append(f"missing_page:{folder / name}")
        for name in sorted(actual - expected):
            problems.append(f"unexpected_page:{folder / name}")

    for root in (paddle_root, ovis_root):
        folder = root / "images" / short_id
        check_names(folder, ".png")
        for page in expected_pages:
            path = folder / f"page_{page:04d}.png"
            if not path.is_file():
                continue
            try:
                with Image.open(path) as image:
                    size = image.size
                    image.verify()
                if root == paddle_root:
                    dimensions[page] = size
                elif page in dimensions and size != dimensions[page]:
                    problems.append(f"image_size_mismatch:page={page}:paddle={dimensions[page]}:ovis={size}")
            except (OSError, ValueError) as exc:
                problems.append(f"invalid_image:{path}:{type(exc).__name__}")

    def valid_box(box: Any, width: float, height: float) -> bool:
        return (isinstance(box, list) and len(box) == 4
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)

    paddle_json = paddle_root / "json" / short_id
    check_names(paddle_json, "_res.json")
    for page in expected_pages:
        path = paddle_json / f"page_{page:04d}_res.json"
        if not path.is_file():
            continue
        data = read_json(path)
        blocks = data.get("parsing_res_list") if isinstance(data, dict) else None
        if not isinstance(blocks, list):
            problems.append(f"invalid_paddle_structure:page={page}")
            continue
        if page in dimensions and (data.get("width"), data.get("height")) != dimensions[page]:
            problems.append(f"paddle_dimensions_mismatch:page={page}")
        if not blocks:
            warnings.append(f"empty_paddle_page:{page}")
        for index, block in enumerate(blocks):
            if not isinstance(block, dict) or "block_label" not in block:
                problems.append(f"invalid_paddle_block:page={page}:block={index}")
            elif page in dimensions and not valid_box(block.get("block_bbox"), *dimensions[page]):
                problems.append(f"invalid_paddle_bbox:page={page}:block={index}")

    ovis_md = ovis_root / "markdown" / short_id
    check_names(ovis_md, ".md")
    for page in expected_pages:
        path = ovis_md / f"page_{page:04d}.md"
        if path.is_file():
            try:
                if not path.read_text(encoding="utf-8").strip():
                    warnings.append(f"empty_ovis_page:{page}")
            except (OSError, UnicodeError):
                problems.append(f"invalid_ovis_markdown:page={page}")

    mineru_path = mineru_root / full_id / "extracted" / full_id / "vlm" / f"{full_id}_content_list.json"
    mineru = read_json(mineru_path)
    if not isinstance(mineru, list):
        problems.append("invalid_mineru_content_list")
    else:
        present = set()
        for index, block in enumerate(mineru):
            if not isinstance(block, dict):
                problems.append(f"invalid_mineru_block:{index}")
                continue
            page = block.get("page_idx")
            if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page < page_count:
                problems.append(f"invalid_mineru_page:block={index}")
            else:
                present.add(page + 1)
            if not valid_box(block.get("bbox"), 1000, 1000):
                problems.append(f"invalid_mineru_bbox:block={index}")
        for page in sorted(expected_pages - present):
            warnings.append(f"no_mineru_blocks:page={page}")

    digest = hashlib.sha256()
    with pdf.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    pdf_sha256 = digest.hexdigest()
    for name, root in (("paddle", paddle_root), ("ovis", ovis_root)):
        manifest = root / f"{short_id}.manifest.json"
        if manifest.is_file():
            data = read_json(manifest)
            if not isinstance(data, dict) or data.get("source_sha256") != pdf_sha256:
                problems.append(f"source_pdf_mismatch:{name}")
            elif data.get("pages_completed") != list(range(1, page_count + 1)):
                problems.append(f"incomplete_manifest:{name}")
        else:
            warnings.append(f"source_hash_unavailable:{name}")

    return {"pdf": str(pdf.resolve()), "pdf_sha256": pdf_sha256, "page_count": page_count,
            "full_id": full_id, "short_id": short_id,
            "ok": not problems, "problems": problems, "warnings": warnings}
