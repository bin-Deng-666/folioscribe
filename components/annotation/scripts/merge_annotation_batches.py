#!/usr/bin/env python3
"""Collect the per-annotator working batches back into the parent batch.

Annotation happens in physically separate batches (one per annotator, ``标注_batchN``) so nobody can
overwrite anybody else. Export and quality reporting happen on the parent batch, which needs all the
patches back in one place:

    annotations/<sample>/<annotator>/page_XXXX.json

Usage::

    python3 scripts/merge_annotation_batches.py \
        --parts-root data/annotation-batches \
        --prefix 标注_batch \
        --parent .../ocr_agent_batch_v10 \
        [--dry-run]

Documents annotated in two parts (the blind overlap set) end up with two annotator directories, which
is exactly what the agreement report and the consensus step consume.
"""
from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def part_directories(parts_root: Path, prefix: str) -> list[Path]:
    return sorted(path for path in parts_root.glob(f"{prefix}*") if (path / "annotations").is_dir())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts-root", type=Path, required=True, help="directory holding the 标注_batchN parts")
    parser.add_argument("--prefix", default="标注_batch")
    parser.add_argument("--parent", type=Path, required=True, help="batch that receives the merged annotations")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    parts = part_directories(args.parts_root, args.prefix)
    if not parts:
        raise SystemExit(f"no parts found under {args.parts_root} matching {args.prefix}*")

    merged = 0
    copied: Counter = Counter()
    overlap: dict[str, list[str]] = {}
    for part in parts:
        annotator = part.name
        for sample_dir in sorted((part / "annotations").iterdir()):
            if not sample_dir.is_dir():
                continue
            sample = sample_dir.name
            if (sample_dir / "document.json").is_file():
                copied[f"{part.name}:document"] += 1
            pages = sorted(sample_dir.glob("page_*.json"))
            if not pages:
                continue
            overlap.setdefault(sample, []).append(annotator)
            for page in pages:
                payload = read_json(page)
                if not isinstance(payload, dict) or not payload.get("blocks"):
                    continue
                target = args.parent / "annotations" / sample / annotator / page.name
                if args.dry_run:
                    merged += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(page, target)
                merged += 1
                copied[part.name] += 1
            document = sample_dir / "document.json"
            if document.is_file() and not args.dry_run:
                target = args.parent / "annotations" / sample / annotator / "document.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(document, target)
            flags = sample_dir / "flags"
            if flags.is_dir() and not args.dry_run:
                target_dir = args.parent / "annotations" / sample / annotator / "flags"
                target_dir.mkdir(parents=True, exist_ok=True)
                for flag in sorted(flags.glob("page_*.json")):
                    shutil.copy2(flag, target_dir / flag.name)
                copied[f"{part.name}:flags"] += len(list(flags.glob("page_*.json")))

    # A sample present in two parts is the blind QC set: report it (admin side only).
    double = {sample: annotators for sample, annotators in overlap.items() if len(annotators) > 1}
    report = {
        "schema_version": "ocr-agent.annotation-merge.v1",
        "parts_root": str(args.parts_root),
        "parent": str(args.parent),
        "parts": [part.name for part in parts],
        "pages_merged": merged,
        "per_part": {key: value for key, value in sorted(copied.items())},
        "double_annotated_documents": double,
        "dry_run": bool(args.dry_run),
    }
    target = args.parent / "annotation_merge_report.json"
    if not args.dry_run:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("parts", "pages_merged", "double_annotated_documents", "dry_run")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
