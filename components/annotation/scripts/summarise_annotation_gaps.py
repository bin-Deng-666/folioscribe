#!/usr/bin/env python3
"""Where exactly does the machine disagree with the annotators, per document, and what task fixes it?

Groups every difference into a small set of precise signatures (reference list split, figure fragment,
caption as text, redundant furniture, paragraph fragment, article boundary) with counts, the documents
they concentrate in, and the model task that should resolve it.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

REFERENCE = re.compile(r"^\s*\[?\d{1,3}[\.\)\]](\s|$)")


def norm(text: str) -> str:
    return "".join(str(text or "").split())


def box(block: dict) -> list[float]:
    values = block.get("bbox_image_px") or [0, 0, 0, 0]
    return [float(v) for v in values]


def area(values: list[float]) -> float:
    return max(0.0, values[2] - values[0]) * max(0.0, values[3] - values[1])


def overlap(a: list[float], b: list[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    root = args.document_root
    registry = {d["sample_id"]: d for d in json.loads((root / "v10_documents.json").read_text(encoding="utf-8"))["documents"]}

    signatures: dict[str, dict] = collections.defaultdict(lambda: {"pages": 0, "blocks": 0, "docs": collections.Counter(), "examples": []})
    per_doc: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    pages = 0

    for annotation in sorted((root / "annotations").glob("*/page_*.json")):
        sample_id = annotation.parent.name
        if sample_id not in registry:
            continue
        record = json.loads(annotation.read_text(encoding="utf-8"))
        human = record.get("blocks") or []
        if any("测试保存" in str(b.get("text") or "") for b in human):
            continue
        page_number = int(record.get("page") or 0)
        result_path = root / "runs" / sample_id / f"page_{page_number:04d}" / "result.json"
        if not result_path.exists():
            continue
        pages += 1
        machine = (json.loads(result_path.read_text(encoding="utf-8")).get("revision") or {}).get("blocks") or []
        human_ids = {b["block_id"]: b for b in human}
        human_texts = [norm(b.get("text")) for b in human if norm(b.get("text"))]

        # ---- signature: reference list split into many blocks
        machine_refs = [b for b in machine if b.get("type") == "text" and REFERENCE.match(str(b.get("text") or ""))]
        human_refs = [b for b in human if REFERENCE.match(str(b.get("text") or ""))]
        if len(machine_refs) >= 3 and len(human_refs) <= max(1, len(machine_refs) // 3):
            key = "reference_list_split"
            entry = signatures[key]
            entry["pages"] += 1
            entry["blocks"] += len(machine_refs) - len(human_refs)
            entry["docs"][sample_id] += 1
            per_doc[sample_id][key] += len(machine_refs) - len(human_refs)
            if len(entry["examples"]) < 4:
                entry["examples"].append(f"{sample_id} p{page_number}: 机器 {len(machine_refs)} 条编号块 → 你 {len(human_refs)} 块")

        for block in machine:
            if block["block_id"] in human_ids:
                if str(block.get("type")) != str(human_ids[block["block_id"]].get("type")):
                    key = f"type:{block.get('type')}->{human_ids[block['block_id']].get('type')}"
                    signatures[key]["pages"] += 1
                    signatures[key]["docs"][sample_id] += 1
                    per_doc[sample_id]["type_mismatch"] += 1
                    if len(signatures[key]["examples"]) < 3:
                        signatures[key]["examples"].append(f"{sample_id} p{page_number}: {str(human_ids[block['block_id']].get('text'))[:40]!r}")
                continue
            text = norm(block.get("text"))
            block_type = str(block.get("type"))
            if block_type in {"image", "chart", "seal"}:
                covered = any(
                    overlap(box(block), box(other)) / max(1.0, area(box(block))) >= 0.5
                    for other in machine + human
                    if other is not block and str(other.get("type")) in {"table", "image", "chart", "text"}
                )
                if covered:
                    key, label = "figure_fragment_or_table_image", "图碎片/表内图"
                elif area(box(block)) < 20000:
                    key, label = "tiny_figure", "小图（多为装饰/图标）"
                else:
                    key, label = "spurious_figure", "独立多余的图"
                signatures[key]["pages"] += 1
                signatures[key]["docs"][sample_id] += 1
                per_doc[sample_id][key] += 1
                if len(signatures[key]["examples"]) < 3:
                    signatures[key]["examples"].append(f"{sample_id} p{page_number} {[round(v) for v in box(block)]} {label}")
                continue
            if block_type == "text" and text:
                if any(text in kept for kept in human_texts):
                    key = "paragraph_fragment_absorbed"
                elif REFERENCE.match(str(block.get("text") or "")):
                    key = "reference_entry_separate"
                else:
                    key = "spurious_text"
                signatures[key]["pages"] += 1
                signatures[key]["docs"][sample_id] += 1
                per_doc[sample_id][key] += 1
                if len(signatures[key]["examples"]) < 3:
                    signatures[key]["examples"].append(f"{sample_id} p{page_number}: {str(block.get('text'))[:44]!r}")
                continue
            if block_type in {"header", "footer", "page_number"}:
                signatures["redundant_furniture"]["pages"] += 1
                signatures["redundant_furniture"]["docs"][sample_id] += 1
                per_doc[sample_id]["redundant_furniture"] += 1

        machine_ids = {b["block_id"] for b in machine}
        for h in human:
            if h["block_id"] in machine_ids:
                continue
            key = "human_added_figure" if str(h.get("type")) in {"image", "chart", "seal"} else "human_added_text"
            signatures[key]["pages"] += 1
            signatures[key]["docs"][sample_id] += 1
            per_doc[sample_id][key] += 1
            if len(signatures[key]["examples"]) < 3:
                signatures[key]["examples"].append(f"{sample_id} p{page_number}: {str(h.get('text'))[:40]!r}")

    order = ["paragraph_fragment_absorbed", "reference_list_split", "reference_entry_separate", "figure_fragment_or_table_image",
             "spurious_figure", "tiny_figure", "spurious_text", "redundant_furniture", "human_added_figure", "human_added_text"]
    lines = ["# 跨文档问题归纳（机器 vs 人工终稿）", "", f"- 样本 {pages} 页", "", "| 问题签名 | 影响页数 | 涉及块数 | 集中在哪些文档 | 例子 |", "|---|---:|---:|---|---|"]
    for key in order + [k for k in signatures if k not in order]:
        entry = signatures.get(key)
        if not entry or not entry["pages"]:
            continue
        docs = ", ".join(f"`{doc}`×{count}" for doc, count in entry["docs"].most_common(3))
        lines.append(f"| `{key}` | {entry['pages']} | {entry['blocks'] or entry['pages']} | {docs} | {'；'.join(entry['examples'][:2])} |")
    lines += ["", "## 问题最集中的文档（按差异块数）", "", "| 文档 | 主要问题 |", "|---|---|"]
    for sample_id, counter in sorted(per_doc.items(), key=lambda item: -sum(item[1].values()))[:8]:
        top = ", ".join(f"{name}×{count}" for name, count in counter.most_common(3))
        lines.append(f"| `{sample_id}` | {top} |")
    text = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
