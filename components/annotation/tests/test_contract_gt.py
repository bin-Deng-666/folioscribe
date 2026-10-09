"""Contract GT projection: OCR-Agent facts re-expressed in the registered contract shapes."""
from __future__ import annotations

import json
from pathlib import Path

from ocr_agent.contract_gt import export_contract_gt, node_id_for_block, schema_root_default


def _document_root(tmp_path: Path) -> Path:
    root = tmp_path / "batch"
    sample = "rd_test"
    (root / "evidence" / sample).mkdir(parents=True)
    (root / "runs" / sample / "page_0001").mkdir(parents=True)
    (root / "runs" / sample / "page_0002").mkdir(parents=True)
    (root / "documents").mkdir(parents=True)
    (root / "manifests").mkdir(parents=True)
    for page in (1, 2):
        evidence = {
            "page": page,
            "page_image": {"width": 1000, "height": 1400},
            "pdf_page": {"width_pt": 612.0, "height_pt": 792.0},
            "evidence": {"pdf": [{"evidence_id": f"pdf.line.p{page}.0000", "kind": "pdf_line", "bbox_image_px": [100.0, 100.0, 500.0, 140.0], "text": "标题", "font_size": 16.0, "bold": True}]},
        }
        (root / "evidence" / sample / f"page_{page:04d}.json").write_text(json.dumps(evidence), encoding="utf-8")
        block = {
            "block_id": f"candidate.block.p{page}.0000",
            "type": "table" if page == 2 else "title",
            "level": 1 if page == 1 else None,
            "text": "标题" if page == 1 else "",
            "html": "<table><tr><td>a</td></tr></table>" if page == 2 else None,
            "bbox_image_px": [100.0, 100.0, 500.0, 140.0],
            "reading_order": 0,
            "source_refs": [f"pdf.line.p{page}.0000"],
            "support_refs": [],
        }
        for key in ("level", "html", "text"):
            if block[key] is None:
                block.pop(key)
        revision = {
            "blocks": [block],
            "field_reviews": {"blocks": {block["block_id"]: {}}},
            "review_items": [],
            "document_relations": [],
        }
        (root / "runs" / sample / f"page_{page:04d}" / "result.json").write_text(json.dumps({"sample_id": sample, "page": page, "status": "PASS", "revision": revision}), encoding="utf-8")
    (root / "documents" / f"{sample}.json").write_text(json.dumps({
        "sample_id": sample,
        "document_flow": {
            "text_edges": [],
            "table_groups": [{"document_table_identity": "t1", "part_count": 2, "parts": [
                {"page": 1, "block_id": "candidate.block.p1.0000", "truncated_at_page_end": True, "repeated_header": None},
                {"page": 2, "block_id": "candidate.block.p2.0000", "truncated_at_page_end": False, "repeated_header": True},
            ]}],
        },
    }), encoding="utf-8")
    (root / "manifests" / f"{sample}.json").write_text(json.dumps({"sample_id": sample, "source_pdf_sha256": "a" * 64}), encoding="utf-8")
    return root


def test_contract_gt_export_writes_registered_files(tmp_path: Path, monkeypatch) -> None:
    # Export correctness is independent of the optional external DocFusion schemas.
    monkeypatch.delenv("DOCPARSE_SCHEMA_ROOT", raising=False)
    root = _document_root(tmp_path)
    report = export_contract_gt(root, "rd_test", schema_root=schema_root_default())
    target = root / "contract_gt" / "rd_test"
    for name in ("canonical_doc_graph.json", "page_geometry_manifest.json", "full_document_gt_bundle.json", "gt_truth_document.json"):
        assert (target / name).is_file(), name
    for name in ("annotation_fact_a1.json", "annotation_fact_a2.json", "annotation_fact_a3.json"):
        assert (target / "a_facts" / name).is_file(), name
    bundle = json.loads((target / "full_document_gt_bundle.json").read_text(encoding="utf-8"))
    assert bundle["table_continuations"][0]["source_table_id"] == node_id_for_block(1, "candidate.block.p1.0000")
    assert bundle["table_continuations"][0]["target_table_id"] == node_id_for_block(2, "candidate.block.p2.0000")
    assert bundle["table_continuations"][0]["header_repeated"] is True
    # completeness is only written when the fragment rows are actually measurable: this fixture's
    # first part has no table HTML, so the fact stays absent instead of being invented
    assert "completeness" not in bundle["table_continuations"][0]
    coverage = bundle["coverage"]
    assert coverage["graph_node_count"] == coverage["a1_mapped_node_count"] + 1  # document root has no entity
    # like the registered deliveries: the root is not reported as an unmapped node
    assert coverage["unmapped_graph_node_ids"] == []
    assert report["validation"]["validated"] is False
    assert report["validation"]["reason"] == "schema root unavailable"


