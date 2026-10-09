#!/usr/bin/env python3
"""Audit reading order across a batch: structure evidence, confidence, and leftover interleaving.

Reads the page evidence written by the pipeline (``reading_order`` on each page result) plus the
revision itself, and re-runs the column audit on every page, so a page whose order was produced by an
older build (or edited by hand) is still judged by the same rule.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ocr_agent.reading_order import audit_order, detect_layout  # noqa: E402


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    registry = args.registry or args.document_root / "v10_documents.json"
    documents = (read_json(registry) or {}).get("documents") or []

    pages = columns = interleaved = checked = 0
    confidence = Counter()
    warnings = Counter()
    offenders: list[dict] = []
    agreement: list[float] = []
    for document in documents:
        sample = document["sample_id"]
        for result_path in sorted((args.document_root / "runs" / sample).glob("page_*/result.json")):
            result = read_json(result_path)
            if not isinstance(result, dict):
                continue
            blocks = (result.get("revision") or {}).get("blocks") or []
            if len(blocks) < 4:
                continue
            evidence_path = args.document_root / "evidence" / sample / f"page_{int(result['page']):04d}.json"
            geometry = read_json(evidence_path) or {}
            width = float((geometry.get("page_image") or {}).get("width") or 0)
            height = float((geometry.get("page_image") or {}).get("height") or 0)
            if width <= 0 or height <= 0:
                continue
            pages += 1
            layout = detect_layout(blocks, width, height)
            if layout.columns > 1:
                columns += 1
            stale = audit_order(blocks, [block["block_id"] for block in blocks], layout, width)
            if stale:
                interleaved += 1
                offenders.append({"sample_id": sample, "page": int(result["page"]), "warnings": stale})
            # The page evidence is the authoritative place for the measured structure (result.json
            # carries a copy for convenience).
            record = (geometry or {}).get("reading_order")
            if not isinstance(record, dict):
                record = result.get("reading_order")
            if isinstance(record, dict):
                checked += 1
                confidence[str(record.get("confidence"))] += 1
                for warning in record.get("warnings") or []:
                    warnings[str(warning).split(":")[0]] += 1
                for value in (record.get("model_agreement") or {}).values():
                    if isinstance(value, (int, float)):
                        agreement.append(float(value))
    report = {
        "schema_version": "ocr-agent.reading-order-audit.v1",
        "document_root": str(args.document_root),
        "pages": pages,
        "column_pages": columns,
        "pages_with_order_evidence": checked,
        "confidence": dict(confidence),
        "warnings": dict(warnings.most_common()),
        "mean_model_agreement": round(sum(agreement) / len(agreement), 3) if agreement else None,
        "pages_still_interleaved": interleaved,
        "offenders": offenders[:50],
    }
    target = args.out or args.document_root / "reading_order_audit.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("pages", "column_pages", "pages_with_order_evidence", "confidence", "warnings", "mean_model_agreement", "pages_still_interleaved")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
