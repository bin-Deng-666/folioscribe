"""Generate page-aligned Paddle/Ovis inputs for the annotation evidence builder.

Run in a dedicated client environment. Real-model execution requires a GPU service;
local unit tests cover rendering and output normalization only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

OVIS_PROMPT = (
    'Extract all readable content from the image in natural human reading order and output '
    'the result as a single Markdown document. For charts or images, represent them using '
    'an HTML image tag: <img src="images/bbox_{left}_{top}_{right}_{bottom}.jpg" />, where '
    'left, top, right, bottom are bounding box coordinates scaled to [0, 1000). '
    'Format formulas as LaTeX. Format tables as HTML: <table>...</table>. '
    'Transcribe all other text as standard Markdown. Preserve the original text without '
    'translation or paraphrasing.'
)


def normalize_paddle_result(value: dict, image_size: tuple[int, int] | None = None) -> dict:
    payload = value.get("res", value)
    if not isinstance(payload, dict) or not isinstance(payload.get("parsing_res_list"), list):
        raise ValueError("Expected official PaddleOCRVL parsing_res_list; a plain chat response is not valid")
    for block in payload["parsing_res_list"]:
        if not isinstance(block, dict) or "block_bbox" not in block or "block_label" not in block:
            raise ValueError("Paddle block is missing geometry or label")
    payload = dict(payload)
    if image_size is not None:
        width, height = image_size
        for field, expected in (("width", width), ("height", height)):
            if field in payload and payload[field] != expected:
                raise ValueError(f"Paddle {field} differs from the input page image")
            payload[field] = expected
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parser", choices=["paddle", "ovis"])
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible /v1 base URL")
    parser.add_argument("--short-id", required=True)
    parser.add_argument("--model", default="ovis-ocr2")
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--limit", type=int, default=0, help="0 means all pages")
    args = parser.parse_args()
    if args.dpi < 1 or args.limit < 0:
        parser.error("dpi must be positive and limit non-negative")
    if not args.short_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.short_id):
        parser.error("short-id must contain only letters, numbers, '_' or '-'")
    import pymupdf
    if args.parser == "paddle":
        from paddleocr import PaddleOCRVL
        pipeline = PaddleOCRVL(
            pipeline_version="v1.6", vl_rec_backend="vllm-server", vl_rec_server_url=args.endpoint,
            use_doc_orientation_classify=False, use_doc_unwarping=False, device="cpu",
        )
        client = None
    else:
        from openai import OpenAI
        client = OpenAI(base_url=args.endpoint, api_key=os.environ.get("FOLIOSCRIBE_MODEL_API_KEY", "EMPTY"))
    target = args.output.resolve()
    for folder in ("images", "json", "markdown", "raw"):
        (target / folder / args.short_id).mkdir(parents=True, exist_ok=True)
    completed = []
    try:
        with pymupdf.open(args.pdf) as document:
            count = min(args.limit or len(document), len(document))
            for index in range(count):
                stem = f"page_{index + 1:04d}"
                image = target / "images" / args.short_id / f"{stem}.png"
                raster = document[index].get_pixmap(dpi=args.dpi, alpha=False)
                raster.save(image)
                if args.parser == "paddle":
                    results = list(pipeline.predict(str(image)))
                    if len(results) != 1:
                        raise ValueError(f"{stem}: expected one page result, got {len(results)}")
                    raw_path = target / "raw" / args.short_id / f"{stem}.json"
                    results[0].save_to_json(save_path=str(raw_path))
                    payload = normalize_paddle_result(
                        json.loads(raw_path.read_text(encoding="utf-8")), (raster.width, raster.height)
                    )
                    (target / "json" / args.short_id / f"{stem}_res.json").write_text(
                        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                else:
                    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
                    response = client.chat.completions.create(
                        model=args.model, temperature=0, max_tokens=16384,
                        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                        messages=[{"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                            {"type": "text", "text": OVIS_PROMPT},
                        ]}],
                    )
                    (target / "raw" / args.short_id / f"{stem}.response.json").write_text(
                        response.model_dump_json(indent=2), encoding="utf-8")
                    choice = response.choices[0]
                    if choice.finish_reason == "length" or not choice.message.content:
                        raise ValueError(f"{stem}: empty or truncated model response")
                    (target / "markdown" / args.short_id / f"{stem}.md").write_text(
                        choice.message.content, encoding="utf-8")
                completed.append(index + 1)
                print(f"{args.parser}: {stem}", flush=True)
    finally:
        if client is not None:
            client.close()
        manifest = {"parser": args.parser, "short_id": args.short_id,
                    "source_pdf": str(args.pdf.resolve()),
                    "source_sha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
                    "pages_completed": completed, "dpi": args.dpi, "endpoint": args.endpoint}
        (target / f"{args.short_id}.manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
