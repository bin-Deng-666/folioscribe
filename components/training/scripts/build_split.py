"""1000 篇切分（分层：桶 × space，9:1）+ 导出训练格式（ms-swift：messages + images）。"""
from __future__ import annotations
import argparse, json, sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sft.projection import project  # noqa: E402

ROOT = Path("data/wiki2gt")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--test-ratio", type=float, default=0.1)
    parser.add_argument("--dump-swift", type=Path, default=None)
    parser.add_argument("--swift-docs", type=int, default=3)
    args = parser.parse_args()
    global ROOT
    ROOT = args.input_root
    if not 0 < args.test_ratio < 1:
        parser.error("--test-ratio must be between 0 and 1")
    docs = json.loads((ROOT / "registry.json").read_text())["documents"]
    groups: dict[tuple, list] = defaultdict(list)
    for doc in docs:
        groups[(doc.get("bucket"), doc["space"])].append(doc)
    train, test = [], []
    for key in sorted(groups, key=str):
        items = sorted(groups[key], key=lambda d: d["sample_id"])
        cut = max(1, round(len(items) * args.test_ratio))
        test.extend(items[:cut]); train.extend(items[cut:])
    split = {"seed": "bucket×space 分层，字典序前 10% 进测试",
             "train": [d["sample_id"] for d in train], "test": [d["sample_id"] for d in test]}
    (ROOT / "split.json").write_text(json.dumps(split, ensure_ascii=False, indent=1))
    print(f"切分：训练 {len(train)} 篇 / 测试 {len(test)} 篇（比例 {args.test_ratio:.0%}）")
    print("  测试集 space 分布:", dict(Counter(d["space"] for d in test).most_common()))
    print("  测试集桶分布:", dict(Counter(d.get("bucket") for d in test).most_common()))
    print(f"  → {ROOT/'split.json'}")

    if args.dump_swift:
        out, pages, table_pages, ev = [], 0, 0, 0
        for doc in train[: args.swift_docs]:
            space, name = doc["sample_id"].split("/", 1)
            for sample in project(ROOT / "docs" / space / name, space, name):
                out.append({"messages": [{"role": "user", "content": "<image>" + sample["prompt"]},
                                         {"role": "assistant", "content": sample["target"]}],
                            "images": [sample["image"]]})
                pages += 1; table_pages += "<table>" in sample["target"]; ev += bool(sample["head"] or sample["tail"])
        args.dump_swift.parent.mkdir(parents=True, exist_ok=True)
        args.dump_swift.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n")
        print(f"  训练格式导出 {len(out)} 条 / {pages} 页（含表 {table_pages}、有事件 {ev}）→ {args.dump_swift}")


if __name__ == "__main__":
    main()
