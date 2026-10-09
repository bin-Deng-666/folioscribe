"""Measure CPU projection on a synthetic long document; does not measure model quality."""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

from sft.output_format import CONTRACT_VERSION, PROMPT_TEMPLATE_VERSION
from sft.projection import project


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=300)
    parser.add_argument("--blocks", type=int, default=20)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if min(args.pages, args.blocks, args.repeat) < 1:
        parser.error("all counts must be positive")
    with tempfile.TemporaryDirectory() as tmp:
        doc = Path(tmp)
        gt = doc / "gt"
        gt.mkdir()
        elements, nodes, entities = [], [], []
        for page in range(1, args.pages + 1):
            for order in range(args.blocks):
                index = len(elements)
                node = f"node-{index}"
                elements.append({"block_type": "paragraph"})
                nodes.append({"node_id": node, "node_type": "paragraph",
                              "source_locator": f"gt_raw.json#elements/{index}"})
                entities.append({"node_id": node, "page_number": page, "reading_order": order,
                                 "text": f"Synthetic paragraph {index}: " + "content " * 20})
        for name, payload in {
            "gt_raw.json": {"elements": elements}, "gt_bundle.json": {"a1_entities": entities},
            "canonical_doc_graph.json": {"nodes": nodes},
            "page_geometry.json": [{"page_number": p, "page_box": [0, 0, 600, 800]}
                                   for p in range(1, args.pages + 1)],
        }.items():
            (gt / name).write_text(json.dumps(payload), encoding="utf-8")
        durations = []
        for _ in range(args.repeat):
            start = time.perf_counter()
            samples = project(doc, "synthetic", "benchmark")
            durations.append(time.perf_counter() - start)
            assert len(samples) == args.pages
        median = statistics.median(durations)
        print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                          "pages": args.pages, "blocks_per_page": args.blocks,
                          "median_seconds": round(median, 4), "runs_seconds": durations,
                          "pages_per_second": round(args.pages / median, 1),
                          "contract": CONTRACT_VERSION, "prompt": PROMPT_TEMPLATE_VERSION}, indent=2))


if __name__ == "__main__":
    main()
