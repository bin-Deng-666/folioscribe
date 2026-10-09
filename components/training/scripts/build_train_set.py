"""Build ms-swift JSONL datasets from wiki2gt inputs using the shared v3 state machine."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from sft.output_format import CONTRACT_VERSION, IMAGE_SPEC_VERSION, PROMPT_TEMPLATE_VERSION
from sft.projection import project, roundtrip_check


def build(root: Path, output: Path, split: str, limit: int = 0) -> dict:
    split_ids = json.loads((root / "split.json").read_text(encoding="utf-8"))
    if set(split_ids.get("train", [])) & set(split_ids.get("test", [])):
        raise ValueError("train and test splits overlap")
    ids = split_ids[split]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate document ids in {split} split")
    if limit:
        ids = ids[:limit]
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"wiki2gt_{split}.jsonl"
    pages = 0
    # Failed projection/roundtrip/image checks must not publish a partial dataset.
    fd, staging = tempfile.mkstemp(prefix=".dataset-", suffix=".tmp", dir=output)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for sample_id in ids:
                space, name = sample_id.split("/", 1)
                samples = project(root / "docs" / space / name, space, name)
                issues = roundtrip_check(samples)
                if issues:
                    raise ValueError(f"{sample_id}: {issues}")
                for item in samples:
                    if not Path(item["image"]).is_file():
                        raise FileNotFoundError(item["image"])
                    row = {
                        "messages": [
                            {"role": "user", "content": "<image>" + item["prompt"]},
                            {"role": "assistant", "content": item["target"]},
                        ],
                        "images": [str(Path(item["image"]).resolve())],
                        "sample_id": sample_id,
                        "page": item["page"],
                    }
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    pages += 1
        os.replace(staging, destination)
    finally:
        Path(staging).unlink(missing_ok=True)

    if split == "train":
        clean = output / "wiki2gt_train.clean.jsonl"
        with destination.open(encoding="utf-8") as source, clean.open("w", encoding="utf-8") as handle:
            for line in source:
                row = json.loads(line)
                handle.write(json.dumps({"messages": row["messages"], "images": row["images"]},
                                        ensure_ascii=False) + "\n")
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "image_spec_version": IMAGE_SPEC_VERSION,
        "split": split, "documents": ids, "pages": pages,
        "input_root": str(root.resolve()), "dataset": destination.name,
    }
    (output / f"wiki2gt_{split}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["train", "test", "both"], default="both")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--roundtrip", action="store_true", help="Compatibility flag; validation is always enabled")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    for split in (["train", "test"] if args.split == "both" else [args.split]):
        print(json.dumps(build(args.input_root, args.output, split, args.limit), ensure_ascii=False))


if __name__ == "__main__":
    main()
