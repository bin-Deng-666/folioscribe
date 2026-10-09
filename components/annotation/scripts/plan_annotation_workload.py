#!/usr/bin/env python3
"""Split an annotation batch into per-annotator workloads with a quality-control overlap set.

Several people annotate one batch, so the work has to be split up front, balanced by **pages** (a
12-page manual is not the same workload as a 2-page report), and a small set of documents must be
annotated by two people so inter-annotator agreement can be measured.

Outputs (under ``--out``, default ``<batch>/workload``):

* ``assignments.json`` - machine-readable plan: annotators, per-document owners, overlap documents
* ``assignments.md``   - the same plan as a readable table
* ``assignments.csv``  - one row per (document, annotator) for spreadsheets

Design decisions
----------------
* balance on **pages**, not document count (document sizes range 1-96 pages here)
* overlap documents are chosen by **QC value**: tables, cross-page continuations, captions and type
  variety are what annotators actually disagree about, so those documents carry the most information
* overlap pairs avoid the same two annotators teaming up twice, and no annotator's total load may
  exceed the balanced average by more than ``--load-slack``
* every document stays whole (all pages to one annotator); an overlap document is simply assigned to
  two annotators
* **the annotators never see this plan**: overlap membership and identity stay admin-side. Each
  annotator gets a random token that selects their queue; ``tokens.json`` is the only file that maps
  a token to a person, and it contains no annotation content.
"""

from __future__ import annotations

import argparse
import csv
import json
import secrets
from collections import Counter
from pathlib import Path
from typing import Any

TEXT_LIKE = {"text", "title", "table_caption", "image_caption", "footnote", "formula"}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def document_complexity(document_root: Path, sample_id: str) -> dict[str, Any]:
    """How much disagreement surface a document has (used to pick the overlap set)."""
    tables = captions = cross_page = 0
    types: Counter = Counter()
    pages = 0
    for result_path in sorted((document_root / "runs" / sample_id).glob("page_*/result.json")):
        result = read_json(result_path)
        if not isinstance(result, dict):
            continue
        pages += 1
        revision = result.get("revision") or {}
        for block in revision.get("blocks") or []:
            block_type = str(block.get("type"))
            types[block_type] += 1
            if block_type == "table":
                tables += 1
            if block_type in {"table_caption", "image_caption"}:
                captions += 1
        cross_page += len(revision.get("cross_page_relations") or [])
    return {
        "pages": pages,
        "tables": tables,
        "captions": captions,
        "cross_page": cross_page,
        "type_variety": len(types),
        "score": tables * 3 + cross_page * 4 + captions * 2 + len(types) + min(pages, 12) * 0.5,
    }


def balance(documents: list[dict[str, Any]], annotators: list[str]) -> dict[str, list[str]]:
    """Longest-processing-time first: the biggest document always goes to the lightest annotator."""
    loads = {annotator: 0 for annotator in annotators}
    assignment: dict[str, list[str]] = {annotator: [] for annotator in annotators}
    for document in sorted(documents, key=lambda item: (-int(item.get("pages") or 0), item["sample_id"])):
        annotator = min(annotators, key=lambda name: (loads[name], name))
        assignment[annotator].append(document["sample_id"])
        loads[annotator] += int(document.get("pages") or 0)
    return assignment


