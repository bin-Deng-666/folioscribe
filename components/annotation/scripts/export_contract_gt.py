#!/usr/bin/env python3
"""Export the DocFusion Contract GT projection of OCR-Agent document runs.

The projection renames OCR-Agent facts into the registered contract shapes (A1/A2/A3, canonical
document graph, page geometry, GT truth, full document bundle). The bundle is the registered
delivery contract; the A1/A2/A3 files are written under ``a_facts/`` with an OCR-Agent fact schema
version because the registered A-file schema pins the render line's provenance.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from ocr_agent.contract_gt import export_contract_gt, schema_root_default  # noqa: E402


def discover(document_root: Path) -> list[str]:
    runs = document_root / "runs"
    if not runs.is_dir():
        return []
    return sorted(path.name for path in runs.iterdir() if path.is_dir())


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Contract GT (A1/A2/A3 + bundle) from OCR-Agent runs")
    parser.add_argument("--document-root", type=Path, required=True, help="OCR-Agent output root (single document or batch)")
    parser.add_argument("--sample-ids", help="comma separated sample ids; defaults to every sample in runs/")
    parser.add_argument("--schema-root", type=Path, help="directory holding the generated JSON Schemas (for validation)")
    args = parser.parse_args()

    samples = [item.strip() for item in args.sample_ids.split(",") if item.strip()] if args.sample_ids else discover(args.document_root)
    if not samples:
        raise SystemExit(f"no runs found under {args.document_root}")

    rows = []
    failures = 0
    for sample_id in samples:
        try:
            report = export_contract_gt(args.document_root, sample_id, schema_root=args.schema_root or schema_root_default())
        except Exception as exc:
            failures += 1
            print(json.dumps({"sample_id": sample_id, "status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
            continue
        rows.append(report)
        print(json.dumps({
            "sample_id": sample_id,
            "status": "ok",
            "counts": report["counts"],
            "registered_valid": report["validation"].get("validated"),
        }, ensure_ascii=False), flush=True)

    summary = {
        "schema_version": "ocr-agent.contract-gt-export-summary.v1",
        "document_root": str(args.document_root),
        "documents": len(rows),
        "failures": failures,
        "registered_valid": sum(1 for row in rows if row["validation"].get("validated")),
        "totals": {
            "pages": sum(row["counts"]["pages"] for row in rows),
            "entities": sum(row["counts"]["entities"] for row in rows),
            "headings": sum(row["counts"]["headings"] for row in rows),
            "table_continuations": sum(row["counts"]["table_continuations"] for row in rows),
        },
    }
    (args.document_root / "contract_gt_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
