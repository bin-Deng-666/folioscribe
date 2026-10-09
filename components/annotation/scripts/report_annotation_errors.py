#!/usr/bin/env python3
"""Error attribution from human annotations: what does the machine get wrong, and how often?

Every saved page patch keeps both sides - ``chain_blocks`` (what the pipeline produced) and ``blocks``
(what the annotator left behind) plus the ``operations`` log - so each page is a labelled example. This
report turns them into a taxonomy with counts, examples and the pipeline stage that should change.

Usage:
    python3 scripts/report_annotation_errors.py --annotations <part>/annotations [--out report.md]
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
from pathlib import Path

TEXT_TYPES = {"text", "title", "caption", "formula", "image_caption", "table_caption", "reference", "footnote"}
FURNITURE = {"header", "footer", "page_number"}


def load_annotation(path: Path, document_root: Path) -> dict:
    """The saved patch carries the annotator's blocks; the machine's baseline is the page result."""
    data = json.loads(path.read_text(encoding="utf-8"))
    result_path = document_root / "runs" / path.parent.name / f"page_{int(data.get('page') or 1):04d}" / "result.json"
    chain = {}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        for block in (result.get("revision") or {}).get("blocks") or []:
            chain[block["block_id"]] = block
    final = {}
    for block in data.get("blocks") or []:
        final[block["block_id"]] = block
    return {"chain": chain, "final": final, "operations": data.get("operations") or [], "data": data}


def normalise(text: str) -> str:
    return " ".join(str(text or "").split())


def classify_text_change(before: str, after: str) -> str:
    """Why did the text change? The reason decides which stage has to be fixed.

    A collapsed comparison separates "the annotator reflowed line breaks / spacing" (a formatting
    policy question) from "the pipeline read the characters wrong" (a recognition problem) - they need
    completely different fixes, and lumping them together would hide both.
    """
    if not normalise(before) and normalise(after):
        return "missing_text"
    collapse = lambda value: "".join(str(value or "").split())
    if collapse(before) == collapse(after):
        return "linebreak_or_space"
    if normalise(before) == normalise(after):
        return "whitespace_only"
    ratio = difflib.SequenceMatcher(None, normalise(before), normalise(after)).ratio()
    if ratio >= 0.75:
        return "recognition_error"
    if ratio >= 0.35:
        return "partial_rewrite"
    return "rewritten"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--document-root", type=Path, default=None, help="batch holding the machine results (defaults to <annotations>/..)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    root = args.annotations
    document_root = args.document_root or root.parent
    pages = sorted(root.glob("*/page_*.json"))
    retypes: collections.Counter = collections.Counter()
    deleted_types: collections.Counter = collections.Counter()
    deleted_with_evidence = deleted_without_evidence = 0
    added_covering_chain = added_empty_area = 0
    text_reasons: collections.Counter = collections.Counter()
    text_examples: list[str] = []
    merge_sizes: collections.Counter = collections.Counter()
    order_only = geometry_changed = 0
    level_changes = 0
    op_counts: collections.Counter = collections.Counter()
    per_type_final: collections.Counter = collections.Counter()
    per_type_chain: collections.Counter = collections.Counter()
    pages_touched = chain_total = final_total = 0
    verdicts: collections.Counter = collections.Counter()

    for path in pages:
        record = load_annotation(path, document_root)
        # Test artifacts from viewer development must not pollute the attribution.
        if any("测试保存" in str(block.get("text") or "") for block in record["final"].values()):
            continue
        chain, final = record["chain"], record["final"]
        if not chain:
            continue
        pages_touched += 1
        chain_total += len(chain)
        final_total += len(final)
        for block in chain.values():
            per_type_chain[block.get("type")] += 1
        for block in final.values():
            per_type_final[block.get("type")] += 1
        for operation in record["operations"]:
            kind = str(operation.get("kind") or "")
            op_counts[kind] += 1
            fields = [str(value) for value in (operation.get("fields") or [])]
            if kind == "retype" and len(fields) >= 1:
                pass
            if kind == "merge":
                merge_sizes[len(fields)] += 1
            if kind == "move":
                if any("bbox" in value for value in fields):
                    geometry_changed += 1
                else:
                    order_only += 1
            if kind == "update" and any(value == "level" for value in fields):
                level_changes += 1

        for block_id, block in chain.items():
            other = final.get(block_id)
            if other is None:
                deleted_types[block.get("type")] += 1
                if block.get("source_refs"):
                    deleted_with_evidence += 1
                else:
                    deleted_without_evidence += 1
                continue
            if block.get("type") != other.get("type"):
                retypes[(str(block.get("type")), str(other.get("type")))] += 1
            before, after = normalise(block.get("text")), normalise(other.get("text"))
            if before != after:
                reason = classify_text_change(block.get("text"), other.get("text"))
                text_reasons[reason] += 1
                if len(text_examples) < 12 and reason in {"recognition_error", "partial_rewrite", "rewritten", "missing_text"}:
                    text_examples.append(f"{path.parent.name} p{record['data'].get('page')}: {str(block.get('text'))[:70]!r} → {str(other.get('text'))[:70]!r} ({reason})")

        for block_id, block in final.items():
            if block_id in chain:
                continue
            box = block.get("bbox_image_px") or [0, 0, 0, 0]
            covered = False
            for other in chain.values():
                other_box = other.get("bbox_image_px") or [0, 0, 0, 0]
                ix1, iy1 = max(box[0], other_box[0]), max(box[1], other_box[1])
                ix2, iy2 = min(box[2], other_box[2]), min(box[3], other_box[3])
                if ix2 > ix1 and iy2 > iy1:
                    covered = True
                    break
            if covered:
                added_covering_chain += 1
            else:
                added_empty_area += 1

        verdict = (record["data"].get("revision_validation") or {}).get("status")
        if verdict:
            verdicts[str(verdict)] += 1

    lines = [
        "# 人工标注错误归因（基于你自己的标注）",
        "",
        f"- 样本：**{pages_touched} 个已标注页面**（来自 `{root}`）",
        f"- 机器块数 {chain_total} → 人工修正后 {final_total}（净变化 {final_total - chain_total:+d}）",
        f"- 操作次数：{', '.join(f'{k} {v}' for k, v in op_counts.most_common())}",
        f"- 标注后的页级校验：{', '.join(f'{k} {v}' for k, v in verdicts.most_common()) or '（未记录）'}",
        "",
        "## 1. 块被删除（机器多给了）",
        f"- 共删除 **{sum(deleted_types.values())}** 个块：有证据引用 {deleted_with_evidence} · 无证据引用 {deleted_without_evidence}",
        "- 按类型：" + "、".join(f"`{t}` {n}" for t, n in deleted_types.most_common(8)),
        "",
        "## 2. 类型判错（retype）",
        f"- 共 **{sum(retypes.values())}** 次改类型，Top 迁移：",
        ] + [f"  - `{a}` → `{b}`：{n}" for (a, b), n in retypes.most_common(10)] + [
        "",
        "## 3. 文本错误（update text）",
        f"- 共 **{sum(text_reasons.values())}** 处文本被改：" + "、".join(f"{k} {v}" for k, v in text_reasons.most_common()),
        "",
        "  样例：",
        ] + [f"  - {example}" for example in text_examples] + [
        "",
        "## 4. 漏块（人工新增）",
        f"- 新增 **{added_covering_chain + added_empty_area}** 个块：覆盖已有块区域 {added_covering_chain} · 机器完全没覆盖区域 {added_empty_area}",
        "",
        "## 5. 过度切分（merge）",
        f"- 合并 **{sum(merge_sizes.values())}** 次：" + "、".join(f"合并 {k} 块 × {v}" for k, v in merge_sizes.most_common()),
        "",
        "## 6. 阅读顺序 / 几何",
        f"- 纯顺序调整 **{order_only}** 次 · 几何（移动/缩放）**{geometry_changed}** 次 · 层级(level)修改 **{level_changes}** 次",
        "",
        "## 7. 类型分布（机器 → 人工）",
        "",
        "| 类型 | 机器 | 人工 | 差值 |",
        "|---|---:|---:|---:|",
        ]
    for block_type in sorted(set(per_type_chain) | set(per_type_final)):
        machine, human = per_type_chain[block_type], per_type_final[block_type]
        lines.append(f"| `{block_type}` | {machine} | {human} | {human - machine:+d} |")

    report = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"报告写入 {args.out}")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