def pick_overlap(
    documents: list[dict[str, Any]],
    assignment: dict[str, list[str]],
    overlaps: int,
    slack: float = 0.12,
) -> list[dict[str, Any]]:
    """Choose documents that maximize QC value while keeping the page load balanced."""
    owner_of = {sample: annotator for annotator, samples in assignment.items() for sample in samples}
    pages_of = {document["sample_id"]: int(document.get("pages") or 0) for document in documents}
    loads = {annotator: sum(pages_of[sample] for sample in samples) for annotator, samples in assignment.items()}
    target = sum(loads.values()) / max(1, len(loads))
    cap = target * (1 + slack)
    pairs: Counter = Counter()
    chosen: list[dict[str, Any]] = []
    # Prefer informative, medium-sized documents, and spread them across cells/owners.
    ranked = sorted(
        documents,
        key=lambda item: (
            -float(item.get("complexity_score") or 0),
            abs(int(item.get("pages") or 0) - 4),
            item["sample_id"],
        ),
    )
    used_cells: Counter = Counter()
    for document in ranked:
        if len(chosen) >= overlaps:
            break
        sample = document["sample_id"]
        owner = owner_of[sample]
        cell = str(document.get("cell_key") or "")
        if used_cells[cell] >= 2:
            continue
        partner = None
        for candidate in sorted(loads, key=lambda name: (loads[name], name)):
            if candidate == owner:
                continue
            if pairs[tuple(sorted((owner, candidate)))] >= 3:
                continue
            if loads[candidate] + pages_of[sample] > cap:
                continue
            partner = candidate
            break
        if partner is None:
            continue
        loads[partner] += pages_of[sample]
        pairs[tuple(sorted((owner, partner)))] += 1
        used_cells[cell] += 1
        chosen.append({
            "sample_id": sample,
            "cell_key": cell,
            "pages": pages_of[sample],
            "annotators": sorted([owner, partner]),
            "complexity": round(float(document.get("complexity_score") or 0), 2),
            "tables": document.get("tables"),
            "cross_page": document.get("cross_page"),
        })
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True, help="batch, e.g. .../ocr_agent_batch_v10")
    parser.add_argument("--registry", type=Path, help="defaults to <document-root>/v10_documents.json")
    parser.add_argument("--annotators", required=True, help="comma separated annotator ids, e.g. a1,a2,a3")
    parser.add_argument("--names", default="", help="optional comma separated display names, same order as --annotators")
    parser.add_argument("--overlap", type=int, default=12, help="number of documents annotated by two people")
    parser.add_argument("--load-slack", type=float, default=0.12, help="allowed extra load for an overlap partner")
    parser.add_argument("--out", type=Path, help="output directory (default <document-root>/workload)")
    args = parser.parse_args()

    registry_path = args.registry or args.document_root / "v10_documents.json"
    registry = read_json(registry_path)
    if not isinstance(registry, dict) or not registry.get("documents"):
        raise SystemExit(f"registry without documents: {registry_path}")
    annotators = [value.strip() for value in args.annotators.split(",") if value.strip()]
    names = [value.strip() for value in args.names.split(",") if value.strip()]
    if names and len(names) != len(annotators):
        raise SystemExit("--names must match --annotators in length")

    documents: list[dict[str, Any]] = []
    for document in registry["documents"]:
        sample = document["sample_id"]
        complexity = document_complexity(args.document_root, sample)
        entry = dict(document)
        entry["cell_key"] = document.get("cell_key") or f"{document.get('domain')}"
        entry["pages"] = int(document.get("pages") or complexity["pages"] or 0)
        entry.update(complexity)
        entry["complexity_score"] = complexity["score"]
        documents.append(entry)

    assignment = balance(documents, annotators)
    overlap = pick_overlap(documents, assignment, args.overlap, args.load_slack)

    pages_of = {document["sample_id"]: int(document.get("pages") or 0) for document in documents}
    overlap_by_sample = {item["sample_id"]: item for item in overlap}
    per_annotator: dict[str, dict[str, Any]] = {}
    for annotator in annotators:
        samples = assignment[annotator]
        overlap_samples = [sample for sample in samples if sample in overlap_by_sample]
        per_annotator[annotator] = {
            "documents": len(samples),
            "pages": sum(pages_of[sample] for sample in samples),
            "overlap_documents": sorted(overlap_samples),
            "cells": dict(sorted(Counter(str(next(d for d in documents if d["sample_id"] == sample).get("cell_key")) for sample in samples).items())),
            "sample_ids": sorted(samples),
        }
    # overlap partners get the document too
    for item in overlap:
        for annotator in item["annotators"]:
            if item["sample_id"] not in per_annotator[annotator]["sample_ids"]:
                per_annotator[annotator]["sample_ids"] = sorted(per_annotator[annotator]["sample_ids"] + [item["sample_id"]])
                per_annotator[annotator]["documents"] += 1
                per_annotator[annotator]["pages"] += item["pages"]
                per_annotator[annotator]["overlap_documents"] = sorted(per_annotator[annotator]["overlap_documents"] + [item["sample_id"]])

    tokens = {annotator: secrets.token_urlsafe(12) for annotator in annotators}
    plan = {
        "schema_version": "ocr-agent.annotation-workload.v1",
        "queue_keys": tokens,
        "document_root": str(args.document_root),
        "registry": str(registry_path),
        "documents": len(documents),
        "pages": sum(pages_of.values()),
        "annotators": [
            {"id": annotator, "name": names[index] if names else annotator, **per_annotator[annotator]}
            for index, annotator in enumerate(annotators)
        ],
        "overlap": overlap,
        "overlap_by_sample": overlap_by_sample,
        "owners": {document["sample_id"]: [annotator for annotator in annotators if document["sample_id"] in per_annotator[annotator]["sample_ids"]] for document in documents},
    }
    target = args.out or (args.document_root / "workload")
    target.mkdir(parents=True, exist_ok=True)
    (target / "assignments.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (target / "tokens.json").write_text(
        json.dumps({"schema_version": "ocr-agent.annotation-tokens.v1", "tokens": tokens}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# 人工修正标注分派表（**管理侧文档，不要发给标注人**）",
        "",
        "> 本文件含标注人身份与 overlap（盲检）信息，仅供分派与质量复核使用。",
        "> 标注人只拿到自己的入口链接，界面上不显示身份、也看不出哪些文档是 overlap。",
        "",
        f"- 批次：`{args.document_root}`",
        f"- 文档 / 页：**{plan['documents']}** / **{plan['pages']}**",
        f"- 标注人：{len(annotators)} 人 · overlap 文档：**{len(overlap)}** 篇（由两人各标一遍）",
        "",
        "| 标注人 | 文档 | 页数 | 其中 overlap | 覆盖单元数 |",
        "|---|---:|---:|---:|---:|",
    ]
    for entry in plan["annotators"]:
        lines.append(
            f"| {entry['name']} (`{entry['id']}`) | {entry['documents']} | {entry['pages']} |"
            f" {len(entry['overlap_documents'])} | {len(entry['cells'])} |"
        )
    lines += ["", "## 标注人入口（发给本人；令牌即身份，界面不显示姓名）", "",
              "| 标注人 | 入口 |", "|---|---|"]
    for entry in plan["annotators"]:
        lines.append(f"| `{entry['id']}` | `http://<host>:8745/?batch={args.document_root.name}&t={tokens[entry['id']]}` |")
    lines += ["", "## Overlap 文档（两人各标一遍，用于一致性检查；不要告知标注人）", "",
              "| 文档 | 单元 | 页数 | 标注人 | 复杂分 | 表格 | 跨页 |", "|---|---|---:|---|---:|---:|---:|"]
    for item in overlap:
        lines.append(
            f"| `{item['sample_id']}` | `{item['cell_key']}` | {item['pages']} | {', '.join(item['annotators'])} |"
            f" {item['complexity']} | {item.get('tables')} | {item.get('cross_page')} |"
        )
    (target / "assignments.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (target / "assignments.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_id", "annotator", "is_overlap", "pages", "cell_key", "overlap_partner"])
        for document in sorted(documents, key=lambda item: item["sample_id"]):
            sample = document["sample_id"]
            owners = plan["owners"][sample]
            for annotator in owners:
                partner = ",".join(other for other in owners if other != annotator)
                writer.writerow([sample, annotator, "yes" if sample in overlap_by_sample else "no", pages_of[sample], document.get("cell_key"), partner])

    print(json.dumps({
        "documents": plan["documents"],
        "pages": plan["pages"],
        "annotators": [(entry["id"], entry["documents"], entry["pages"], len(entry["overlap_documents"])) for entry in plan["annotators"]],
        "overlap": len(overlap),
        "out": str(target),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
