"""Measure whether a corpus PDF actually has a *usable* text layer.

The corpus gate in ``ocr_agent.corpus_selection`` reads ``text_layer_coverage`` from the upstream
perceptual profile, which counts any extracted character - including the control codes and
private-use glyphs that old journal typesetting (and CNKI scans with a bad OCR layer) produce. A
document like that passes the gate yet carries no text the pipeline can anchor to, so its pages end
up CHECK-only or, worse, its correct model reading gets overwritten by font noise.

This audit re-measures the text layer from the PDF itself and emits one record per document:

* ``junk_pages``  - pages where >3% of the extracted characters are glyph codes
* ``clean_ratio`` - share of characters that are letters/digits/punctuation
* ``chars_per_page`` - clean characters per page
* ``image_coverage`` - share of the page area covered by embedded pictures
* ``verdict`` - ``ok`` | ``glyph_noise`` | ``image_only``

Run it, then feed the ndjson to the selection script with ``--pdf-text-audit``.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import fitz

GLYPH_NOISE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ue000-\uf8ff\ufffd]")
JUNK_PAGE_RATIO = 0.03
CLEAN_CHAR_MIN = 120.0
IMAGE_COVERAGE_MIN = 0.60


def _clean_stats(text: str) -> tuple[float, int]:
    if not text:
        return 1.0, 0
    clean = GLYPH_NOISE.sub("", text)
    good = sum(1 for character in clean if character.isalnum() or unicodedata.category(character)[0] in {"P", "L", "Z"})
    return (good / len(text) if text else 1.0), good


def audit_pdf(pdf: Path, max_pages: int) -> dict[str, Any]:
    document = fitz.open(pdf)
    try:
        pages = min(document.page_count, max_pages)
        junk = 0
        clean_chars = 0
        ratio_sum = 0.0
        image_area = 0.0
        for index in range(pages):
            page = document[index]
            text = page.get_text("text") or ""
            ratio, good = _clean_stats(text)
            clean_chars += good
            ratio_sum += ratio
            if text and (len(text) - len(GLYPH_NOISE.sub("", text))) / len(text) > JUNK_PAGE_RATIO:
                junk += 1
            area = abs(page.rect.width * page.rect.height) or 1.0
            covered = 0.0
            for image in page.get_images(full=True):
                try:
                    for rect in page.get_image_rects(image[0]):
                        covered += abs(rect.width * rect.height)
                except Exception:  # pragma: no cover - defensive: broken xref
                    continue
            image_area += min(1.0, covered / area)
        pages = max(1, pages)
        return {
            "page_count": document.page_count,
            "pages_audited": pages,
            "junk_pages": junk,
            "chars_per_page": round(clean_chars / pages, 1),
            "image_coverage": round(image_area / pages, 3),
            "clean_ratio": round(ratio_sum / pages, 4),
        }
    finally:
        document.close()


def verdict_for(record: dict[str, Any]) -> str:
    pages = max(1, record["pages_audited"])
    if record["junk_pages"] / pages >= 0.5 or (record["junk_pages"] and record["chars_per_page"] < CLEAN_CHAR_MIN):
        return "glyph_noise"
    if record["chars_per_page"] < CLEAN_CHAR_MIN and record["image_coverage"] >= IMAGE_COVERAGE_MIN:
        return "image_only"
    return "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True, help="selection JSON whose documents carry assets.pdf")
    parser.add_argument("--out", type=Path, required=True, help="ndjson audit output")
    parser.add_argument("--max-pages", type=int, default=6, help="pages sampled per document (default 6)")
    parser.add_argument("--resume", action="store_true", help="skip samples already present in --out")
    args = parser.parse_args()

    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    done: set[str] = set()
    if args.resume and args.out.is_file():
        with args.out.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    done.add(json.loads(line)["sample_id"])
                except Exception:
                    continue
    args.out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with args.out.open("a", encoding="utf-8") as out:
        for document in selection["documents"]:
            sample = document["sample_id"]
            if sample in done:
                continue
            pdf = Path(str((document.get("assets") or {}).get("pdf") or ""))
            record: dict[str, Any] = {"sample_id": sample, "sha16": document.get("sha16"), "pdf": str(pdf)}
            if not pdf.is_file():
                record.update({"verdict": "missing_pdf", "pages_audited": 0, "junk_pages": 0, "chars_per_page": 0.0, "image_coverage": 0.0, "page_count": 0})
            else:
                try:
                    record.update(audit_pdf(pdf, args.max_pages))
                    record["verdict"] = verdict_for(record)
                except Exception as error:  # pragma: no cover - defensive: unreadable PDF
                    record.update({"verdict": "unreadable", "error": str(error)[:200], "pages_audited": 0, "junk_pages": 0, "chars_per_page": 0.0, "image_coverage": 0.0, "page_count": 0})
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            total += 1
            if total % 200 == 0:
                print(f"audited {total}", flush=True)
    print(f"done: {total} documents -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
