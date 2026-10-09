#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    manifest = _read(args.root / "pilot_manifest.json")
    rows = []
    for page in manifest["pages"]:
        run_dir = args.root / "runs" / manifest["sample_id"] / f"page_{page:04d}"
        result_path = run_dir / "result.json"
        if not result_path.is_file():
            rows.append({"page": page, "status": "CHECK", "missing": True, "model_seconds": 0.0})
            continue
        result = _read(result_path)
        elapsed = 0.0
        requests = 0
        for path in list((run_dir / "traces").glob("*.json")) + list((run_dir / "decisions").glob("*.error.json")):
            data = _read(path)
            if "elapsed_seconds" in data:
                elapsed += float(data["elapsed_seconds"])
                requests += 1
        rows.append({
            "page": page,
            "status": result.get("status", "CHECK"),
            "blocks": len(result.get("revision", {}).get("blocks", [])),
            "tasks": result.get("task_count", 0),
            "requests": requests,
            "model_seconds": round(elapsed, 3),
            "errors": result.get("validation", {}).get("error_count", 0),
            "unresolved_fields": result.get("validation", {}).get("unresolved_field_count", 0),
        })
    model_times = [row["model_seconds"] for row in rows]
    report = {
        "schema_version": "ocr-agent-benchmark.v1",
        "sample_id": manifest["sample_id"],
        "page_count": len(rows),
        "pass_pages": sum(row["status"] == "PASS" for row in rows),
        "check_pages": sum(row["status"] == "CHECK" for row in rows),
        "request_count": sum(row.get("requests", 0) for row in rows),
        "equivalent_model_seconds": round(sum(model_times), 3),
        "average_model_seconds_per_page": round(statistics.mean(model_times), 3) if model_times else 0.0,
        "median_model_seconds_per_page": round(statistics.median(model_times), 3) if model_times else 0.0,
        "p95_model_seconds_per_page": round(_percentile(model_times, 0.95), 3) if model_times else 0.0,
        "build": _read(args.root / "build_summary.json") if (args.root / "build_summary.json").is_file() else None,
        "pages": rows,
    }
    target = args.root / "benchmark.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


if __name__ == "__main__":
    main()
