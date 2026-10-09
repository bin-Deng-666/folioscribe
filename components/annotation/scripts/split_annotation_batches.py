#!/usr/bin/env python3
"""Split an annotation batch into one physical working batch per annotator.

Several people annotate the same corpus, so the work is split into real, self-contained batches. Each
part carries its own copy of the per-document products (``runs/``, ``evidence/``, ``documents/``) plus
the source PDF, and its own ``annotations/`` directory - so two people can annotate the same document
without ever touching each other's files, and nobody has to be told that some documents are annotated
twice (the overlap set stays in the admin-side manifest).

The copies are **hard links**: same filesystem, zero extra space, and a later rewrite of the parent
file (a re-run) creates a new inode, so the part keeps exactly the revision it was built from.

    python3 scripts/split_annotation_batches.py \
        --document-root .../ocr_agent_batch_v10 \
        --assignments  .../ocr_agent_batch_v10/workload/assignments.json \
        --out-root     data/annotation-batches \
        --prefix 标注_batch
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

PART_DIRS = ("runs", "evidence", "documents", "manifests", "pdf", "annotations")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def link_file(source: Path, target: Path, mode: str) -> str:
    """Link one file, creating parents. Returns 'hardlink' | 'symlink' | 'copy' | 'skipped'."""
    if target.exists() or target.is_symlink():
        return "skipped"
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "symlink":
        target.symlink_to(source)
        return "symlink"
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def link_tree(source_dir: Path, target_dir: Path, mode: str) -> int:
    count = 0
    if not source_dir.is_dir():
        return 0
    for path in sorted(source_dir.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source_dir)
        if link_file(path, target_dir / relative, mode) != "skipped":
            count += 1
    return count


def build_part(
    document_root: Path,
    out_root: Path,
    label: str,
    sample_ids: list[str],
    registry_by_sample: dict[str, dict[str, Any]],
    mode: str,
    refresh: bool,
) -> dict[str, Any]:
    part = out_root / label
    part.mkdir(parents=True, exist_ok=True)
    for name in PART_DIRS:
        (part / name).mkdir(parents=True, exist_ok=True)
    linked = {"runs": 0, "evidence": 0, "documents": 0, "manifests": 0, "pdf": 0}
    documents: list[dict[str, Any]] = []
    pages = 0
    for sample in sample_ids:
        entry = dict(registry_by_sample.get(sample) or {"sample_id": sample})
        for name in ("runs", "evidence"):
            linked[name] += link_tree(document_root / name / sample, part / name / sample, mode)
        for name in ("documents", "manifests"):
            source = document_root / name / f"{sample}.json"
            if source.is_file() and link_file(source, part / name / source.name, mode) != "skipped":
                linked[name] += 1
        pdf = Path(str(entry.get("pdf") or ""))
        if pdf.is_file():
            target = part / "pdf" / f"{sample}{pdf.suffix}"
            if link_file(pdf, target, mode) != "skipped":
                linked["pdf"] += 1
            entry["pdf"] = str(target)
        documents.append(entry)
        pages += int(entry.get("pages") or 0)
    registry = {
        "schema_version": (read_json(document_root / "v10_documents.json") or {}).get("schema_version"),
        "part": {"label": label, "documents": len(documents), "pages": pages, "source_batch": document_root.name},
        "documents": documents,
    }
    (part / "v10_documents.json").write_text(json.dumps(registry, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (part / "标注说明.md").write_text(
        "\n".join([
            f"# 本批标注任务（{label}）",
            "",
            f"- 文档 **{len(documents)}** 篇 / **{pages}** 页（页面图与证据已在本目录内）",
            f"- 打开方式：`http://<host>:8745/?batch={label}`",
            "- 操作说明：右上角「标注人工修正说明」（含类型/字段表与图文演示）",
            "",
            "## 流程",
            "1. 左侧栏切到「标注」模式，选一篇文档 → 逐页核对；",
            "2. 卡片里先定类型，再填「实际内容」（参考文本只用于核对）；",
            "3. 双栏页面注意阅读顺序（顺序行会提示是否跨栏交错，可用「按栏重排」）；",
            "4. 本页标完自动保存，左侧栏显示已标注/未标注；一页不落再换下一篇。",
            "",
            "> 本目录里的 `runs/`、`evidence/` 是上游产物（只读）；标注只写 `annotations/`。",
            "",
        ]),
        encoding="utf-8",
    )
    return {"label": label, "path": str(part), "documents": len(documents), "pages": pages, "linked": linked}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True, help="parent batch (kept intact)")
    parser.add_argument("--assignments", type=Path, help="defaults to <document-root>/workload/assignments.json")
    parser.add_argument("--out-root", type=Path, help="defaults to the parent's directory")
    parser.add_argument("--prefix", default="标注_batch", help="part name prefix (default 标注_batch)")
    parser.add_argument("--names", default="", help="optional display names, comma separated in annotator order")
    parser.add_argument("--mode", choices=["hardlink", "symlink"], default="hardlink")
    parser.add_argument("--refresh", action="store_true", help="re-link files missing from existing parts")
    parser.add_argument("--dry-run", action="store_true", help="report the split without touching the disk")
    args = parser.parse_args()

    assignments_path = args.assignments or args.document_root / "workload" / "assignments.json"
    plan = read_json(assignments_path)
    if not isinstance(plan, dict) or not plan.get("annotators"):
        raise SystemExit(f"assignment plan not found: {assignments_path}")
    registry = read_json(args.document_root / "v10_documents.json") or {}
    registry_by_sample = {str(item["sample_id"]): item for item in registry.get("documents") or []}
    out_root = args.out_root or args.document_root.parent
    names = [value.strip() for value in args.names.split(",") if value.strip()]

    parts: list[dict[str, Any]] = []
    for index, annotator in enumerate(plan["annotators"], start=1):
        label = f"{args.prefix}{index}"
        if names and index - 1 < len(names):
            label = f"{label}_{names[index - 1]}"
        sample_ids = list(annotator.get("sample_ids") or [])
        missing = [sample for sample in sample_ids if sample not in registry_by_sample]
        if args.dry_run:
            parts.append({
                "label": label, "annotator": annotator.get("id"), "documents": len(sample_ids),
                "pages": int(annotator.get("pages") or 0), "missing_from_registry": missing,
            })
            continue
        report = build_part(args.document_root, out_root, label, sample_ids, registry_by_sample, args.mode, args.refresh)
        report["annotator"] = annotator.get("id")
        report["missing_from_registry"] = missing
        parts.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)

    overlap = [
        {"sample_id": item["sample_id"], "annotators": item["annotators"], "pages": item.get("pages")}
        for item in plan.get("overlap") or []
    ]
    label_by_annotator = {entry.get("annotator") or "": entry["label"] for entry in parts}
    manifest = {
        "schema_version": "ocr-agent.annotation-parts.v1",
        "source_batch": str(args.document_root),
        "out_root": str(out_root),
        "mode": args.mode,
        "dry_run": bool(args.dry_run),
        "documents": sum(entry["documents"] for entry in parts),
        "parts": parts,
        "overlap": [
            {**item, "part_labels": [label_by_annotator.get(annotator, annotator) for annotator in item["annotators"]]}
            for item in overlap
        ],
    }
    target = out_root / "annotation_parts_manifest.json"
    if not args.dry_run:
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({
        "parts": [(entry["label"], entry["documents"], entry["pages"]) for entry in parts],
        "overlap": len(overlap),
        "manifest": str(target) if not args.dry_run else "(dry-run)",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
