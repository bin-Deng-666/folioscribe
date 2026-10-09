"""Reusable document selection policy; callers supply profiles and asset roots.

Thresholds are inherited heuristics, not a published accuracy benchmark.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

POLICY: dict[str, Any] = {
    "version": "ocr-agent.corpus-selection.v1",
    "asset_gate": {
        "note": "a document is runnable only when every evidence source the chain reads is present",
        "requires_pdf": True,
        "requires_mineru_raw": True,
        "requires_paddle_pages": True,
        "requires_ovis_pages": True,
        "paddle_ovis_page_counts_must_match": True,
    },
    "content_gate": {
        "max_pages": 100,
        "min_text_layer_coverage": 0.05,
        "max_scan_likelihood": 0.5,
        "image_dominance_min_area": 0.35,
        "image_dominance_max_text": 0.10,
        "note": (
            "a document is dropped when whole-page images dominate (scan_likelihood), when the native text "
            "layer is too thin to ground annotations, or when images dominate the canvas AND the text layer "
            "is thin at the same time. Documents that are image-rich but carry a healthy text layer "
            "(screenshots in a manual, for example) stay selected: the chain handles figures explicitly"
        ),
    },
    "tier_rules": {
        "core": (
            "table_rich OR (has_tables AND has_headings) OR (has_headings AND cross_page_flow AND pages>=8)"
        ),
        "tiny_pages": 2,
    },
    "capability_flags": {
        "table_rich": "table_page_rate>=0.30 OR mineru_table_count>=5 OR docx_tables>=5",
        "has_tables": "table_page_rate>0 OR mineru_table_count>=1 OR docx_tables>=1",
        "has_headings": "docx_heading_named_style_use>=2 OR heading_body_size_ratio>=1.30",
        "has_figures": "figure_page_rate>=0.20",
        "has_furniture": "running_header_rate>=0.30 OR page_number_rate>=0.50",
        "cross_page_flow": "pages>=5 AND text_layer_coverage>=0.10",
        "multi_column": "two_column_page_share>=0.50",
        "has_toc": "toc_leader_line_rate>0",
    },
}


def page_assets(directory: Path, pattern: str = "page_*.png") -> int:
    return len(list(directory.glob(pattern))) if directory.is_dir() else 0


def evaluate(entry: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    roots = sources["roots"]
    sample_id = entry["sample_id"]
    short_id = sample_id.removeprefix("rd_")
    full_id = sources["full_ids"].get(short_id, "")
    perceptual = sources["perceptual"].get(short_id, {})
    features = perceptual.get("doc_features", {}) if perceptual else {}
    difficulty = sources["difficulty"].get(full_id, {}) if full_id else {}
    docx = sources["docx"].get(sample_id, {})
    mineru_scene = sources["mineru_scene"].get(full_id, {}) if full_id else {}
    sample_dir = sources["gt_dirs"].get(sample_id)

    pdf = (sample_dir / "pdf" / "document.pdf") if sample_dir else None
    mineru_dir = (roots["mineru"] / full_id) if full_id else None
    paddle_images = page_assets(roots["paddle"] / "images" / short_id)
    paddle_json = page_assets(roots["paddle"] / "json" / short_id, "page_*.json")
    ovis_images = page_assets(roots["ovis"] / "images" / short_id)
    ovis_markdown = (roots["ovis"] / "markdown" / short_id / "page_0001.md").is_file()
    docx_name = str(docx.get("docx_file") or "")
    docx_path = (roots["docx"] / docx_name) if docx_name else None

    pages = paddle_images or int(perceptual.get("page_count") or difficulty.get("n_pages") or 0)
    text_coverage = float(features.get("text_layer_coverage") or 0.0)
    image_coverage = float(features.get("image_area_coverage") or 0.0)
    scan_likelihood = float(features.get("scan_likelihood") or 0.0)
    table_page_rate = float(features.get("table_page_rate") or 0.0)
    figure_page_rate = float(features.get("figure_page_rate") or 0.0)
    running_header_rate = float(features.get("running_header_rate") or 0.0)
    page_number_rate = float(features.get("page_number_rate") or 0.0)
    two_column_share = float((features.get("column_count_hist") or {}).get("2") or 0.0)
    heading_ratio = float(features.get("heading_body_size_ratio") or 0.0)
    mineru_table_count = int(((mineru_scene.get("table_structure") or {}).get("count")) or 0)
    docx_tables = int(docx.get("tables") or 0)
    docx_heading_style = int(docx.get("heading_named_style_use") or 0)

    flags = {
        "table_rich": table_page_rate >= 0.30 or mineru_table_count >= 5 or docx_tables >= 5,
        "has_tables": table_page_rate > 0 or mineru_table_count >= 1 or docx_tables >= 1,
        "has_headings": docx_heading_style >= 2 or heading_ratio >= 1.30,
        "has_figures": figure_page_rate >= 0.20,
        "has_furniture": running_header_rate >= 0.30 or page_number_rate >= 0.50,
        "cross_page_flow": pages >= 5 and text_coverage >= 0.10,
        "multi_column": two_column_share >= 0.50,
        "has_toc": float(features.get("toc_leader_line_rate") or 0.0) > 0,
    }

    exclusions: list[str] = []
    if not sample_dir or not pdf or not pdf.is_file():
        exclusions.append("missing_pdf")
    if not full_id or not mineru_dir or not mineru_dir.is_dir():
        exclusions.append("missing_mineru_raw")
    if paddle_images == 0 or paddle_json == 0:
        exclusions.append("missing_paddle_pages")
    if ovis_images == 0 or not ovis_markdown:
        exclusions.append("missing_ovis_pages")
    warnings: list[str] = []
    if paddle_images and ovis_images and paddle_images != ovis_images:
        exclusions.append(f"page_count_mismatch:paddle={paddle_images},ovis={ovis_images}")
    if perceptual and paddle_images and int(perceptual.get("page_count") or 0) != paddle_images:
        warnings.append(f"profile_page_count={perceptual.get('page_count')},assets={paddle_images}")
    if not perceptual:
        exclusions.append("missing_perceptual_profile")
    if difficulty.get("counts", {}).get("unparseable_doc"):
        exclusions.append("unparseable_document")
    if pages > POLICY["content_gate"]["max_pages"]:
        exclusions.append(f"pages>{POLICY['content_gate']['max_pages']}")
    if text_coverage < POLICY["content_gate"]["min_text_layer_coverage"]:
        exclusions.append("text_layer_too_thin")
    if scan_likelihood >= POLICY["content_gate"]["max_scan_likelihood"]:
        exclusions.append("whole_page_scan")
    if (
        image_coverage >= POLICY["content_gate"]["image_dominance_min_area"]
        and image_coverage > text_coverage
        and text_coverage < POLICY["content_gate"]["image_dominance_max_text"]
    ):
        exclusions.append("image_dominant_canvas")

    tier = ""
    if not exclusions:
        if flags["table_rich"] or (flags["has_tables"] and flags["has_headings"]) or (
            flags["has_headings"] and flags["cross_page_flow"] and pages >= 8
        ):
            tier = "core"
        else:
            tier = "extended"

    return {
        "sample_id": sample_id,
        "short_id": short_id,
        "full_id": full_id,
        "domain": sample_dir.parent.name if sample_dir else "",
        "tier": tier,
        "exclusions": exclusions,
        "warnings": warnings,
        "pages": pages,
        "tiny": pages <= POLICY["tier_rules"]["tiny_pages"],
        "page_metrics": {
            "text_layer_coverage": round(text_coverage, 4),
            "image_area_coverage": round(image_coverage, 4),
            "scan_likelihood": round(scan_likelihood, 4),
            "table_page_rate": round(table_page_rate, 4),
            "figure_page_rate": round(figure_page_rate, 4),
            "running_header_rate": round(running_header_rate, 4),
            "page_number_rate": round(page_number_rate, 4),
            "two_column_page_share": round(two_column_share, 4),
            "heading_body_size_ratio": round(heading_ratio, 4),
        },
        "structure": {
            "docx_available": bool(docx_path and docx_path.is_file()),
            "docx_tables": docx_tables,
            "docx_heading_named_style_use": docx_heading_style,
            "docx_body_paragraphs": int(docx.get("body_paras") or 0),
            "mineru_table_count": mineru_table_count,
            "parser_ok_share": round(float((difficulty.get("shares") or {}).get("ok") or 0.0), 4),
            "difficulty_tier": difficulty.get("tier", ""),
        },
        "flags": flags,
        "assets": {
            "pdf": str(pdf) if pdf else "",
            "gt_dir": str(sample_dir) if sample_dir else "",
            "mineru_raw": str(mineru_dir) if mineru_dir else "",
            "paddle_root": str(roots["paddle"] / "json" / short_id),
            "ovis_root": str(roots["ovis"] / "markdown" / short_id),
            "source_docx": str(docx_path) if docx_path and docx_path.is_file() else "",
        },
    }

