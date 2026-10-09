"""wiki2gt → SFT 样本投影（v3 DSL 格式）。

input  = 页图 + 前一页结束后的 <title_tree> + <prev_page_tail>
target = 当页 DSL：六类型标记 + Markdown 内容 + <|loc|> + <|truncated|>/<|continued|>

与推理共用同一契约：build_title_tree / build_prev_tail / derive_heading_level /
blocks_to_dsl / parse_blocks 都来自 sft/output_format.py，禁止另写一套。

用法：
  folioscribe-project --input-root data/training-demo --show-state
  folioscribe-project --input-root data/training-demo --roundtrip
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import re
from pathlib import Path

from .output_format import (
    build_prompt,
    blocks_to_dsl,
    derive_heading_level,
    parse_blocks,
)
from .markdown import bbox_0_1000, normalize_text
from .document_state import DocState

CELL_OWNER = re.compile(r"(?P<table>.+)-r(?P<row>\d+)c(?P<col>\d+)$")


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


def table_html(struct, page_idx, carry_header: dict | None, cells_on_page: dict | None = None):
    """当页表格片段 → HTML（带 rowspan/colspan；首片段记为表头行）。"""
    if cells_on_page:
        cells = []
        for cell in struct.get("cells") or []:
            key = (int(cell["row"]), int(cell["col"]))
            if key in cells_on_page:
                merged = dict(cell)
                merged["text"] = cells_on_page[key] or cell.get("text") or ""
                cells.append(merged)
    else:
        cells = [c for c in struct.get("cells") or []
                 if any(b.get("page_idx") == page_idx for b in (c.get("boxes") or []))]
    kept, covered = [], set()
    for cell in sorted(cells, key=lambda c: (int(c["row"]), int(c["col"]))):
        position = (int(cell["row"]), int(cell["col"]))
        if position in covered:
            continue
        kept.append(cell)
        for dr in range(int(cell.get("rowspan") or 1)):
            for dc in range(int(cell.get("colspan") or 1)):
                if dr or dc:
                    covered.add((position[0] + dr, position[1] + dc))
    cells = kept
    if not cells:
        return "", 0, []
    rows = sorted({c["row"] for c in cells})
    grid = {}
    for cell in cells:
        grid[(cell["row"], cell["col"])] = cell
    out = ["<table>"]
    header_rows: list[int] = []
    if carry_header:
        for r, cols in sorted(carry_header.items()):
            out.append("<tr>" + "".join(f"<th>{html.escape(cols[c])}</th>" for c in sorted(cols)) + "</tr>")
    for r in rows:
        if r in (carry_header or {}):
            continue
        tds = []
        for c in sorted(k for (rr, k) in grid if rr == r):
            cell = grid[(r, c)]
            text = html.escape(normalize_text(cell.get("text") or ""))
            span = (f' rowspan="{cell["rowspan"]}"' if int(cell.get("rowspan") or 1) > 1 else "") + \
                   (f' colspan="{cell["colspan"]}"' if int(cell.get("colspan") or 1) > 1 else "")
            tag = "th" if cell.get("tag") == "th" or r == rows[0] else "td"
            tds.append(f"<{tag}{span}>{text}</{tag}>")
            if tag == "th":
                header_rows.append(r)
        out.append("<tr>" + "".join(tds) + "</tr>")
    out.append("</table>")
    return "\n".join(out), len(rows), sorted(set(header_rows))


def project(doc_dir: Path, space: str, doc: str, pages_limit: int | None = None,
            show_state: bool = False) -> list[dict]:
    gt = doc_dir / "gt"
    elements = json.loads((gt / "gt_raw.json").read_text())["elements"]
    bundle = json.loads((gt / "gt_bundle.json").read_text())
    graph = json.loads((gt / "canonical_doc_graph.json").read_text())
    geometry = json.loads((gt / "page_geometry.json").read_text())
    total = len(geometry)
    page_boxes = {int(g["page_number"]): g.get("page_box") for g in geometry}
    node_type = {n["node_id"]: n["node_type"] for n in graph["nodes"]}
    elem_of = {n["node_id"]: int(n["source_locator"].rsplit("/", 1)[1]) for n in graph["nodes"]
               if n.get("source_locator", "").startswith("gt_raw.json#elements/")}
    by_node: dict[str, list] = {}
    cells_by_page: dict[tuple[int, str, int, int], str] = {}
    for entity in bundle["a1_entities"]:
        by_node.setdefault(entity["node_id"], []).append(entity)
        owner = CELL_OWNER.fullmatch(entity.get("node_id") or "")
        if owner:
            cells_by_page[(entity["page_number"], owner.group("table"),
                           int(owner.group("row")), int(owner.group("col")))] = entity.get("text") or ""

    page_nodes: dict[int, list[tuple[str, str, list[dict]]]] = {}
    for node_id, kind in node_type.items():
        by_page: dict[int, list[dict]] = {}
        for entity in by_node.get(node_id, []):
            by_page.setdefault(entity["page_number"], []).append(entity)
        for page_number, entities in by_page.items():
            page_nodes.setdefault(page_number, []).append((node_id, kind, entities))
    table_cells: dict[tuple[int, str], dict[tuple[int, int], str]] = {}
    for (page_number, table_id, row, col), text in cells_by_page.items():
        table_cells.setdefault((page_number, table_id), {})[(row, col)] = text

    xp_pages, headers = {}, {}
    for index, element in enumerate(elements):
        if element.get("block_type") != "table":
            continue
        cont = element.get("continuation") or {}
        xp_pages[index] = [p + 1 for p in (cont.get("pages") or [])]
        headers[index] = {}

    state = DocState()
    samples = []
    if pages_limit is not None and pages_limit < 1:
        raise ValueError("pages_limit must be positive")
    for page in range(1, min(pages_limit or total, total) + 1):
        context = state.context_for(page)
        tree_slot = context["title_tree"]
        tail_slot = context["prev_page_tail"]
        open_tree = [dict(node) for node in tree_slot["open_tree"]]
        open_block = state.open_block
        page_box = page_boxes.get(page) or [0.0, 0.0, 595.0, 842.0]
        pw, ph = page_box[2], page_box[3]
        raw_blocks = []
        for node_id, kind, entities in page_nodes.get(page, []):
            if kind not in ("heading", "paragraph", "table", "image"):
                continue
            index = elem_of.get(node_id)
            element = elements[index] if index is not None else {}
            order = min(e["reading_order"] for e in entities)
            if kind == "table":
                struct = parse_struct(element.get("table_structure"))
                pages_of_table = xp_pages.get(index) or [page]
                position = pages_of_table.index(page) + 1 if page in pages_of_table else 1
                carry = headers.get(index) if (element.get("continuation") or {}).get("header_repeated") else None
                page_cells = table_cells.get((page, node_id), {})
                html, n_rows, hdr = table_html(struct, page - 1, carry, page_cells)
                if position == 1:
                    headers[index] = {}
                    first_cells = {c["row"]: {cc["col"]: normalize_text(cc.get("text") or "")
                                              for cc in struct.get("cells") or [] if cc["row"] == c["row"]}
                                   for c in struct.get("cells") or []
                                   if any(b.get("page_idx") == page - 1 for b in (c.get("boxes") or []))}
                    headers[index] = {r: cols for r, cols in list(first_cells.items())[:1]}
                frag = next((f for f in (element.get("fragments") or []) if f.get("page_idx") == page - 1), None)
                bbox = bbox_0_1000(frag.get("bbox_pt"), pw, ph) if frag else None
                raw_blocks.append({"order": order, "dsl_type": "table", "content": html,
                                   "continued": position > 1, "truncated": position < len(pages_of_table),
                                   "page": page - 1, "bbox": bbox})
            else:
                text = normalize_text(" ".join((e.get("text") or "") for e in sorted(entities, key=lambda x: x["reading_order"])))
                if not text and kind != "image":
                    continue
                if kind == "heading":
                    raw_blocks.append({"order": order, "dsl_type": "title", "content": text,
                                       "raw_level": int(element.get("tag", "h1")[1:] or 1),
                                       "continued": False, "truncated": False, "page": page - 1})
                elif kind == "image":
                    frag = next((f for f in (element.get("fragments") or []) if f.get("page_idx") == page - 1), None)
                    bbox = bbox_0_1000(frag.get("bbox_pt"), pw, ph) if frag else None
                    raw_blocks.append({"order": order, "dsl_type": "image", "content": "",
                                       "continued": False, "truncated": False, "page": page - 1,
                                       "bbox": bbox})
                else:
                    is_xp = bool(element.get("cross_page"))
                    pages_of = [f.get("page_idx") + 1 for f in (element.get("fragments") or [])]
                    position = pages_of.index(page) + 1 if page in pages_of else 1
                    raw_blocks.append({"order": order, "dsl_type": "text", "content": text,
                                       "continued": is_xp and position > 1,
                                       "truncated": is_xp and position < len(pages_of),
                                       "page": page - 1})
        raw_blocks.sort(key=lambda b: b["order"])

        # 目标块流：标题层级推导 + 续块只在上一块存在时保留 continued
        target_blocks: list[dict] = []
        for block in raw_blocks:
            out = {"type": block["dsl_type"], "content": block["content"],
                   "page": block["page"], "truncated": block["truncated"]}
            if block.get("bbox") and block["dsl_type"] in ("table", "image"):
                out["bbox"] = block["bbox"]
            if block["dsl_type"] == "title":
                level = derive_heading_level(open_tree, block["raw_level"])
                out["level"] = level
            if block["continued"] and open_block is not None and open_block["type"] == block["dsl_type"]:
                out["continued"] = True
            elif block["continued"]:
                out["continued"] = False  # 无可接续块：视为新块（语义回退）
            target_blocks.append(out)
            # 状态演化
            if out["type"] == "title":
                while open_tree and open_tree[-1]["level"] >= level:
                    open_tree.pop()
                open_tree.append({"level": level, "title": out["content"]})

        target_dsl = blocks_to_dsl(target_blocks)
        # Build the prompt from the state BEFORE consuming this page's target.
        # The page line uses the same zero-based id as the DSL loc metadata.
        prompt = build_prompt(page - 1, total, tree_slot, tail_slot)
        state.ingest(page, target_dsl)
        item = {"sample_id": f"{space}/{doc}", "page": page, "total_pages": total,
                "image": str(doc_dir / "page_images" / f"page_{page:04d}.png"),
                "prompt": prompt, "target": target_dsl,
                "head": next((f"{b['type']}_from_prev" for b in target_blocks if b.get("continued")), None),
                "tail": next((f"{b['type']}_to_next" for b in reversed(target_blocks) if b.get("truncated")), None),
                "title_tree": tree_slot, "prev_tail": tail_slot, "blocks": len(target_blocks)}
        if show_state:
            item["state_after"] = {
                **state.state_snapshot(),
                "open_block_type": state.open_block["type"] if state.open_block else None,
            }
        samples.append(item)
    return samples


def roundtrip_check(samples: list[dict]) -> list[str]:
    """解析回读自检：target DSL -> parse_blocks -> blocks_to_dsl 必须逐字还原。"""
    issues = []
    for item in samples:
        first = blocks_to_dsl(parse_blocks(item["target"]))
        if first != item["target"]:
            issues.append(f"p{item['page']}: roundtrip mismatch")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--sample", default="", help="<SPACE>/<doc>；缺省取 registry 第一篇")
    parser.add_argument("--pages", default="", help="如 1-3（缺省全部）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--show-state", action="store_true")
    parser.add_argument("--roundtrip", action="store_true", help="跑解析回读自检")
    args = parser.parse_args()

    registry = json.loads((args.input_root / "registry.json").read_text())["documents"]
    wanted = [args.sample] if args.sample else [d["sample_id"] for d in registry][: (args.limit or 1)]
    pages_limit = None
    if args.pages:
        lo, _, hi = args.pages.partition("-")
        pages_limit = int(hi or lo)
    for sample_id in wanted:
        space, doc = sample_id.split("/", 1)
        samples = project(args.input_root / "docs" / space / doc, space, doc, pages_limit, args.show_state)
        print(f"\n{'=' * 78}\n样本 {sample_id}（{len(samples)} 页）")
        for item in samples[: int(args.limit or 2)]:
            print(f"\n--- 第 {item['page']}/{item['total_pages']} 页 | head={item['head']} tail={item['tail']} | 块 {item['blocks']}")
            print("【title_tree】")
            print(json.dumps(item["title_tree"], ensure_ascii=False)[:420])
            print("【prev_tail】")
            print((item["prev_tail"] or "<empty/>")[:260])
            print("【target DSL】")
            print(item["target"][:800])
        if args.roundtrip:
            issues = roundtrip_check(samples)
            print(f"\n回读自检: {'PASS 全部一致' if not issues else issues[:5]}")


if __name__ == "__main__":
    main()
