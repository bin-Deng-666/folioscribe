#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ocr_agent.batch_registry import load_registry

ROOT = Path(__file__).resolve().parents[1]






def run(cmd: list[str], env: dict[str, str]) -> None:
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run OCR-Agent on multiple complete documents")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=0, help="document limit; 0 processes the whole registry")
    parser.add_argument("--anydoc-outline", type=Path)
    parser.add_argument("--registry", type=Path, required=True, help="document registry with explicit PDF and parser paths")
    parser.add_argument("--workers", type=int, default=1, help="number of documents to run concurrently (default: sequential)")
    parser.add_argument("--build-workers", type=int, default=4, help="number of documents to build concurrently; each build runs in its own staging root")
    args = parser.parse_args()
    if args.count < 0 or args.workers < 1 or args.build_workers < 1:
        parser.error("count must be non-negative and worker counts must be positive")
    documents = load_registry(args.registry, args.count)
    args.output = args.output.resolve()
    if args.anydoc_outline:
        args.anydoc_outline = args.anydoc_outline.resolve()
    print(f"registry={args.registry} documents={len(documents)}", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    registry = args.output / "batch_documents.json"
    registered_documents = documents
    if registry.is_file():
        existing = json.loads(registry.read_text(encoding="utf-8")).get("documents", [])
        selected = {str(item["sample_id"]): item for item in documents}
        registered_documents = [selected.pop(str(item.get("sample_id")), item) for item in existing]
        registered_documents.extend(selected.values())
    registry.write_text(json.dumps({"schema_version": "ocr-agent-document-batch.v1", "documents": registered_documents}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")

    def build_one(document: dict[str, object]) -> None:
        sample = str(document["sample_id"])
        # Each document is built in its own staging root: build_document.py writes shared file names
        # (document_manifest.json, dataset_manifest.json), which cannot be written concurrently.
        staging = args.output / ".build" / sample
        staging.mkdir(parents=True, exist_ok=True)
        print(f"\n=== BUILD {sample} ({document['pages']} pages) ===", flush=True)
        build_cmd = [sys.executable, str(ROOT / "scripts/build_document.py"), "--output", str(staging), "--sample-id", sample, "--full-id", str(document["full_id"]), "--short-id", str(document["short_id"]), "--pdf", str(document["pdf"]), "--mineru-root", str(document["mineru_root"]), "--paddle-root", str(document["paddle_root"]), "--ovis-root", str(document["ovis_root"]), "--all-pages"]
        if document.get("source_docx"):
            build_cmd += ["--source-docx", str(document["source_docx"])]
        if args.anydoc_outline and args.anydoc_outline.is_file():
            build_cmd += ["--anydoc-outline", str(args.anydoc_outline)]
        try:
            run(build_cmd, env)
        except subprocess.CalledProcessError as exc:
            if document.get("source_docx") and exc.returncode == 3:
                print(f"DOCX binding unavailable for {sample}; retrying PDF-only build", flush=True)
                fallback = [item for item in build_cmd if item not in ["--source-docx", str(document["source_docx"])]]
                run(fallback, env)
            else:
                raise
        manifest = json.loads((staging / "document_manifest.json").read_text(encoding="utf-8"))
        merged_files = []
        target_evidence = args.output / "evidence" / sample
        target_evidence.mkdir(parents=True, exist_ok=True)
        for path in sorted((staging / "evidence" / sample).glob("page_*.json")):
            destination = target_evidence / path.name
            shutil.copy2(path, destination)
            merged_files.append(str(destination))
        if merged_files:
            manifest["evidence_files"] = merged_files
        (args.output / "manifests").mkdir(exist_ok=True)
        (args.output / "manifests" / f"{sample}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(staging, ignore_errors=True)

    build_workers = max(1, min(args.build_workers, len(documents)))
    failures: list[dict[str, object]] = []

    def guarded_build(document: dict[str, object]) -> None:
        """One unusable document must never take the whole batch down (see `failures`)."""
        try:
            build_one(document)
        except Exception as exc:  # noqa: BLE001 - the batch reports it and keeps going
            sample = str(document.get("sample_id"))
            detail = str(exc)
            failures.append({"stage": "build", "sample_id": sample, "error": detail[-400:]})
            print(f"!!! BUILD FAILED {sample}: {detail.splitlines()[-1] if detail else ''}", flush=True)

    if build_workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=build_workers) as pool:
            futures = [pool.submit(guarded_build, document) for document in documents]
            for future in as_completed(futures):
                future.result()
    else:
        for document in documents:
            guarded_build(document)
    (args.output / ".build").exists() and shutil.rmtree(args.output / ".build", ignore_errors=True)
    (args.output / "failures.json").write_text(json.dumps({"schema_version": "ocr-agent-batch-failures.v1", "failures": failures}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((args.output / "manifests").glob("*.json"))]
    write_json_atomic = lambda path, value: path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_json_atomic(
        args.output / "build_summary.json",
        {
            "schema_version": "ocr-agent-document-batch-build.v1",
            "document_count": len(manifests),
            "page_count": sum(len(item.get("pages", [])) for item in manifests),
            "build_worker_count": build_workers,
            "wall_seconds": round(sum(float((item.get("build_timing") or {}).get("wall_seconds") or 0.0) for item in manifests), 1),
            "pages": [{"sample_id": item.get("sample_id"), "pages": len(item.get("pages", []))} for item in manifests],
        },
    )
    print(f"\n=== BUILD complete: {len(documents)} documents ===", flush=True)
    workers = max(1, min(args.workers, len(documents)))

    def run_one(document: dict[str, object]) -> None:
        sample = str(document["sample_id"])
        if not (args.output / "manifests" / f"{sample}.json").is_file():
            # its build failed; nothing to annotate
            return
        doc_env = dict(env)
        doc_env["OCR_AGENT_SAMPLE_ID"] = sample
        print(f"=== RUN {sample} (worker) ===", flush=True)
        try:
            run([sys.executable, str(ROOT / "scripts/run_document.py"), "--document-root", str(args.output), "--all-pages"], doc_env)
        except Exception as exc:  # noqa: BLE001 - keep annotating the rest of the batch
            detail = str(exc)
            failures.append({"stage": "run", "sample_id": sample, "error": detail[-400:]})
            print(f"!!! RUN FAILED {sample}: {detail.splitlines()[-1] if detail else ''}", flush=True)
            return
        per_sample = args.output / "documents" / f"{sample}.json"
        if per_sample.is_file():
            document_result = json.loads(per_sample.read_text(encoding="utf-8"))
            page_results = document_result.get("pages", [])
            document_result.update({
                "pass_pages": sum(item.get("status") == "PASS" for item in page_results),
                "check_pages": sum(item.get("status") == "CHECK" for item in page_results),
            })
            per_sample.write_text(json.dumps(document_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_one, document): str(document["sample_id"]) for document in documents}
            for future in as_completed(futures):
                future.result()
    else:
        for document in documents:
            run_one(document)
    all_pages = []
    for path in sorted((args.output / "runs").glob("*/page_*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        all_pages.append({"sample_id": result.get("sample_id"), "page": result.get("page"), "status": result.get("status"), "blocks": len(result.get("revision", {}).get("blocks", [])), "unresolved_fields": result.get("validation", {}).get("unresolved_field_count", 0)})
    batch_result = {
        "schema_version": "ocr-agent-document-batch-result.v1",
        "document_count": len(registered_documents),
        "page_count": len(all_pages),
        "pass_pages": sum(item["status"] == "PASS" for item in all_pages),
        "check_pages": sum(item["status"] == "CHECK" for item in all_pages),
        "documents": [json.loads(path.read_text(encoding="utf-8")) for path in sorted((args.output / "documents").glob("*.json"))],
        "pages": all_pages,
    }
    (args.output / "batch_result.json").write_text(json.dumps(batch_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"batch_root={args.output}")


if __name__ == "__main__":
    main()
