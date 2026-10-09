#!/usr/bin/env python3
"""Collapse overlapping text blocks in finished page results (maintenance pass).

The pipeline consolidates duplicates/containers while it builds a page, but later steps (evidence
containment, crossing-page absorption) can still leave two text blocks over the same pixels, which the
revision validator rejects. This pass applies the same consolidation to the *final* blocks of every
``result.json`` and re-runs the validator, so a finished batch satisfies "one visual line = one block".

Usage:
    python3 scripts/repair_overlapping_blocks.py --document-root <batch> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocr_agent.jsonio import write_json_atomic  # noqa: E402
from ocr_agent.evidence_builder import (  # noqa: E402
    _consolidate_nested_text_blocks,
    _enforce_evidence_containment,
)
from ocr_agent.validators import validate_revision  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    root = Path(args.document_root).expanduser().resolve()
    fixed = inspected = remaining = 0
    for result_path in sorted(root.glob("runs/*/page_*/result.json")):
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        report = raw.get("validation") or {}
        if not report.get("errors"):
            continue
        inspected += 1
        if args.limit and inspected > args.limit:
            break
        sample_id = str(raw["sample_id"])
        page_number = int(raw["page"])
        evidence_path = root / "evidence" / sample_id / f"page_{page_number:04d}.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        # The validator indexes the page evidence, so the page object is the evidence record itself.
        page = {**evidence, "sample_id": sample_id, "page": page_number}
        revision = raw["revision"]
        blocks, _, dropped = _consolidate_nested_text_blocks(
            page, revision["blocks"], revision.get("field_reviews") or {}, []
        )
        coverage = any(error.get("code") == "bbox_does_not_cover_evidence" for error in report.get("errors") or [])
        if not dropped and not coverage:
            remaining += len(report.get("errors") or [])
            continue
        if not dropped:
            blocks = revision["blocks"]
            _enforce_evidence_containment(page, blocks, revision.get("field_reviews") or {})
            for index, block in enumerate(blocks):
                block["reading_order"] = index
            revision["blocks"] = blocks
        revision["blocks"] = blocks
        # A merged block owns its neighbour's references, so its box has to reach them again.
        _enforce_evidence_containment(page, blocks, revision.get("field_reviews") or {})
        for index, block in enumerate(blocks):
            block["reading_order"] = index
        new_report = validate_revision(page, revision)
        if args.dry_run:
            print(f"  [dry-run] {sample_id} p{page_number}: 合并 {len(dropped)} 块 → errors {len(report.get('errors') or [])} → {len(new_report.get('errors') or [])}")
        else:
            raw["revision"] = revision
            raw["validation"] = new_report
            write_json_atomic(result_path, raw)
            print(f"  {sample_id} p{page_number}: 合并 {len(dropped)} 块 → errors {len(report.get('errors') or [])} → {len(new_report.get('errors') or [])}")
        fixed += 1
        remaining += len(new_report.get("errors") or [])
    print(f"处理 {inspected} 页 · 修复 {fixed} 页 · 剩余 errors {remaining}{'（dry-run）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
