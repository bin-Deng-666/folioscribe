#!/usr/bin/env python3
"""Inter-annotator agreement for the blind overlap documents.

Some documents are annotated twice on purpose (the overlap set in ``workload/assignments.json``), which
is the only honest way to measure how well the annotation guideline is being applied. This script reads
the merged per-annotator patches from the parent batch and reports, per overlap document:

* block matching by id, falling back to box overlap, so a deleted/added box shows up as a difference
* per field: type, text (normalised + character similarity), title level, caption parent, cross-page
  link, and bbox IoU
* a document/page summary and the list of disagreements a reviewer has to settle

With ``--apply`` it also materialises ``annotations/<sample>/consensus/page_XXXX.json`` for the pages
that agree at or above the thresholds (a copy of the agreed revision). Export then prefers the
consensus file, which is what makes the Contract GT for an overlap document unambiguous.

    python3 scripts/report_overlap_agreement.py \
        --document-root .../ocr_agent_batch_v10 \
        --workload .../ocr_agent_batch_v10/workload/assignments.json \
        [--apply --text-threshold 0.98 --iou-threshold 0.90]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

PUNCT = re.compile(r"[\s\u3000]+")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def normalise(value: str) -> str:
    return PUNCT.sub("", str(value or "")).casefold()


def similarity(first: str, second: str) -> float:
    first, second = normalise(first), normalise(second)
    if first == second:
        return 1.0
    if not first or not second:
        return 0.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def iou(first: list[float], second: list[float]) -> float:
    if not (isinstance(first, list) and isinstance(second, list) and len(first) == 4 and len(second) == 4):
        return 0.0
    x1, y1 = max(first[0], second[0]), max(first[1], second[1])
    x2, y2 = min(first[2], second[2]), min(first[3], second[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_first = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    area_second = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = area_first + area_second - inter
    return inter / union if union > 0 else 0.0


def match_blocks(left: list[dict], right: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pair blocks by id first, then by best box overlap; leftovers are added/removed blocks."""
    used: set[int] = set()
    pairs: list[tuple[dict, dict]] = []
    by_id = {entry.get("block_id"): index for index, entry in enumerate(right)}
    for block in left:
        index = by_id.get(block.get("block_id"))
        if index is not None and index not in used:
            used.add(index)
            pairs.append((block, right[index]))
    for block in left:
        if any(block is pair[0] for pair in pairs):
            continue
        best = None
        for index, other in enumerate(right):
            if index in used:
                continue
            score = iou(block.get("bbox_image_px"), other.get("bbox_image_px"))
            if score >= 0.5 and (best is None or score > best[0]):
                best = (score, index)
        if best is not None:
            used.add(best[1])
            pairs.append((block, right[best[1]]))
    extra_left = [block for block in left if not any(block is pair[0] for pair in pairs)]
    extra_right = [entry for index, entry in enumerate(right) if index not in used]
    return pairs, extra_left, extra_right


