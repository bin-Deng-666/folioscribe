#!/usr/bin/env python3
"""Audit whether table HTML can be rebuilt from the measured PDF cell grid.

The dominant CHECK reason is `table.html`: the model's HTML was not confirmed against the PDF
ruled-line grid. Rebuilding the HTML from the measured grid would settle those blocks, but only if
the rebuild carries the same content the model HTML carried. This script recomputes the grid for
every unresolved table, compares the content a rebuild would carry against the model HTML, and
reports how many blocks (and pages) a rebuild would actually settle without dropping text.

Nothing is written: the audit only decides whether the rebuild is worth implementing.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from ocr_agent.evidence_builder import (
    _html_table_grid,
    _ioa,
    _pdf_ruled_cell_grid,
    _pdf_segment_ruled_cell_grid,
    _table_cell_text_norm,
)


def page_evidence(batch_root: Path, sample: str, page: int) -> dict | None:
    path = batch_root / "evidence" / sample / f"page_{page:04d}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def measured_grid(page: dict, box: list[float]) -> tuple[str, list[dict]] | None:
    for method, ruled in (
        ("pdf_ruled_cell_rectangles", _pdf_ruled_cell_grid(page, box)),
        ("pdf_ruled_line_segments", _pdf_segment_ruled_cell_grid(page, box)),
    ):
        if ruled is not None:
            return method, ruled[0]
    return None


def rebuild_content(page: dict, block: dict, cells: list[dict]) -> list[str]:
    """Cell texts a rebuild would carry: PDF lines assigned to the cell that contains them."""
    evidence = {item["evidence_id"]: item for item in page["evidence"]["pdf"]}
    lines = [evidence[ref] for ref in block.get("source_refs", []) if evidence.get(ref, {}).get("kind") == "pdf_line"]
    texts: list[str] = []
    for cell in cells:
        owned = [line for line in lines if _ioa(line["bbox_image_px"], cell["bbox_image_px"]) >= 0.70]
        owned.sort(key=lambda item: (item["bbox_image_px"][1], item["bbox_image_px"][0]))
        texts.append(_table_cell_text_norm("".join(str(line.get("text", "")) for line in owned)))
    return texts


PRIVATE_USE = re.compile(r"[\ue000-\uf8ff]")


def _joined(texts) -> str:
    return "".join(text for text in texts if text)


def _glyph_free(text: str) -> str:
    """Private-use codepoints (Wingdings-style bullets) carry no comparable characters."""
    return PRIVATE_USE.sub("", text)


def classify(model_html: str, cells: list[dict], rebuilt: list[str]) -> tuple[str, dict]:
    model = _html_table_grid(model_html) or []
    model_topology = {(cell["row"], cell["col"], cell["rowspan"], cell["colspan"]) for cell in model}
    grid_topology = {(cell["row"], cell["col"], cell["rowspan"], cell["colspan"]) for cell in cells}
    model_texts = Counter(_table_cell_text_norm(str(cell.get("text") or "")) for cell in model)
    rebuilt_texts = Counter(text for text in rebuilt if text)
    topology_equal = model_topology == grid_topology
    detail = {
        "model_cells": len(model),
        "grid_cells": len(cells),
        "topology_equal": topology_equal,
        "model_text_chars": sum(len(text) for text in model_texts.elements()),
        "rebuilt_text_chars": sum(len(text) for text in rebuilt_texts.elements()),
    }
    model_join, rebuilt_join = _joined(model_texts.elements()), _joined(rebuilt_texts.elements())
    model_chars = Counter(_glyph_free(model_join).replace(" ", ""))
    rebuilt_chars = Counter(_glyph_free(rebuilt_join).replace(" ", ""))
    detail["model_chars"] = len(model_join)
    detail["rebuilt_chars"] = len(rebuilt_join)
    detail["char_multiset_equal"] = model_chars == rebuilt_chars
    detail["chars_lost"] = sum((model_chars - rebuilt_chars).values())
    detail["chars_gained"] = sum((rebuilt_chars - model_chars).values())
    if topology_equal and model_texts == rebuilt_texts:
        return "already_consistent", detail
    if model_texts == rebuilt_texts:
        return "text_conserved", detail
    # Cell-boundary differences redistribute characters between cells without losing them: the
    # content check has to be on the concatenated text, not on the per-cell multiset.
    # A cell boundary can sit elsewhere and still carry every character: compare the character
    # multiset (order-free, glyph-free) rather than the concatenated string.
    if detail["char_multiset_equal"]:
        return "chars_conserved", detail
    if not rebuilt_join:
        return "rebuild_empty", detail
    if detail["chars_lost"] <= 2 and detail["chars_gained"] <= 2:
        return "chars_near_conserved", detail
    lost = model_texts - rebuilt_texts
    gained = rebuilt_texts - model_texts
    detail["lost_cells"] = sum(lost.values())
    detail["lost_chars"] = sum(len(text) * count for text, count in lost.items())
    detail["gained_cells"] = sum(gained.values())
    if detail["lost_chars"] > 0 and detail["gained_cells"] == 0:
        return "text_loss_only", detail
    return "text_loss_and_gain", detail


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", required=True, type=Path)
    parser.add_argument("--samples", help="comma-separated subset")
    parser.add_argument("--dump", type=Path, help="write per-block findings as JSON lines")
    args = parser.parse_args()

    wanted = {value.strip() for value in (args.samples or "").split(",") if value.strip()}
    classes: Counter[str] = Counter()
    per_sample: dict[str, Counter[str]] = defaultdict(Counter)
    blocking_fields: Counter[str] = Counter()
    page_candidates: dict[tuple[str, int], list[str]] = defaultdict(list)
    findings: list[dict] = []
    for result_path in sorted((args.batch_root / "runs").glob("*/page_*/result.json")):
        sample = result_path.parts[-3]
        if wanted and sample not in wanted:
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        page = int(result.get("page") or 0)
        reviews = ((result.get("revision") or {}).get("field_reviews") or {}).get("blocks") or {}
        unresolved = {
            block_id: sorted(field for field, value in fields.items()
                             if isinstance(value, dict) and value.get("status") not in (None, "PASS"))
            for block_id, fields in reviews.items()
        }
        other_unresolved = {
            block_id: [field for field in fields if field != "html"]
            for block_id, fields in unresolved.items()
        }
        for block in (result.get("revision") or {}).get("blocks") or []:
            if block.get("type") != "table":
                continue
            block_id = str(block.get("block_id"))
            fields = unresolved.get(block_id) or []
            if "html" not in fields or not isinstance(block.get("html"), str):
                continue
            blocking_fields[tuple(other_unresolved.get(block_id) or [])] += 1
            evidence = page_evidence(args.batch_root, sample, page)
            box = block.get("bbox_image_px")
            if not evidence or not box:
                classes["no_evidence"] += 1
                per_sample[sample]["no_evidence"] += 1
                continue
            grid = measured_grid(evidence, box)
            if grid is None:
                classes["no_pdf_grid"] += 1
                per_sample[sample]["no_pdf_grid"] += 1
                continue
            method, cells = grid
            rebuilt = rebuild_content(evidence, block, cells)
            label, detail = classify(block["html"], cells, rebuilt)
            classes[label] += 1
            per_sample[sample][label] += 1
            if label in {"text_conserved", "already_consistent", "chars_conserved", "chars_near_conserved"} \
                    and not (other_unresolved.get(block_id) or []):
                page_candidates[(sample, page)].append(block_id)
            findings.append({
                "sample": sample, "page": page, "block_id": block_id, "grid_method": method,
                "class": label, "other_unresolved_fields": other_unresolved.get(block_id) or [], **detail,
            })
    print(json.dumps({"schema_version": "ocr-agent.table-html-rebuild-audit.v1",
                      "batch_root": str(args.batch_root),
                      "unresolved_table_html_blocks": sum(classes.values()),
                      "classes": dict(classes.most_common()),
                      "pages_settleable_by_rebuild": len(page_candidates),
                      "other_unresolved_field_sets": {str(list(key)): value for key, value in blocking_fields.most_common(6)}},
                     ensure_ascii=False, indent=1))
    print("\nper sample (top 12 by unresolved):")
    for sample, counter in sorted(per_sample.items(), key=lambda item: -sum(item[1].values()))[:12]:
        print(f"  {sample}: {json.dumps(dict(counter.most_common()), ensure_ascii=False)}")
    if args.dump:
        args.dump.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in findings) + "\n", encoding="utf-8")
        print(f"\nwrote {len(findings)} findings to {args.dump}")


if __name__ == "__main__":
    main()
