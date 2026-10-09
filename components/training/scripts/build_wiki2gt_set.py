"""把 wiki2gt 选样做成 SFT 输入集（**只建软链**，不复制数据）。

产物结构：
  <out>/
  ├── registry.json                      # 样本清单（sample_id = "<SPACE>/<doc>"）
  ├── README.md                          # 输入侧约定
  └── docs/<SPACE>/<doc>/
      ├── gt        -> <源>/<SPACE>/<doc>/gt
      ├── page_images -> <源>/<SPACE>/<doc>/ocr_agent/page_images
      └── document.pdf -> <源>/<SPACE>/<doc>/render/document.pdf
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

SOURCE = Path("data/wiki2gt")

README = """# wiki2gt SFT 输入集（只读软链）

- 单元：`docs/<SPACE>/<doc>/`，全部是**指向源语料的软链**，本目录零数据占用。
- 标签来源：`gt/`（`gt_raw.json` 结构+标签树+表结构、`gt_bundle.json` 文本/顺序/跨页片段、
  `canonical_doc_graph.json` 节点类型、`page_geometry.json` 页数）。
- 页图输入：`page_images/page_XXXX.png`（150 dpi，1241×1754）。
- 清单：`registry.json`（每篇带 `pages / tables / xp_tables / xp_text / bucket`）。
- 源语料为命令行 `--source` 指定的只读目录。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pdf", action="store_true", help="额外软链 document.pdf（默认不建，省遍历）")
    args = parser.parse_args()

    selection = json.loads(args.selection.read_text())
    documents, missing = [], []
    for row in selection["documents"]:
        space, doc = row["space"], row["doc"]
        src = args.source / space / doc
        if not (src / "gt/gt_raw.json").is_file() or not (src / "ocr_agent/page_images").is_dir():
            missing.append(f"{space}/{doc}")
            continue
        target = args.out / "docs" / space / doc
        target.mkdir(parents=True, exist_ok=True)
        for name, origin in (("gt", src / "gt"), ("page_images", src / "ocr_agent/page_images")):
            link = target / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(origin)
        if args.pdf:
            link = target / "document.pdf"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(src / "render/document.pdf")
        documents.append({"sample_id": f"{space}/{doc}", "space": space, "doc": doc,
                          "pages": row["pages"], "tables": row["tables"], "xp_tables": row["xp_tables"],
                          "xp_text": row["xp_text"], "images": row["images"], "titles": row["titles"],
                          "bucket": row.get("bucket"), "gt_dir": f"docs/{space}/{doc}/gt",
                          "image_dir": f"docs/{space}/{doc}/page_images"})
    registry = {"dataset": "wiki2gt", "selection": args.selection.name, "source_root": str(args.source),
                "physical_at": str(args.out), "count": len(documents), "documents": documents}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=1))
    (args.out / "README.md").write_text(README)

    buckets, spaces = Counter(d["bucket"] for d in documents), Counter(d["space"] for d in documents)
    pages = sum(d["pages"] for d in documents)
    print(f"✅ {len(documents)} 篇 / {pages:,} 页 → {args.out}")
    if missing:
        print(f"   ⚠️ 缺失 {len(missing)} 篇（未链接）: {missing[:5]}")
    print("   桶分布:", dict(buckets.most_common()))
    print("   space:", dict(spaces.most_common()))
    print(f"   跨页表 {sum(d['xp_tables'] for d in documents):,} · 跨页文本篇 "
          f"{sum(1 for d in documents if d['xp_text'])} · 图 {sum(d['images'] for d in documents):,}")


if __name__ == "__main__":
    main()
