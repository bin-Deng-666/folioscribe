"""逐页对拍：投影样本 vs wiki2gt GT 的跨页表事实。"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sft.projection import CELL_OWNER, project  # noqa: E402

ROOT = Path("data/wiki2gt")


def gt_facts(doc_dir: Path):
    bundle = json.loads((doc_dir / "gt/gt_bundle.json").read_text())
    elements = json.loads((doc_dir / "gt/gt_raw.json").read_text())["elements"]
    # 权威口径：整页单元格 = a1_entities 里该页的全部 cell 实体
    # （fragment_continuations 只登记"跨页表"，单页表不在其中，不能当整页期望值）
    page_cells, page_rows = {}, {}
    for entity in bundle.get("a1_entities") or []:
        owner = CELL_OWNER.fullmatch(entity.get("node_id") or "")
        if not owner:
            continue
        page = int(entity.get("page_number") or 0)
        page_cells.setdefault(page, set()).add(entity["entity_id"])
        page_rows.setdefault(page, set()).add(owner.group("table"))
    table_pages, text_pages = set(), set()
    for element in elements:
        cont = element.get("continuation") or {}
        pages = {int(idx) + 1 for idx in (cont.get("pages") or [])}
        if element.get("block_type") == "table":
            table_pages |= pages
        if element.get("cross_page"):
            text_pages |= pages or {int(f.get("page_idx", 0)) + 1 for f in (element.get("fragments") or [])}
    return page_cells, page_rows, table_pages, text_pages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--pages", type=int, default=30)
    parser.add_argument("--demo", type=int, default=3)
    parser.add_argument("--bucket", default="", help="按选样桶过滤，如 xp_table_merged")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    global ROOT
    ROOT = args.input_root
    registry = json.loads((ROOT / "registry.json").read_text())["documents"]
    pool = [d for d in registry if not args.bucket or d.get("bucket") == args.bucket]
    picks = pool[args.offset: args.offset + args.n]
    label = args.bucket or "xp_tables>=1"
    print(f"[{label}] 抽 {len(picks)} 篇（每篇前 {args.pages} 页，offset {args.offset}）")
    if not args.quiet:
        print(f"{'文档':<22}{'页':>4}{'表页':>6}{'事件对':>8}{'单元格对':>9}{'表体空':>7}  结论")
    bad_total = 0
    for index, item in enumerate(picks):
        space, doc = item["sample_id"].split("/", 1)
        samples = project(ROOT / "docs" / space / doc, space, doc, args.pages)
        doc_dir = ROOT / "docs" / space / doc
        page_cells, _, table_pages, text_pages = gt_facts(doc_dir)
        ev_ok = ev_all = cell_ok = cell_all = empty_tables = 0
        sample_out = []
        for sample in samples:
            body = sample["target"]
            has_table = "<table>" in body
            for event, gt_page in ((sample["head"], sample["page"]), (sample["tail"], sample["page"])):
                if event:
                    ev_all += 1
                    ev_ok += gt_page in table_pages or gt_page in text_pages
            if has_table:
                cells = len(re.findall(r"<t[dh][ >]", body))
                expected = len(page_cells.get(sample["page"], ()))
                cell_all += 1
                cell_ok += cells == expected
                empty_tables += cells == 0
                sample_out.append((sample["page"], cells, expected, sample["head"], sample["tail"]))
        flag = "✅" if ev_all == ev_ok and cell_ok == cell_all and not empty_tables else "❌"
        bad_total += flag == "❌"
        if not args.quiet:
            print(f"{item['sample_id']:<22}{len(samples):>4}{sum(1 for s in samples if '<table>' in s['target']):>6}"
                  f"{ev_ok}/{ev_all:<6}{cell_ok}/{cell_all:<7}{empty_tables:>7}  {flag}")
        if flag == "❌" and index < args.demo:
            for row in sample_out[:4]:
                print(f"     p{row[0]}: 格 {row[1]}(GT {row[2]}) head={row[3]} tail={row[4]}")
    print(f"\n不通过 {bad_total}/{len(picks)} 篇")


if __name__ == "__main__":
    main()