def compare_page(left_blocks: list[dict], right_blocks: list[dict]) -> dict:
    pairs, removed, added = match_blocks(left_blocks, right_blocks)
    fields: Counter = Counter()
    agreed: Counter = Counter()
    disagreements: list[dict] = []
    for first, second in pairs:
        checks = {
            "type": str(first.get("type")) == str(second.get("type")),
            "text": similarity(str(first.get("text") or ""), str(second.get("text") or "")) >= 0.98,
            "level": (first.get("level") or 0) == (second.get("level") or 0),
            "parent_id": (first.get("parent_id") or "") == (second.get("parent_id") or ""),
            "cross_page": json.dumps(first.get("cross_page_text") or first.get("cross_page_table") or {}, sort_keys=True)
                          == json.dumps(second.get("cross_page_text") or second.get("cross_page_table") or {}, sort_keys=True),
            "bbox": iou(first.get("bbox_image_px"), second.get("bbox_image_px")) >= 0.90,
        }
        for field, ok in checks.items():
            fields[field] += 1
            if ok:
                agreed[field] += 1
            else:
                detail = {
                    "field": field,
                    "block_id": first.get("block_id"),
                    "left": str(first.get(field) or first.get("text") or "")[:80],
                    "right": str(second.get(field) or second.get("text") or "")[:80],
                }
                if field == "bbox":
                    detail["iou"] = round(iou(first.get("bbox_image_px"), second.get("bbox_image_px")), 3)
                if field == "text":
                    detail["similarity"] = round(similarity(str(first.get("text") or ""), str(second.get("text") or "")), 3)
                disagreements.append(detail)
    for block in removed:
        disagreements.append({"field": "block_removed_by_second", "block_id": block.get("block_id")})
    for block in added:
        disagreements.append({"field": "block_added_by_second", "block_id": block.get("block_id")})
    return {
        "blocks_left": len(left_blocks),
        "blocks_right": len(right_blocks),
        "matched": len(pairs),
        "fields": {field: {"compared": count, "agreed": agreed[field], "rate": round(agreed[field] / count, 3)} for field, count in fields.items()},
        "disagreements": disagreements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True)
    parser.add_argument("--workload", type=Path, help="defaults to <document-root>/workload/assignments.json")
    parser.add_argument("--out", type=Path, help="defaults to <document-root>/overlap_agreement.json")
    parser.add_argument("--apply", action="store_true", help="write consensus patches for pages that agree")
    parser.add_argument("--text-threshold", type=float, default=0.98)
    parser.add_argument("--iou-threshold", type=float, default=0.90)
    args = parser.parse_args()

    workload_path = args.workload or args.document_root / "workload" / "assignments.json"
    plan = read_json(workload_path) or {}
    overlap = [item["sample_id"] for item in plan.get("overlap") or []]
    if not overlap:
        raise SystemExit(f"no overlap documents in {workload_path}")

    annotations = args.document_root / "annotations"
    documents: list[dict] = []
    agreement_totals: Counter = Counter()
    agreement_agreed: Counter = Counter()
    consensus_written = 0
    for sample in overlap:
        sample_dir = annotations / sample
        annotators = sorted(path.name for path in sample_dir.iterdir() if path.is_dir()) if sample_dir.is_dir() else []
        if len(annotators) < 2:
            documents.append({"sample_id": sample, "status": "waiting", "annotators": annotators, "pages": 0})
            continue
        first, second = annotators[0], annotators[1]
        pages = sorted({path.name for path in (sample_dir / first).glob("page_*.json")} & {path.name for path in (sample_dir / second).glob("page_*.json")})
        page_reports = []
        for name in pages:
            left = (read_json(sample_dir / first / name) or {}).get("blocks") or []
            right = (read_json(sample_dir / second / name) or {}).get("blocks") or []
            report = compare_page(left, right)
            report["page"] = int(name.split("_")[-1].split(".")[0])
            page_reports.append(report)
            for field, stats in report["fields"].items():
                agreement_totals[field] += stats["compared"]
                agreement_agreed[field] += stats["agreed"]
            if args.apply:
                text_rate = report["fields"].get("text", {}).get("rate", 1.0)
                bbox_rate = report["fields"].get("bbox", {}).get("rate", 1.0)
                clean = not report["disagreements"] and text_rate >= args.text_threshold and bbox_rate >= args.iou_threshold
                if clean:
                    target = sample_dir / "consensus" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sample_dir / first / name, target)
                    consensus_written += 1
        documents.append({
            "sample_id": sample,
            "status": "compared",
            "annotators": annotators,
            "pages": page_reports,
            "disagreements": sum(len(page["disagreements"]) for page in page_reports),
        })

    report = {
        "schema_version": "ocr-agent.overlap-agreement.v1",
        "document_root": str(args.document_root),
        "workload": str(workload_path),
        "overlap_documents": len(overlap),
        "compared_documents": sum(1 for entry in documents if entry["status"] == "compared"),
        "waiting_documents": sum(1 for entry in documents if entry["status"] == "waiting"),
        "field_agreement": {field: {"compared": count, "agreed": agreement_agreed[field], "rate": round(agreement_agreed[field] / count, 3)} for field, count in agreement_totals.items()},
        "consensus_written": consensus_written,
        "documents": documents,
    }
    target = args.out or args.document_root / "overlap_agreement.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("overlap_documents", "compared_documents", "waiting_documents", "field_agreement", "consensus_written")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
