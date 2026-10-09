#!/usr/bin/env python3
"""Validate raw MinerU, Paddle and Ovis assets against an actual PDF before annotation.

Unlike the historical selection-based audit, this entry point has no dependency on
private profile directories. It checks exact page names, image sizes, coordinate
bounds, parser structure and source hashes when manifests are present.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ocr_agent.parser_assets import audit_parser_assets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--full-id", required=True)
    parser.add_argument("--short-id", required=True)
    parser.add_argument("--paddle-root", type=Path, required=True)
    parser.add_argument("--ovis-root", type=Path, required=True)
    parser.add_argument("--mineru-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = audit_parser_assets(args.pdf, full_id=args.full_id, short_id=args.short_id,
                                 mineru_root=args.mineru_root, paddle_root=args.paddle_root,
                                 ovis_root=args.ovis_root)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
