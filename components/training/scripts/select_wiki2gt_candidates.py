"""wiki2gt → SFT 候选筛选（先扫描全量特征，再分层定额抽样）。

用法：
  python3 select_sft_candidates.py --scan   [--workers 12]     # 全量特征 → features.jsonl
  python3 select_sft_candidates.py --select [--target 1000]    # 分层抽样 → selection.json + REPORT.md
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import re
import statistics
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path("data/wiki2gt")
OUT = Path("data/selection")
CELL_RE = re.compile(r"r\d+c\d+$")
SKIP = {"_history", "_runs"}


def parse_struct(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        for loader in (json.loads, ast.literal_eval):
            try:
                return loader(value)
            except Exception:
                continue
    return {}


def document_features(path: str) -> dict | None:
    d = Path(path)
    try:
        geo = json.loads((d / "gt/page_geometry.json").read_text())
        elements = json.loads((d / "gt/gt_raw.json").read_text()).get("elements", [])
        bundle = json.loads((d / "gt/gt_bundle.json").read_text())
    except Exception as exc:  # noqa: BLE001
        return {"space": d.parent.name, "doc": d.name, "error": str(exc)[:80]}
    pages = len(geo) if isinstance(geo, list) else 0
    levels, paras, images = Counter(), 0, 0
    tables = xp_tables = max_span = header_repeated = merged = max_rows = 0
    for element in elements:
        kind, tag = element.get("block_type"), element.get("tag", "")
        frags = element.get("fragments") or []
        if kind == "title" and tag[:1] == "h" and tag[1:].isdigit():
            levels[f"h{tag[1]}"] += 1
        elif kind == "image":
            images += 1
        elif kind == "text" and not element.get("cross_page"):
            paras += 1
        elif kind == "table":
            tables += 1
            struct = parse_struct(element.get("table_structure"))
            rows, cells = int(struct.get("row_count") or 0), struct.get("cells") or []
            max_rows = max(max_rows, rows)
            merged += any(int(c.get("rowspan") or 1) > 1 or int(c.get("colspan") or 1) > 1 for c in cells)
            cont = element.get("continuation") or {}
            span = len(cont.get("pages") or []) or len({f.get("page_idx") for f in frags})
            max_span = max(max_span, span)
            if element.get("cross_page") or span > 1:
                xp_tables += 1
                merged += 0
            if cont.get("header_repeated"):
                header_repeated += 1
    xp_text = sum(1 for e in elements if e.get("block_type") == "text" and e.get("cross_page"))
    text_chars = max_cell = 0
    pages_seen = set()
    for entity in bundle.get("a1_entities", []):
        text = entity.get("text") or ""
        pages_seen.add(entity.get("page_number"))
        if CELL_RE.search(entity.get("entity_id", "")):
            max_cell = max(max_cell, len(text))
        else:
            text_chars += len(text)
    xp_tables = sum(1 for e in elements if e.get("block_type") == "table"
                    and (e.get("cross_page") or len({f.get("page_idx") for f in (e.get("fragments") or [])}) > 1))
    merged_tables = sum(1 for e in elements if e.get("block_type") == "table" and (
        lambda s: any(int(c.get("rowspan") or 1) > 1 or int(c.get("colspan") or 1) > 1 for c in (s.get("cells") or [])))(
            parse_struct(e.get("table_structure"))))
    signature = hashlib.sha1(json.dumps([e.get("tag") for e in elements]
                                        + [parse_struct(e.get("table_structure")).get("column_count")
                                           for e in elements if e.get("block_type") == "table"]).encode()).hexdigest()[:16]
    return {"space": d.parent.name, "doc": d.name, "pages": pages, "elements": len(elements),
            "titles": sum(levels.values()), "levels": dict(levels), "level_kinds": len(levels),
            "paras": paras, "images": images, "tables": tables, "xp_tables": xp_tables,
            "max_span": max_span, "header_repeated": header_repeated, "merged_tables": merged_tables,
            "max_rows": max_rows, "xp_text": xp_text, "text_chars": text_chars, "max_cell_chars": max_cell,
            "empty_pages": max(0, pages - len(pages_seen)), "signature": signature}


def scan(workers: int) -> None:
    docs = sorted(str(d) for space in ROOT.iterdir() if space.is_dir() and space.name not in SKIP
                  for d in space.iterdir() if d.is_dir() and (d / "gt/gt_raw.json").is_file())
    print(f"扫描 {len(docs)} 篇，workers={workers}", flush=True)
    rows, done, errors = [], 0, 0
    with ProcessPoolExecutor(max_workers=workers) as pool, (OUT / "features.jsonl").open("w") as fh:
        for row in pool.map(document_features, docs, chunksize=16):
            done += 1
            if not row or row.get("error"):
                errors += 1
            else:
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            if done % 2000 == 0:
                print(f"  {done}/{len(docs)}（失败 {errors}）", flush=True)
    print(f"完成：可用 {len(rows)} 篇，失败 {errors} 篇", flush=True)


def select(target: int, seed: int) -> None:
    rows = [json.loads(line) for line in (OUT / "features.jsonl").read_text().splitlines() if line.strip()]
    rows = [r for r in rows if not r.get("error") and 2 <= r["pages"] <= 80]
    rng = random.Random(seed)

    def clean(r):
        return r["text_chars"] >= 400 and r["max_cell_chars"] <= 6000 and r["empty_pages"] == 0

    pool = [r for r in rows if clean(r)]
    selected: list[dict] = []
    taken: set[str] = set()
    sig_used: Counter = Counter()
    space_used: Counter = Counter()
    space_cap = int(target * 0.25)

    def take(candidates, n, reason):
        candidates = sorted(candidates, key=lambda r: (-r["xp_tables"], -r["tables"], rng.random()))
        added = 0
        for r in candidates:
            if added >= n:
                break
            if r["doc"] in taken or sig_used[r["signature"]] >= 2 or space_used[r["space"]] >= space_cap:
                continue
            taken.add(r["doc"]); sig_used[r["signature"]] += 1; space_used[r["space"]] += 1
            selected.append({**r, "bucket": reason}); added += 1
        print(f"  {reason:<22} {added:>4} 篇（候选 {len(candidates)}）")
        return added

    # 注：本语料 header_repeated 恒为 False（渲染器不重复表头），故不设该桶
    buckets = [
        ("xp_table_multi", lambda r: r["xp_tables"] >= 1 and r["max_span"] >= 3 and r["max_rows"] >= 6, 250),
        ("xp_table_merged", lambda r: r["xp_tables"] >= 1 and r["merged_tables"] >= 1, 150),
        ("xp_table_plain", lambda r: r["xp_tables"] >= 1, 120),
        ("xp_text", lambda r: r["xp_text"] >= 1, 100),
        ("short_doc", lambda r: 2 <= r["pages"] <= 6 and r["tables"] >= 1, 100),
        ("heading_rich", lambda r: r["titles"] >= 20 and r["level_kinds"] >= 3, 90),
        ("figure_rich", lambda r: r["images"] >= 8, 60),
        ("long_doc", lambda r: 40 <= r["pages"] <= 80, 50),
        ("table_dense", lambda r: r["tables"] >= 8, 40),
    ]
    for name, rule, quota in buckets:
        take([r for r in pool if rule(r)], quota, name)
    # 兜底：按 space 均衡补齐
    remaining = target - len(selected)
    if remaining > 0:
        take([r for r in pool if r["xp_tables"] >= 1 or r["tables"] >= 1], remaining, "fill_diverse")
    if len(selected) < target:
        take([r for r in pool if r["doc"] not in taken], target - len(selected), "fill_any")

    (OUT / "selection.json").write_text(json.dumps(
        {"target": target, "selected": len(selected), "seed": seed, "documents": selected}, ensure_ascii=False, indent=1))
    space_cnt = Counter(r["space"] for r in selected)
    print(f"\n最终 {len(selected)} 篇 | space 分布:", dict(space_cnt.most_common()))
    print("页数: 中位", statistics.median(r['pages'] for r in selected),
          "| 跨页表合计", sum(r['xp_tables'] for r in selected),
          "| 跨页文本篇", sum(1 for r in selected if r['xp_text']))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scan", action="store_true"); parser.add_argument("--select", action="store_true")
    parser.add_argument("--workers", type=int, default=max(4, (os.cpu_count() or 8) // 2))
    parser.add_argument("--target", type=int, default=1000); parser.add_argument("--seed", type=int, default=20260921)
    args = parser.parse_args()
    ROOT, OUT = args.input_root, args.output
    OUT.mkdir(parents=True, exist_ok=True)
    if args.workers < 1 or args.target < 1:
        parser.error("workers and target must be positive")
    if args.scan:
        scan(args.workers)
    if args.select:
        select(args.target, args.seed)