def test_contract_gt_table_continuation_completeness_uses_measured_rows(tmp_path: Path) -> None:
    """Continuation completeness: logical row numbering, deduplicated repeated header, page-edge state."""
    root = _document_root(tmp_path)
    sample = "rd_test"
    for page, rows in ((1, ("h", "r1")), (2, ("h", "r2"))):
        path = root / "runs" / sample / f"page_{page:04d}" / "result.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        block = result["revision"]["blocks"][0]
        block["type"] = "table"
        block["html"] = "<table>" + "".join(f"<tr><td>{row}</td></tr>" for row in rows) + "</table>"
        path.write_text(json.dumps(result), encoding="utf-8")
    export_contract_gt(root, sample, schema_root=schema_root_default())
    bundle = json.loads((root / "contract_gt" / sample / "full_document_gt_bundle.json").read_text(encoding="utf-8"))
    completeness = bundle["table_continuations"][0]["completeness"]
    # head fragment: rows 0-1 with the table continuing to the next page
    assert completeness == {
        "this_page": "truncated_bottom",
        "missing_directions": ["bottom"],
        "row_range": [0, 1],
        "total_rows": 3,
    }
    # the repeated header is counted once: the second fragment contributes one new row (index 2)
    assert completeness["total_rows"] == 3


def test_contract_gt_verified_table_grid_projects_rows_and_cells(tmp_path: Path) -> None:
    """A verified PDF cell matrix becomes row/cell nodes, entities, relations and A1 table_regions."""
    root = _document_root(tmp_path)
    sample = "rd_test"
    path = root / "runs" / sample / "page_0002" / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    block = result["revision"]["blocks"][0]
    block["table_cell_geometry"] = [
        {"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "bbox_image_px": [100.0, 100.0, 300.0, 120.0], "text": "h"},
        {"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "bbox_image_px": [100.0, 120.0, 300.0, 140.0], "text": "v"},
    ]
    block["table_grid_evidence"] = {"rows": 2, "cols": 1, "physical_cells": 2, "method": "pdf_ruled_line_segments"}
    path.write_text(json.dumps(result), encoding="utf-8")
    export_contract_gt(root, sample, schema_root=schema_root_default())
    target = root / "contract_gt" / sample
    bundle = json.loads((target / "full_document_gt_bundle.json").read_text(encoding="utf-8"))
    graph = json.loads((target / "canonical_doc_graph.json").read_text(encoding="utf-8"))
    table_node = node_id_for_block(2, "candidate.block.p2.0000")
    kinds = {node["node_id"]: node["node_type"] for node in graph["nodes"]}
    assert kinds[f"{table_node}-r0"] == "table_row"
    assert kinds[f"{table_node}-r1-c0"] == "table_cell"
    entities = {entity["node_id"]: entity for entity in bundle["a1_entities"]}
    # row boxes are the measured union of their cells (image px converted to the contract's PDF points)
    assert entities[f"{table_node}-r0"]["bbox"] == [61.2, 56.5714, 183.6, 67.8857]
    assert entities[f"{table_node}-r1"]["bbox"] == [61.2, 67.8857, 183.6, 79.2]
    assert entities[f"{table_node}-r0"]["bbox"] == entities[f"{table_node}-r0-c0"]["bbox"]
    assert entities[f"{table_node}-r1-c0"]["text"] == "v"
    relations = {(r["relation_type"], r["source_node_id"], r["target_node_id"]) for r in bundle["a2_relations"]}
    assert ("table_row_of", f"{table_node}-r0", table_node) in relations
    assert ("table_cell_of", f"{table_node}-r1-c0", f"{table_node}-r1") in relations
    roles = {node["node_id"]: node["role"] for node in bundle["a2_nodes"]}
    assert roles[f"{table_node}-r0-c0"] == "body"
    tree = {node["node_id"]: node for node in bundle["a3_nodes"]}
    assert tree[f"{table_node}-r0-c0"]["parent_node_id"] == f"{table_node}-r0"
    assert tree[f"{table_node}-r0"]["parent_node_id"] == table_node
    assert tree[f"{table_node}-r0"]["span_end_order"] >= tree[f"{table_node}-r0-c0"]["order"]
    # the graph is fully mapped/classified/linked: rows and cells carry entities and roles
    coverage = bundle["coverage"]
    assert coverage["unmapped_graph_node_ids"] == [] and coverage["unclassified_node_ids"] == []
    assert coverage["unlinked_node_ids"] == []
    a1 = json.loads((target / "a_facts" / "annotation_fact_a1.json").read_text(encoding="utf-8"))
    region = a1["table_regions"][0]
    assert region["table_id"] == table_node and region["page_id"] == "page:0002"
    assert [cell["role"] for cell in region["cell_grid"]] == ["header", "body"]
    assert len(region["border_bands"]) >= 1


def test_contract_gt_hashes_and_raw_file_follow_reference_convention(tmp_path: Path) -> None:
    """Hashes are compact+sorted-keys JSON, and gt_raw_sha256 is the sha256 of gt_raw.json."""
    import hashlib

    root = _document_root(tmp_path)
    export_contract_gt(root, "rd_test", schema_root=schema_root_default())
    target = root / "contract_gt" / "rd_test"
    bundle = json.loads((target / "full_document_gt_bundle.json").read_text(encoding="utf-8"))
    graph_text = (target / "canonical_doc_graph.json").read_text(encoding="utf-8")
    geometry_text = (target / "page_geometry_manifest.json").read_text(encoding="utf-8")
    compact = lambda payload: hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert bundle["canonical_doc_graph_sha256"] == compact(json.loads(graph_text))
    assert bundle["page_geometry_sha256"] == compact(json.loads(geometry_text))
    raw_bytes = (target / "gt_raw.json").read_bytes()
    assert raw_bytes == (target / "canonical_doc_graph.json").read_bytes()
    truth = json.loads((target / "gt_truth_document.json").read_text(encoding="utf-8"))
    assert truth["gt_raw_sha256"] == hashlib.sha256(raw_bytes).hexdigest()


def test_contract_gt_a1_projection_carries_contract_method_and_regions(tmp_path: Path) -> None:
    root = _document_root(tmp_path)
    export_contract_gt(root, "rd_test", schema_root=schema_root_default())
    a1 = json.loads((root / "contract_gt" / "rd_test" / "a_facts" / "annotation_fact_a1.json").read_text(encoding="utf-8"))
    # the registry requires at least two unique claim methods
    assert len(a1["annotation_method"]) >= 2
    assert len(set(a1["annotation_method"])) == len(a1["annotation_method"])
    assert "table_regions" in a1


def test_contract_gt_entities_carry_contract_fields(tmp_path: Path) -> None:
    root = _document_root(tmp_path)
    export_contract_gt(root, "rd_test", schema_root=schema_root_default())
    bundle = json.loads((root / "contract_gt" / "rd_test" / "full_document_gt_bundle.json").read_text(encoding="utf-8"))
    entity = bundle["a1_entities"][0]
    assert entity["node_id"] == node_id_for_block(1, "candidate.block.p1.0000")
    assert entity["page_number"] == 1
    assert entity["bbox"] == [100.0, 100.0, 500.0, 140.0]
    assert entity["reading_order"] == 0  # contract reading order is 0-based, like the reference deliveries
    assert entity["fragment_ids"] == ["fragment:pdf.line.p1.0000"]
    assert {"font_size_pt", "font_weight", "text_indent_pt", "text_align", "font_family"} <= set(entity["rendered_style"])
    heading = bundle["a2_heading_entities"][0]
    assert heading["heading_role"] == "title" and heading["level_absolute"] == 1
    assert bundle["a2_nodes"][1]["role"] == "heading"
    assert bundle["a2_nodes"][2]["role"] == "table"
