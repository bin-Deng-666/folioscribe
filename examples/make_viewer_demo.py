"""Create synthetic OCR-Agent runs for trying the full annotation viewer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw


def create_demo(root: Path) -> Path:
    for sample in ("demo_guide", "demo_notes"):
        for page in (1, 2, 3):
            page_dir = root / "runs" / sample / f"page_{page:04d}"
            evidence_dir = root / "evidence" / sample
            page_dir.mkdir(parents=True, exist_ok=True)
            evidence_dir.mkdir(parents=True, exist_ok=True)
            image_path = page_dir / "page.png"
            title = f"Sample guide - page {page}"
            text = f"Review this paragraph on page {page}. Select a box to edit its text."
            image = Image.new("RGB", (800, 1100), "white")
            draw = ImageDraw.Draw(image)
            draw.text((60, 65), title, fill="black", font_size=30)
            draw.text((60, 180), text, fill="black", font_size=18)
            draw.rectangle((60, 300, 740, 500), outline="black", width=2)
            draw.line((60, 370, 740, 370), fill="black", width=2)
            draw.text((80, 325), "Item                         Value", fill="black", font_size=20)
            draw.text((80, 405), f"Page                         {page}", fill="black", font_size=20)
            draw.text((380, 1040), str(page), fill="gray", font_size=18)
            image.save(image_path)
            blocks = [
                {"block_id": f"demo.p{page}.0001", "type": "title", "level": 1,
                 "text": title, "bbox_image_px": [55, 55, 700, 115], "reading_order": 0},
                {"block_id": f"demo.p{page}.0002", "type": "text", "text": text,
                 "bbox_image_px": [55, 170, 750, 210], "reading_order": 1},
                {"block_id": f"demo.p{page}.0003", "type": "table",
                 "html": f"<table><tr><th>Item</th><th>Value</th></tr><tr><td>Page</td><td>{page}</td></tr></table>",
                 "bbox_image_px": [60, 300, 740, 500], "reading_order": 2},
            ]
            evidence = {"sample_id": sample, "page": page,
                        "page_image": {"path": str(image_path.resolve()), "width": 800, "height": 1100},
                        "pdf_page": {"width_pt": 400, "height_pt": 550},
                        "evidence": {key: [] for key in ("pdf", "mineru", "paddle", "ovis", "anydoc", "tools")}}
            result = {"sample_id": sample, "page": page, "status": "CHECK", "task_count": 0,
                      "revision": {"blocks": blocks, "field_reviews": {}, "review_items": []},
                      "validation": {"error_count": 0, "review_count": 0, "unresolved_field_count": 0}}
            for path, value in [(evidence_dir / f"page_{page:04d}.json", evidence), (page_dir / "result.json", result)]:
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(create_demo(parser.parse_args().output))
