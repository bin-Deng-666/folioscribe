#!/usr/bin/env python3
"""Learn the *target* annotation structure from the human's final blocks (not from error counts).

For every difference between the machine's page and the annotator's final page this reports *what the
difference means structurally*: a text block the human removed because it is a fragment of a paragraph
they kept, a figure fragment inside a table, a caption that had to be bound, furniture that had to go.
The output is the specification the pipeline has to hit.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
from pathlib import Path


def norm(text: str) -> str:
    return "".join(str(text or "").split())


def box(block: dict) -> list[float]:
    values = block.get("bbox_image_px") or [0, 0, 0, 0]
    return [float(values[0]), float(values[1]), float(values[2]), float(values[3])]


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

    human_text_chars: list[int] = []
    human_text_lines: list[int] = []
    human_per_page: list[int] = []
    deleted_text_absorbed = deleted_text_spurious = 0
    deleted_image_inside = deleted_image_spurious = 0
    kept_types: collections.Counter = collections.Counter()
    retype_matrix: collections.Counter = collections.Counter()
    retype_font_ratio: dict[str, list[float]] = collections.defaultdict(list)
    furniture_kept = furniture_dropped = 0
    absorbed_examples: list[str] = []
    spurious_examples: list[str] = []
    caption_attached = 0
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
        evidence_path = root / "evidence" / sample_id / f"page_{page_number:04d}.json"
        if not result_path.exists():
            continue
        pages += 1
        machine = (json.loads(result_path.read_text(encoding="utf-8")).get("revision") or {}).get("blocks") or []
        evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else {}
        typography = evidence.get("typography") or {}
        summary = evidence.get("typography_summary") or {}
        body_size = summary.get("body_font_size")
        page_image = evidence.get("page_image") or {}
        page_area = max(1.0, float(page_image.get("width") or 0) * float(page_image.get("height") or 0))

        human_by_id = {b["block_id"]: b for b in human}
        human_texts = [norm(b.get("text")) for b in human if str(b.get("text") or "").strip()]
        human_texts = [t for t in human_texts if t]
        human_boxes = [box(b) for b in human]

        for block in human:
            kept_types[str(block.get("type"))] += 1
            if str(block.get("type")) in {"header", "footer", "page_number"}:
                furniture_kept += 1
            text = str(block.get("text") or "")
            if text.strip() and str(block.get("type")) in {"text", "title", "image_caption", "table_caption", "reference"}:
                human_text_chars.append(len(norm(text)))
                height = box(block)[3] - box(block)[1]
                if body_size:
                    human_text_lines.append(max(1, round(height / (float(body_size) * 1.5))))
            if block.get("parent_id"):
                caption_attached += 1
        human_per_page.append(len(human))

        for block in machine:
            block_id = block.get("block_id")
            if block_id in human_by_id:
                before, after = str(block.get("type")), str(human_by_id[block_id].get("type"))
                if before != after:
                    retype_matrix[(before, after)] += 1
                    entry = typography.get(block_id) or {}
                    if entry.get("font_size") and body_size:
                        retype_font_ratio[f"{before}->{after}"].append(round(float(entry["font_size"]) / float(body_size), 2))
                continue
            # removed by the annotator: was its content absorbed into a block they kept, or simply junk?
            text = norm(block.get("text"))
            if str(block.get("type")) == "text":
                absorbed = bool(text) and any(text in kept for kept in human_texts)
                if not absorbed and text:
                    covered = False
                    for kept_box, kept in zip(human_boxes, human):
                        if overlap(box(block), kept_box) / max(1.0, area(box(block))) >= 0.7 and text in norm(kept.get("text")):
                            covered = True
                            break
                    absorbed = covered
                if absorbed:
                    deleted_text_absorbed += 1
                    if len(absorbed_examples) < 5:
                        absorbed_examples.append(f"{sample_id} p{page_number}: '{(str(block.get('text')) or '')[:48]}'")
                else:
                    deleted_text_spurious += 1
                    if len(spurious_examples) < 5:
                        spurious_examples.append(f"{sample_id} p{page_number}: type={block.get('type')} '{(str(block.get('text')) or '')[:48]}'")
            elif str(block.get("type")) in {"image", "chart", "seal"}:
                inside = any(
                    overlap(box(block), box(other)) / max(1.0, area(box(block))) >= 0.5
                    for other in machine + human
                    if other is not block
                )
                if inside or area(box(block)) < 0.02 * page_area:
                    deleted_image_inside += 1
                else:
                    deleted_image_spurious += 1
            elif str(block.get("type")) in {"header", "footer", "page_number"}:
                furniture_dropped += 1

    def stats(values: list[int]) -> str:
        if not values:
            return "n/a"
        ordered = sorted(values)
        return f"中位 {statistics.median(ordered):.0f} · P25 {ordered[len(ordered)//4]} · P75 {ordered[3*len(ordered)//4]} · 最大 {max(ordered)}"

    report = [
        "# 目标标注形态（从你的终稿学到的规格）",
        "",
        f"- 样本：{pages} 页 · 你的终稿共 {sum(human_per_page)} 块（每页中位 {statistics.median(human_per_page):.0f} 块）",
        "",
        "## 1. 文本块的粒度（你保留的）",
        f"- 字符数：{stats(human_text_chars)}",
        f"- 估算行数：{stats(human_text_lines)}",
        "→ 你按 **逻辑段落**切块，而不是按行/按模型片段；一行一块必被合并。",
        "",
        "## 2. 你删掉的文本块：是被吸收还是垃圾",
        f"- 内容被并入你保留的块（**段落被机器切碎**）：**{deleted_text_absorbed}**",
        f"- 内容未被任何保留块包含（真多余）：**{deleted_text_spurious}**",
        "",
        "  吸收样例：" + ("；".join(absorbed_examples) or "无"),
        "  多余样例：" + ("；".join(spurious_examples) or "无"),
        "",
        "## 3. 你删掉的图块",
        f"- 与其它块高度重叠 / 面积极小（**图碎片、表内图**）：**{deleted_image_inside}**",
        f"- 独立且不小的图（真的多余）：**{deleted_image_spurious}**",
        "",
        "## 4. 页眉页脚等家具",
        f"- 你保留 {furniture_kept} 个 · 你删除 {furniture_dropped} 个",
        "→ " + ("家具是要保留的（机器应保证不误删、不误并）" if furniture_kept >= furniture_dropped else "家具多数应删除"),
        "",
        "## 5. 改类型：机器 → 你的终稿（附字号比值，用于判定谁对）",
        "",
        "| 迁移 | 次数 | 机器块字号/正文比值（中位） |",
        "|---|---:|---:|",
    ]
    for (before, after), count in retype_matrix.most_common(12):
        ratios = retype_font_ratio.get(f"{before}->{after}") or []
        ratio_text = f"{statistics.median(ratios):.2f}" if ratios else "n/a"
        report.append(f"| `{before}` → `{after}` | {count} | {ratio_text} |")
    report += [
        "",
        "  字号比值 ≈1.0 表示「机器误判为标题的正文」，>1.15 表示「真标题被判成正文」。",
        "",
        "## 6. 图注绑定",
        f"- 你的终稿里有 `parent_id`（图注绑定）的块：**{caption_attached}**",
        "",
        "## 7. 由规格倒推的产出要求",
        "1. **段落级文本**：同栏、行距紧、未断句的相邻文本必须合成一块；连续编号参考文献合成一块；",
        "2. **图只保留独立图示**：表内图、与其它图/文字高度重叠的小图直接丢弃（不在你的终稿里）；",
        "3. **家具保留但绝不合并**：页眉页脚跨页重复，位置固定，合并会把它们吃掉；",
        "4. **类型由排版证据定**：字号比值 ≈1 的「标题」降为正文；",
        "5. **图注必须绑定 `parent_id`**：按位置 + 宽度对齐判定，不靠文字模式。",
    ]
    text = "\n".join(report) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
