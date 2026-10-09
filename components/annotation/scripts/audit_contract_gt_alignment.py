#!/usr/bin/env python3
"""Audit the Contract GT export against the registered deliveries' field conventions.

The legal shape of the contracts lives in the registry; the *conventions* (what a hash covers,
whether reading order is 0-based, which namespace fragment ids use, how the coverage counters
relate, whether table rows/cells are nodes) only show up in the delivered reference bundles.
This script compares an OCR-Agent export against a reference delivery and prints one line per
convention, so alignment stays checkable instead of being asserted in prose.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_sha(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def bundle_dir(root: Path, document: str | None = None) -> Path:
    """Accept a bundle directory or a batch root; a batch defaults to its most complete document."""
    if (root / "full_document_gt_bundle.json").is_file():
        return root
    candidates = sorted(root.glob("contract_gt/*/full_document_gt_bundle.json"))
    if document:
        candidates = [path for path in candidates if path.parent.name == document]
    if not candidates:
        raise SystemExit(f"no full_document_gt_bundle.json under {root}")

    def weight(path: Path) -> int:
        bundle = load(path)
        return sum(1 for entity in bundle.get("a1_entities") or [] if "-r" in entity["node_id"])

    return max(candidates, key=weight).parent


def conventions(directory: Path) -> dict[str, Any]:
    bundle = load(directory / "full_document_gt_bundle.json")
    graph = load(directory / "canonical_doc_graph.json")
    geometry = load(directory / "page_geometry_manifest.json")
    truth = load(directory / "gt_truth_document.json")
    raw = (directory / "gt_raw.json").read_bytes() if (directory / "gt_raw.json").is_file() else b""
    entities = bundle.get("a1_entities") or []
    headings = bundle.get("a2_heading_entities") or []
    continuations = bundle.get("table_continuations") or []
    coverage = bundle.get("coverage") or {}
    relation_types = sorted({str(item.get("relation_type")) for item in bundle.get("a2_relations") or []})
    child_ids = [entity["node_id"] for entity in entities if "-r" in entity["node_id"]]
    return {
        "canonical_graph_hash_is_compact_sorted": bundle.get("canonical_doc_graph_sha256") == compact_sha(graph),
        "page_geometry_hash_is_compact_sorted": bundle.get("page_geometry_sha256") == compact_sha(geometry),
        "gt_raw_hash_is_file_sha": bool(raw) and truth.get("gt_raw_sha256") == hashlib.sha256(raw).hexdigest(),
        "reading_order_is_zero_based": bool(entities) and min(int(e["reading_order"]) for e in entities) == 0,
        "fragment_ids_use_fragment_namespace": bool(entities)
        and all(str(f).startswith("fragment:") for e in entities for f in e.get("fragment_ids") or []),
        "rendered_style_has_font_family": bool(entities)
        and all("font_family" in (e.get("rendered_style") or {}) for e in entities),
        "relation_types": relation_types,
        "table_rows_and_cells_are_nodes": any(
            node.get("node_type") in {"table_row", "table_cell"} for node in graph.get("nodes") or []
        ),
        "table_children_have_entities": bool(child_ids)
        and {entity["node_id"] for entity in entities} >= set(child_ids),
        "coverage_root_excluded_from_unmapped": not coverage.get("unmapped_graph_node_ids"),
        "coverage_a1_is_graph_minus_root": coverage.get("a1_mapped_node_count") == coverage.get("graph_node_count", 0) - 1,
        "coverage_a2_a3_cover_graph": coverage.get("a2_classified_node_count") == coverage.get("graph_node_count")
        and coverage.get("a3_linked_node_count") == coverage.get("graph_node_count"),
        "headings_have_identifiability": all("identifiability_by_profile" in h for h in headings) if headings else None,
        "continuations_carry_completeness": (
            all("completeness" in c for c in continuations) if continuations else None
        ),
        "counts": {
            "entities": len(entities),
            "graph_nodes": len(graph.get("nodes") or []),
            "relations": len(bundle.get("a2_relations") or []),
            "table_continuations": len(continuations),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-root", required=True, type=Path, help="OCR-Agent batch root or contract_gt document dir")
    parser.add_argument("--reference", type=Path, help="reference delivery bundle dir (realdoc/mimic gt/)")
    parser.add_argument("--document", help="sample_id to audit inside a batch root")
    args = parser.parse_args()
    chosen = bundle_dir(args.export_root, args.document)
    print(f"audited document: {chosen.name}")
    ours = conventions(chosen)
    print("OCR-Agent export:", json.dumps(ours, ensure_ascii=False, indent=1))
    if not args.reference:
        return
    reference = conventions(bundle_dir(args.reference))
    print("\nreference export:", json.dumps(reference, ensure_ascii=False, indent=1))
    shared = [key for key in ours if key in reference and key != "counts"]
    print("\nconvention                                | reference | ocr-agent | aligned")
    for key in shared:
        left, right = reference[key], ours[key]
        aligned = "yes" if left == right else "no"
        print(f"{key:<41} | {str(left):<9} | {str(right):<9} | {aligned}")


if __name__ == "__main__":
    main()
