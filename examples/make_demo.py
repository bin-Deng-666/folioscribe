"""Generate a tiny, synthetic wiki2gt-shaped input set (no private document data)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw


def create_demo(root: Path) -> Path:
    doc = root / "docs" / "demo" / "three_pages"
    gt = doc / "gt"
    images = doc / "page_images"
    gt.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)

    def write(path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    elements = [
        {"block_type": "heading", "tag": "h1"},
        {"block_type": "image", "fragments": [{"page_idx": 0, "bbox_pt": [10, 40, 60, 80]}]},
        {
            "block_type": "table",
            "continuation": {"pages": [0, 1, 2], "header_repeated": True},
            "fragments": [{"page_idx": p, "bbox_pt": [10, 100, 290, 200]} for p in range(3)],
            "table_structure": {},
        },
    ]
    cells = []
    for r, text in enumerate(["Item", "Alpha", "Beta", "Gamma"]):
        cells.append({"row": r, "col": 0, "text": text, "tag": "th" if r == 0 else "td",
                      "boxes": [{"page_idx": p} for p in ([0, 1, 2] if r == 0 else [r - 1])]})
    elements[2]["table_structure"] = {"cells": cells}
    nodes = [{"node_id": name, "node_type": kind, "source_locator": f"gt_raw.json#elements/{i}"}
             for i, (name, kind) in enumerate([("heading", "heading"), ("picture", "image"), ("table", "table")])]
    entities = [{"node_id": "heading", "page_number": 1, "reading_order": 0, "text": "Demo document"},
                {"node_id": "picture", "page_number": 1, "reading_order": 1, "text": ""}]
    for page in range(1, 4):
        entities.append({"node_id": "table", "page_number": page, "reading_order": 2, "text": ""})
        for row in (0, page):
            entities.append({"node_id": f"table-r{row}c0", "page_number": page,
                             "reading_order": 3 + row, "text": cells[row]["text"]})
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        if page == 1:
            draw.text((20, 20), "Demo document", fill="black")
            draw.rectangle((20, 80, 120, 160), fill="steelblue")
        draw.rectangle((20, 200, 580, 400), outline="black", width=2)
        draw.line((20, 300, 580, 300), fill="black", width=2)
        draw.text((30, 230), "Item", fill="black")
        draw.text((30, 330), cells[page]["text"], fill="black")
        image.save(images / f"page_{page:04d}.png")
    write(gt / "gt_raw.json", {"elements": elements})
    write(gt / "gt_bundle.json", {"a1_entities": entities})
    write(gt / "canonical_doc_graph.json", {"nodes": nodes})
    write(gt / "page_geometry.json", [{"page_number": p, "page_box": [0, 0, 300, 400]} for p in range(1, 4)])
    write(root / "registry.json", {"documents": [{"sample_id": "demo/three_pages", "space": "demo", "bucket": "table"}]})
    # Demo has one training document; it is not an evaluation benchmark.
    write(root / "split.json", {"train": ["demo/three_pages"], "test": []})
    return doc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(create_demo(args.output))
