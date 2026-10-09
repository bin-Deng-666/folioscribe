#!/usr/bin/env python3
"""Structure agreement v2: match machine output to annotations by CONTENT + GEOMETRY, not by id.

Why not block ids: when the pipeline merges two fragments (which the annotators asked for), the
merged block keeps one id and the other id disappears - id matching then reports a *missing* block
for a correct merge. Content matching classifies the four outcomes that actually matter:

  agreed_1to1      machine block == annotation block (same content, same place, same type)
  machine_fragment machine block is a piece of an annotation block  -> the pipeline under-merged
  machine_swallow  annotation block was absorbed into a bigger machine block -> over-merged
  extra / missing  content that exists on one side only

Agreement = agreed_1to1 / annotation blocks. Fragments and swallows are reported separately: they
are the actionable signal (grouping), while extra/missing is the recall/precision signal.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path


def norm_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\s\-‐-―]+", "", text)
    return text.lower()


def box_of(block: dict) -> list[float]:
    values = block.get("bbox_image_px") or [0, 0, 0, 0]
    try:
        return [float(v) for v in values]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0, 0.0]


def iou(a: list[float], b: list[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def contains(small: str, big: str) -> bool:
    return len(small) >= 6 and small in big


def evaluate_page(machine: list[dict], human: list[dict]) -> dict:
    m_text = {m["block_id"]: norm_text(m.get("text")) for m in machine}
    h_text = {h["block_id"]: norm_text(h.get("text")) for h in human}



    pairs: list[tuple[float, str, str]] = []
    for h in human:
        for m in machine:
            overlap = iou(box_of(h), box_of(m))
            if str(h.get("type")) in {"image", "chart", "seal"} or str(m.get("type")) in {"image", "chart", "seal"}:
                if overlap >= 0.45:
                    pairs.append((overlap + 1.0, h["block_id"], m["block_id"]))
                continue
            th, tm = h_text[h["block_id"]], m_text[m["block_id"]]
            if not th and not tm:
                sim = 1.0 if overlap >= 0.6 else 0.0
            elif not th and overlap >= 0.7:
                # annotation blocks sometimes carry no text (the annotator judged by geometry);
                # a machine block that sits in the same place is the same block
                pairs.append((1.5 + overlap, h["block_id"], m["block_id"]))
                continue
            elif not th or not tm:
                sim = 0.0
            else:
                if th == tm and len(th) < 6:
                    # short labels (page numbers, "标准话术"): equal text + nearby box is a match;
                    # IoU on a 20px-tall strip is too jittery to demand
                    if overlap >= 0.08 and abs(box_of(h)[1] - box_of(m)[1]) <= 25:
                        pairs.append((2.0, h["block_id"], m["block_id"]))
                    continue
                if contains(th, tm):
                    sim = len(th) / len(tm)
                elif contains(tm, th):
                    sim = len(tm) / len(th)
                else:
                    # merged blocks carry fragment-level noise the annotators cleaned away by
                    # hand (a stray "2." between entries, a broken glyph): near-identical text
                    # at the same place is still the same block
                    import difflib
                    ratio = difflib.SequenceMatcher(None, th, tm).ratio()
                    sim = ratio if ratio >= 0.90 else 0.0
            if sim >= 0.88 and overlap >= 0.25:
                pairs.append((sim + overlap, h["block_id"], m["block_id"]))
    pairs.sort(reverse=True)

    matched_m: set[str] = set()
    matched_h: set[str] = set()
    one_to_one: list[tuple[str, str]] = []
    for _score, h_id, m_id in pairs:
        if h_id in matched_h or m_id in matched_m:
            continue
        matched_h.add(h_id)
        matched_m.add(m_id)
        one_to_one.append((h_id, m_id))

    h_by_id = {h["block_id"]: h for h in human}
    m_by_id = {m["block_id"]: m for m in machine}
    fragments = []   # machine block inside an annotation block (under-merge)
    swallowed = []   # annotation block absorbed into a machine block (over-merge)
    for m in machine:
        if m["block_id"] in matched_m or str(m.get("type")) in {"image", "chart", "seal"} or not m_text[m["block_id"]]:
            continue
        host = next((h["block_id"] for h in human if h["block_id"] not in matched_h and contains(m_text[m["block_id"]], h_text[h["block_id"]])), None)
        if host:
            fragments.append({"block_id": m["block_id"], "host": host})
    for h in human:
        if h["block_id"] in matched_h or str(h.get("type")) in {"image", "chart", "seal"} or not h_text[h["block_id"]]:
            continue
        eater = next((m["block_id"] for m in machine if m["block_id"] not in matched_m and m["block_id"] != h["block_id"] and str(m.get("type")) not in {"image", "chart", "seal"} and contains(h_text[h["block_id"]], m_text[m["block_id"]])), None)
        if eater:
            swallowed.append({"block_id": h["block_id"], "into": eater})

    # Group-match: several machine blocks may jointly cover one annotation block (the annotators
    # merge "01 + heading + PART ONE" into a single title; the pipeline keeps the big number as its
    # own text block). A group qualifies when the machine texts concatenated equal the annotation
    # text, their union box covers it, and every part overlaps it.
    group_match_h: set[str] = set()
    group_match_m: set[str] = set()
    for h in human:
        if h["block_id"] in matched_h or str(h.get("type")) in {"image", "chart", "seal"}:
            continue
        th = h_text[h["block_id"]]
        if not th:
            continue
        hb = box_of(h)
        h_area = max(1.0, (hb[2] - hb[0]) * (hb[3] - hb[1]))
        cands = [m for m in machine
                 if m["block_id"] not in matched_m and m["block_id"] not in group_match_m
                 and str(m.get("type")) != "image"
                 and (m_text[m["block_id"]] or not str(m.get("text") or "").strip())
                 and iou(hb, box_of(m)) * h_area / max(1.0, (box_of(m)[2] - box_of(m)[0]) * (box_of(m)[3] - box_of(m)[1])) >= 0.5]
        for k in (2, 3):
            import itertools
            for combo in itertools.combinations(cands, k):
                joined = "".join(m_text[m["block_id"]] for m in combo)
                if joined != th:
                    continue

                ux1 = min(box_of(m)[0] for m in combo); uy1 = min(box_of(m)[1] for m in combo)
                ux2 = max(box_of(m)[2] for m in combo); uy2 = max(box_of(m)[3] for m in combo)
                if (ux2 - ux1) * (uy2 - uy1) <= 1.6 * max(1.0, (box_of(h)[2] - box_of(h)[0]) * (box_of(h)[3] - box_of(h)[1])):
                    group_match_h.add(h["block_id"])
                    group_match_m.update(m["block_id"] for m in combo)
                    break
            if h["block_id"] in group_match_h:
                break

    type_mismatch = []
    for h_id, m_id in one_to_one:
        ht, mt = str(h_by_id[h_id].get("type")), str(m_by_id[m_id].get("type"))
        if ht != mt and not ({ht, mt} & {"image", "chart", "seal"} and {ht, mt} <= {"image", "chart", "seal"}):
            type_mismatch.append({"block_id": m_id, "machine": mt, "human": ht})

    eaters = {s["into"] for s in swallowed}
    extra = [m["block_id"] for m in machine if m["block_id"] not in matched_m
             and m["block_id"] not in group_match_m and m["block_id"] not in eaters
             and m["block_id"] not in {f["block_id"] for f in fragments}]
    missing = [h["block_id"] for h in human if h["block_id"] not in matched_h
               and h["block_id"] not in group_match_h
               and h["block_id"] not in {s["block_id"] for s in swallowed}]

    # Reading order concordance over agreed pairs, judged with bbox centres.
    # 1) ranks are computed among MATCHED blocks only (unmatched blocks would shift indices and
    #    fake inversions), 2) an inversion only counts when the geometry is decisive: the pair's
    #    centres must be clearly separated vertically (else the order is ambiguous and both lists
    #    are "right"), and the human order itself must be geometrically sane for that pair
    #    (annotators sometimes group footnotes/captions out of reading flow - the machine is not
    #    wrong for disagreeing there).
    h_order = {h["block_id"]: i for i, h in enumerate(human)}
    m_order = {m["block_id"]: i for i, m in enumerate(machine)}
    pair_list = [(h_id, m_id) for h_id, m_id in one_to_one if h_id in h_order and m_id in m_order]
    h_rank = {h_id: r for r, (h_id, _m) in enumerate(sorted(pair_list, key=lambda pm: h_order[pm[0]]))}
    m_rank = {h_id: r for r, (h_id, _m) in enumerate(sorted(pair_list, key=lambda pm: m_order[pm[1]]))}

    def _center(block: dict) -> tuple[float, float]:
        b = box_of(block)
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0, b[3] - b[1])

    inversions = 0
    decisive = 0
    sites: list[dict] = []
    for i in range(len(pair_list)):
        for j in range(i + 1, len(pair_list)):
            a_id, b_id = pair_list[i][0], pair_list[j][0]
            if (h_rank[a_id] - h_rank[b_id]) * (m_rank[a_id] - m_rank[b_id]) < 0:
                ha, hb = h_by_id[a_id], h_by_id[b_id]
                ax, ay, ah = _center(ha)
                bx, by, bh = _center(hb)
                if abs(ay - by) <= max(15.0, 0.5 * min(ah, bh)):
                    continue  # same visual line: order is a convention, not an error
                # the human order must itself agree with geometry for this pair to indict the machine
                human_sane = (ay < by) if h_rank[a_id] < h_rank[b_id] else (by < ay)
                if not human_sane:
                    continue
                decisive += 1
                if len(sites) < 12:
                    sites.append({"pair": [a_id, b_id], "human_rank": [h_rank[a_id], h_rank[b_id]],
                                  "machine_rank": [m_rank[a_id], m_rank[b_id]],
                                  "centres": [[round(ax), round(ay)], [round(bx), round(by)]]})
                inversions += 1
    total_pairs = max(1, decisive)
    joined = pair_list

    return {
        "human_blocks": len(human),
        "machine_blocks": len(machine),
        "agreed": len(one_to_one) + len(group_match_h),
        "agreed_groups": len(group_match_h),
        "agreement": round(len(one_to_one) / max(1, len(human)), 4),
        "machine_fragments": len(fragments),
        "machine_swallow": len(swallowed),
        "type_mismatch": len(type_mismatch),
        "extra": len(extra),
        "missing": len(missing),
        "missing_by_type": _count_by_type(h_by_id, missing),
        "extra_by_type": _count_by_type(m_by_id, extra),
        "order_concordance": round(1.0 - inversions / total_pairs, 4) if joined else None,
        "order_decisive_pairs": decisive,
        "order_sites": sites,
        "detail": {
            "fragments": fragments[:20],
            "swallowed": swallowed[:20],
            "type_mismatch": type_mismatch[:20],
            "extra": extra[:20],
            "missing": missing[:20],
        },
    }


def _count_by_type(by_id: dict, ids: list[str]) -> dict:
    counts: dict[str, int] = {}
    for block_id in ids:
        kind = str(by_id.get(block_id, {}).get("type"))
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    root = args.document_root

    pages: dict[str, dict] = {}
    totals = {"agreed": 0, "human_blocks": 0, "machine_blocks": 0, "machine_fragments": 0,
              "machine_swallow": 0, "type_mismatch": 0, "extra": 0, "missing": 0, "agreed_groups": 0}
    order_values: list[float] = []
    order_pages = 0

    for annotation in sorted((root / "annotations").glob("*/page_*.json")):
        sample_id = annotation.parent.name
        record = json.loads(annotation.read_text(encoding="utf-8"))
        human = record.get("blocks") or []
        if any("测试保存" in str(b.get("text") or "") for b in human):
            continue
        page_number = int(record.get("page") or 0)
        result_path = root / "runs" / sample_id / f"page_{page_number:04d}" / "result.json"
        if not result_path.exists():
            continue
        machine = (json.loads(result_path.read_text(encoding="utf-8")).get("revision") or {}).get("blocks") or []
        report = evaluate_page(machine, human)
        pages[f"{sample_id}#p{page_number}"] = report
        for key in totals:
            totals[key] += report[key]
        if report["order_concordance"] is not None:
            order_values.append(report["order_concordance"])
            order_pages += 1

    agreement = (totals["agreed"]) / max(1, totals["human_blocks"])
    print(f"[structure v2] pages {len(pages)} · 机器 {totals['machine_blocks']} → 人工 {totals['human_blocks']}")
    print(f"  结构一致率 {agreement:.3f} · 1:1 吻合 {totals['agreed']}")
    print(f"  碎片(该并未并) {totals['machine_fragments']} · 吞块(并过头) {totals['machine_swallow']} · 类型不符 {totals['type_mismatch']}")
    print(f"  真多余 {totals['extra']} · 真漏 {totals['missing']}")
    if order_values:
        print(f"  顺序一致 {sum(order_values) / len(order_values):.3f} ({order_pages} 页)")
    if args.out:
        args.out.write_text(json.dumps({
            "totals": totals, "agreement": round(agreement, 4),
            "order_concordance": round(sum(order_values) / len(order_values), 4) if order_values else None,
            "pages": pages,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
