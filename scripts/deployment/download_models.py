"""Download one upstream checkpoint and record the resolved Hugging Face revision."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MODELS = {
    "paddle": "PaddlePaddle/PaddleOCR-VL-1.6",
    "ovis": "ATH-MaaS/OvisOCR2",
    "qwen": "Qwen/Qwen3.5-2B",
    "logics": "Logics-MLLM/Logics-Parsing-V3",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=MODELS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", default="main", help="Pin an immutable commit for reproducible experiments")
    args = parser.parse_args()
    from huggingface_hub import HfApi, snapshot_download
    repository = MODELS[args.model]
    info = HfApi().model_info(repository, revision=args.revision)
    destination = args.output / repository.split("/")[-1]
    snapshot_download(repo_id=repository, revision=info.sha, local_dir=destination)
    (destination / "folioscribe-source.json").write_text(json.dumps({
        "repository": repository, "requested_revision": args.revision, "resolved_revision": info.sha,
        "source": f"https://huggingface.co/{repository}",
    }, indent=2) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
