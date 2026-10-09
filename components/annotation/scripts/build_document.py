#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ocr_agent.anydoc_reader import read_anydoc
from ocr_agent.overlay import render_overlay
from ocr_agent.evidence_builder import build_document_evidence
from ocr_agent.jsonio import read_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--full-id", required=True)
    parser.add_argument("--short-id", required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--mineru-root", type=Path, required=True)
    parser.add_argument("--paddle-root", type=Path, required=True)
    parser.add_argument("--ovis-root", type=Path, required=True)
    parser.add_argument("--pages", default="1,2,3,7,12")
    parser.add_argument("--all-pages", action="store_true")
    parser.add_argument("--source-docx", type=Path, help="optional bound native DOCX used as document-structure evidence")
    parser.add_argument("--anydoc-docx", type=Path, help="optional source DOCX for document outline")
    parser.add_argument("--anydoc-outline", type=Path, help="optional materialized AnyDoc outline text")
    args = parser.parse_args()
    output = args.output
    anydoc_markdown = ""
    if args.anydoc_docx:
        anydoc_markdown = read_anydoc(args.anydoc_docx)
    elif args.anydoc_outline:
        anydoc_markdown = args.anydoc_outline.read_text(encoding="utf-8")
    pages = None if args.all_pages else tuple(int(value) for value in args.pages.split(",") if value.strip())
    try:
        paths = build_document_evidence(
            output,
            sample_id=args.sample_id,
            full_id=args.full_id,
            short_id=args.short_id,
            pages=pages,
            anydoc_markdown=anydoc_markdown,
            source_pdf=args.pdf,
            mineru_root=args.mineru_root,
            paddle_root=args.paddle_root,
            ovis_root=args.ovis_root,
            source_docx=args.source_docx,
        )
    except ValueError as exc:
        message = str(exc)
        if args.source_docx and (message.startswith("source DOCX does not bind") or message.startswith("Word table fragment coverage failed")):
            print(message, file=sys.stderr)
            raise SystemExit(3) from exc
        raise
    for path in paths:
        page = read_json(path)
        overlay = output / "overlays" / page["sample_id"] / f"page_{page['page']:04d}_candidate.png"
        render_overlay(page, page["current_candidate"]["blocks"], overlay)
        print(path)
    print(f"prepared={len(paths)} root={output}")


if __name__ == "__main__":
    main()
