#!/usr/bin/env python3
"""Build an inference smoke set: a folder of symlinks into an existing batch.

Nothing is copied: ``runs/`` and ``evidence/`` are symlinked per sample, so the smoke scripts can use
a batch-shaped root while the batch itself stays the single source of truth. Only the outputs of the
smoke run are real files.

Selection is deliberately diverse, because the prompt has to survive all of these:
  * cross-page text continuation (the text event)
  * multi-part tables (the table event)
  * two-column pages (reading order)
  * a longer document (state accumulation over many pages)
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def two_column_pages(document_root: Path, sample: str) -> int:
    """Count pages whose blocks split into two columns (same rule as the annotation pipeline)."""
    try:
        from ocr_agent.reading_order import detect_layout  # type: ignore
    except Exception:
        return 0
    count = 0
    for result_path in sorted((document_root / "runs" / sample).glob("page_*/result.json")):
        result = read_json(result_path)
        blocks = ((result or {}).get("revision") or {}).get("blocks") or []
        if len(blocks) < 4:
            continue
        evidence = read_json(document_root / "evidence" / sample / f"page_{int(result['page']):04d}.json") or {}
        geometry = evidence.get("page_image") or {}
        width, height = float(geometry.get("width") or 0), float(geometry.get("height") or 0)
        if width <= 0 or height <= 0:
            continue
        if detect_layout(blocks, width, height).columns > 1:
            count += 1
    return count


def features(document_root: Path, sample: str) -> dict:
    text_links = table_parts = tables = pages = 0
    for result_path in sorted((document_root / "runs" / sample).glob("page_*/result.json")):
        result = read_json(result_path)
        if not isinstance(result, dict):
            continue
        pages += 1
        for block in ((result.get("revision") or {}).get("blocks") or []):
            if (block.get("cross_page_text") or {}).get("continues_block_id"):
                text_links += 1
            part = block.get("cross_page_table") or {}
            if part.get("part_count") and int(part["part_count"]) > 1:
                table_parts += 1
            if block.get("type") == "table":
                tables += 1
    columns = two_column_pages(document_root, sample)
    return {"pages": pages, "text_links": text_links, "table_parts": table_parts, "tables": tables,
            "two_column_pages": columns}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True, help="source batch, e.g. .../ocr_agent_batch_v10")
    parser.add_argument("--root", type=Path, required=True, help="smoke root to create")
    parser.add_argument("--max-pages", type=int, default=12, help="skip documents longer than this")
    parser.add_argument("--per-group", type=int, default=2, help="documents per selection group")
    parser.add_argument("--force", action="store_true", help="允许在非空目录上重建软链")
    args = parser.parse_args()

    registry = read_json(args.document_root / "v10_documents.json") or {}
    documents = {row["sample_id"]: row for row in registry.get("documents", [])}
    rows = []
    for sample, document in sorted(documents.items()):
        pages = int(document.get("pages") or 0)
        if not (3 <= pages <= args.max_pages):
            continue
        item = dict(document)
        item.update(features(args.document_root, sample))
        rows.append(item)

    def top(key: str, count: int, exclude: set[str]) -> list[dict]:
        pool = [row for row in rows if row["sample_id"] not in exclude and row[key] > 0]
        pool.sort(key=lambda row: (-row[key], row["pages"], row["sample_id"]))
        return pool[:count]

    picked: list[dict] = []
    picked_ids: set[str] = set()
    for group, key in (("cross_page_text", "text_links"), ("cross_page_table", "table_parts"),
                       ("two_column", "two_column_pages"), ("table_heavy", "tables")):
        for row in top(key, args.per_group, picked_ids):
            row["smoke_group"] = group
            picked.append(row)
            picked_ids.add(row["sample_id"])
    longest = sorted([r for r in rows if r["sample_id"] not in picked_ids], key=lambda r: -r["pages"])[:1]
    for row in longest:
        row["smoke_group"] = "long_document"
        picked.append(row)
        picked_ids.add(row["sample_id"])

    if args.root.exists() and any(args.root.iterdir()) and not args.force:
        raise SystemExit(f"{args.root} 已存在且非空；确认要重建请加 --force（不会删除 runs/ 产物）")
    args.root.mkdir(parents=True, exist_ok=True)
    for kind in ("runs", "evidence", "documents"):
        (args.root / kind).mkdir(exist_ok=True)

    linked = []
    for row in picked:
        sample = row["sample_id"]
        for kind in ("runs", "evidence"):
            source = args.document_root / kind / sample
            target = args.root / kind / sample
            if not source.is_dir():
                continue
            if target.is_symlink() or target.exists():
                target.unlink() if target.is_symlink() else None
            os.symlink(source.resolve(), target)
            linked.append(f"{kind}/{sample}")
        document = args.document_root / "documents" / f"{sample}.json"
        if document.is_file():
            target = args.root / "documents" / f"{sample}.json"
            if target.is_symlink() or target.exists():
                target.unlink() if target.is_symlink() else None
            os.symlink(document.resolve(), target)

    smoke_registry = {
        "schema_version": "sft-docparser.smoke-set.v1",
        "source_batch": str(args.document_root),
        "documents": picked,
    }
    (args.root / "registry.json").write_text(json.dumps(smoke_registry, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"链接 {len(linked)} 项 → {args.root}")
    print(f"{'组':<16}{'文档':<22}{'页':>3}{'文本跨页':>8}{'表跨页':>7}{'表':>4}{'双栏页':>6}")
    for row in picked:
        print(f"{row['smoke_group']:<16}{row['sample_id']:<22}{row['pages']:>3}{row['text_links']:>8}"
              f"{row['table_parts']:>7}{row['tables']:>4}{row['two_column_pages']:>6}")


if __name__ == "__main__":
    main()
