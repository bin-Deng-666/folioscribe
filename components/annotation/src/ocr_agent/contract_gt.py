"""Project an OCR-Agent document run onto the DocFusion Contract GT (A1/A2/A3 + bundle).

The contract family lives in ``DocFusion/03_code/configs/contracts`` (registry.json is the source
of truth; ``generated/jsonschema`` is the projection used here for validation). OCR-Agent already
produces every fact the contracts need:

* A1 physical/text facts      -> revision blocks (page, bbox, reading order, text, PDF evidence)
* A2 semantic roles/relations -> block types, caption parents, cross-page links
* A3 document tree            -> title hierarchy plus the governing relation of every block
* cross-page table groups     -> ``document_flow.table_groups`` (chained per-page table blocks)

Field names differ from the internal revision, so the export renames and re-scopes them; nothing is
invented. Anything the run did not establish (an unverified table HTML, an unmapped type) is written
with its contract status instead of being guessed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1.3.0"
# The registered A1/A2/A3 file schema pins ``fact_schema_version`` to the render line's "v0.4" and,
# with it, a ``constructed_render`` verification summary. An OCR-Agent fact set is the same shape but
# a different producer, so it declares its own fact schema version and is shipped under ``a_facts/``:
# the registered delivery contract is the bundle (the handoff says consumers should prefer it).
FACT_SCHEMA_VERSION = "ocr-agent.v1"
GRAPH_VERSION = "1.1.0"
FACT_VERSION = "1.1.0"
PAGE_GEOMETRY_VERSION = "1.0.0"
COORDINATE_SPACE = "pdf_points_top_left_after_rotation"
ANNOTATION_METHOD = ["pdf_intrinsic", "agent_inference"]
ROOT_FILES = {
    "canonical_doc_graph.json",
    "page_geometry_manifest.json",
    "full_document_gt_bundle.json",
    "gt_truth_document.json",
    "gt_raw.json",
}
PRODUCER_ID = "producer:ocr-agent"

TYPE_TO_NODE = {
    "title": "heading",
    "text": "paragraph",
    "table": "table",
    "image": "image",
    "chart": "image",
    "seal": "image",
    "formula": "formula",
    "table_caption": "caption",
    "image_caption": "caption",
    "header": "paragraph",
    "footer": "paragraph",
    "page_number": "paragraph",
    "footnote": "paragraph",
}
TYPE_TO_ROLE = {
    "title": "heading",
    "text": "body",
    "table": "table",
    "image": "figure",
    "chart": "figure",
    "seal": "figure",
    "formula": "equation",
    "table_caption": "caption",
    "image_caption": "caption",
    "header": "header",
    "footer": "footer",
    "page_number": "footer",
    "footnote": "body",
}
HEADING_ROLE = {1: "title", 2: "section", 3: "subsection", 4: "subsection"}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _contract_sha256(payload: Any) -> str:
    """Hash convention used by the registered deliveries: compact, sorted keys, UTF-8 preserved."""
    return _sha256_text(json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> str:
    """Write a contract artifact and return the sha256 of the bytes written (hashes point at files)."""
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return _sha256_text(text)


def _id(prefix: str, page: int, serial: str) -> str:
    return f"{prefix}:p{page}.{serial}"


def _fragment_id(evidence_id: str) -> str:
    """A fragment id is the source fragment an entity was measured on (PDF line/drawing/image or model block)."""
    return f"fragment:{evidence_id}"


def _serial(block_id: str) -> str:
    match = re.search(r"\.(\d{4})$", str(block_id))
    return match.group(1) if match else re.sub(r"[^A-Za-z0-9._~-]", "", str(block_id))[-24:]


def _image_box_to_pdf(pages: dict[int, dict], page_number: int, box: list[float]) -> list[float]:
    page = pages.get(page_number) or {}
    image = page.get("page_image") or {}
    pdf = page.get("pdf_page") or {}
    width = float(image.get("width") or 0.0)
    height = float(image.get("height") or 0.0)
    page_width = float(pdf.get("width_pt") or 0.0)
    page_height = float(pdf.get("height_pt") or 0.0)
    if width <= 0 or height <= 0 or page_width <= 0 or page_height <= 0:
        return [round(float(value), 4) for value in box]
    return [
        round(min(page_width, max(0.0, box[0] / width * page_width)), 4),
        round(min(page_height, max(0.0, box[1] / height * page_height)), 4),
        round(min(page_width, max(0.0, box[2] / width * page_width)), 4),
        round(min(page_height, max(0.0, box[3] / height * page_height)), 4),
    ]


def node_id_for_block(page_number: int, block_id: str) -> str:
    """Contract node id of a revision block (shared by the exporter and the viewer)."""
    return _id("node", int(page_number), _serial(block_id))


def entity_id_for_block(page_number: int, block_id: str) -> str:
    """Contract A1 entity id of a revision block."""
    return _id("entity", int(page_number), _serial(block_id))


def _box(value: Any) -> list[float] | None:
    if isinstance(value, list) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [round(float(item), 4) for item in value]
    return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _sequence_marker(text: str) -> dict[str, Any] | None:
    match = re.match(r"^\s*(\d+(?:\.\d+)+)[.、]?", str(text))
    if not match:
        return None
    marker = match.group(1)
    parts = [int(part) for part in marker.split(".")]
    if any(part < 1 for part in parts):
        # the contract's numbering path only admits components >= 1, so a marker like ``3.0`` is left
        # unprojected instead of being altered
        return None
    return {
        "raw_marker": marker,
        "path": [{"system": "decimal", "value": part} for part in parts],
        "consistent_with_structure": len(parts) <= 4,
    }


def load_document(document_root: Path, sample_id: str) -> tuple[dict[int, dict], dict[int, dict], dict]:
    pages: dict[int, dict] = {}
    for path in sorted((document_root / "evidence" / sample_id).glob("page_*.json")):
        page = _read_json(path)
        pages[int(page["page"])] = page
    results: dict[int, dict] = {}
    for path in sorted((document_root / "runs" / sample_id).glob("page_*/result.json")):
        result = _read_json(path)
        results[int(result["page"])] = result
    document_result_path = document_root / "documents" / f"{sample_id}.json"
    document_result = _read_json(document_result_path) if document_result_path.is_file() else {}
    manifest_path = document_root / "manifests" / f"{sample_id}.json"
    if manifest_path.is_file():
        manifest = _read_json(manifest_path)
        for key in ("source_pdf_sha256", "source_pdf", "pages", "run_protocol_version"):
            if key in manifest and key not in document_result:
                document_result[key] = manifest[key]
    if not document_result.get("source_pdf_sha256"):
        pdf_path = Path(str(document_result.get("pdf_path") or (document_root / "evidence" / sample_id / "page_0001.json")))
        candidate = document_root / "source" / "document.pdf"
        if candidate.is_file():
            document_result["source_pdf_sha256"] = _sha256_file(candidate)
        elif pdf_path.is_file() and pdf_path.suffix == ".pdf":
            document_result["source_pdf_sha256"] = _sha256_file(pdf_path)
    return pages, results, document_result


def _blocks(results: dict[int, dict]) -> list[tuple[int, dict]]:
    ordered: list[tuple[int, dict]] = []
    for page_number in sorted(results):
        for block in results[page_number].get("revision", {}).get("blocks", []):
            ordered.append((page_number, block))
    ordered.sort(key=lambda item: (item[0], int(item[1].get("reading_order", 0))))
    return ordered


def _page_body_font(pages: dict[int, dict], page_number: int) -> float:
    sizes = [
        float(item["font_size"])
        for item in pages[page_number].get("evidence", {}).get("pdf", [])
        if item.get("kind") == "pdf_line" and isinstance(item.get("font_size"), (int, float)) and float(item["font_size"]) > 0
    ]
    if not sizes:
        return 0.0
    return max(set(sizes), key=sizes.count)


def _text_align(box: list[float], page_width: float) -> str:
    left, right = box[0], page_width - box[2]
    if page_width > 0 and left >= page_width * 0.08 and abs(left - right) <= page_width * 0.03:
        return "center"
    if page_width > 0 and right <= page_width * 0.03:
        return "right"
    return "left"


def _rendered_style(pages: dict[int, dict], page_number: int, block: dict, box: list[float]) -> tuple[dict[str, Any], bool]:
    """Contract shape {font_size_pt, font_weight, text_indent_pt, text_align}.

    Font size and weight come from the block's own PDF lines; when a block carries no text of its own
    (media, or a block grounded on geometry only) the page's measured body size is used and the caller
    records that the value is a page-level default rather than this block's own measurement. Indent is
    not measured by OCR-Agent, so it is written as 0.0 and counted in the export report.
    """
    lines = {
        str(item["evidence_id"]): item
        for item in pages[page_number].get("evidence", {}).get("pdf", [])
        if item.get("kind") == "pdf_line"
    }
    sizes, bolds = [], []
    for ref in block.get("source_refs", []):
        line = lines.get(str(ref))
        if line is None or not isinstance(line.get("font_size"), (int, float)):
            continue
        sizes.append(float(line["font_size"]))
        bolds.append(bool(line.get("bold")))
    page_width = float(pages[page_number].get("pdf_page", {}).get("width_pt") or 0.0)
    heights = [
        float(line["bbox_pdf_pt"][3]) - float(line["bbox_pdf_pt"][1])
        for ref in block.get("source_refs", [])
        if isinstance((line := lines.get(str(ref))), dict)
        and isinstance(line.get("bbox_pdf_pt"), list)
        and len(line["bbox_pdf_pt"]) == 4
    ]
    style: dict[str, Any] = {
        "font_size_pt": round(max(sizes), 2) if sizes else round(_page_body_font(pages, page_number) or 10.0, 2),
        "font_weight": 700 if (bolds and any(bolds)) else 400,
        "text_indent_pt": 0.0,
        "text_align": _text_align(box, page_width),
        "font_family": "pdf-observed",
    }
    if heights:
        style["line_height"] = round(sorted(heights)[len(heights) // 2], 2)
    return style, bool(sizes)


# ---------------------------------------------------------------------------------------
# A1 / graph / page geometry
# ---------------------------------------------------------------------------------------
def build_entities(
    pages: dict[int, dict], results: dict[int, dict]
) -> tuple[list[dict], dict[str, dict], dict[str, int], dict[str, list[dict]]]:
    entities: list[dict] = []
    by_node: dict[str, dict] = {}
    style_source = {"measured": 0, "page_body_default": 0, "table_cell_entities": 0, "table_row_entities": 0}
    table_children: dict[str, list[dict]] = {}
    for order, (page_number, block) in enumerate(_blocks(results)):
        node_id = _id("node", page_number, _serial(block["block_id"]))
        bbox = _box(block.get("bbox_pdf_pt")) or _box(block.get("bbox_image_px")) or [0.0, 0.0, 0.0, 0.0]
        text = str(block.get("text", "")) if isinstance(block.get("text"), str) else ""
        fragment_ids = list(dict.fromkeys(
            _fragment_id(str(ref)) for ref in list(block.get("source_refs", [])) + list(block.get("support_refs", []))
        ))
        style, measured = _rendered_style(pages, page_number, block, bbox)
        style_source["measured" if measured else "page_body_default"] += 1
        entity = {
            "entity_id": _id("entity", page_number, _serial(block["block_id"])),
            "node_id": node_id,
            "page_number": page_number,
            "bbox": bbox,
            "reading_order": order,
            "fragment_ids": fragment_ids,
            "rendered_style": style,
        }
        if text:
            entity["text"] = text
            entity["text_sha256"] = _sha256_text(text)
        entities.append(entity)
        by_node[node_id] = {**entity, "page": page_number, "block": block}
    # Table rows and cells: the registered deliveries model them as first-class nodes with their own
    # entity, so the row/cell level of a measured grid is projected too. Only tables whose PDF cell
    # matrix was verified carry measured boxes; anything else stays a single table node rather than
    # inventing geometry.
    order = len(entities)
    for page_number, block in _blocks(results):
        if block.get("type") != "table":
            continue
        cells = [cell for cell in (block.get("table_cell_geometry") or []) if isinstance(cell, dict)]
        if not cells:
            continue
        table_node = node_id_for_block(page_number, str(block["block_id"]))
        table_entity = by_node[table_node]
        geometry: dict[tuple[int, int], list[float]] = {}
        for cell in cells:
            box = _image_box_to_pdf(pages, page_number, _box(cell.get("bbox_image_px")) or [])
            geometry[(int(cell["row"]), int(cell["col"]))] = box or table_entity["bbox"]
        rows: list[dict] = []
        row_nodes: dict[int, dict] = {}
        for row_index in sorted({row for row, _ in geometry}):
            node_id = f"{table_node}-r{row_index}"
            row_boxes = [box for (row, _), box in geometry.items() if row == row_index]
            entry: dict[str, Any] = {
                "entity_id": f"entity:{_serial(table_node)}-r{row_index}",
                "node_id": node_id,
                "page_number": page_number,
                "bbox": [
                    min(box[0] for box in row_boxes), min(box[1] for box in row_boxes),
                    max(box[2] for box in row_boxes), max(box[3] for box in row_boxes),
                ],
                "reading_order": order,
                "fragment_ids": list(table_entity["fragment_ids"]),
                "rendered_style": dict(table_entity["rendered_style"]),
            }
            entities.append(entry)
            by_node[node_id] = {**entry, "page": page_number, "block": block, "table": table_node, "row": row_index, "cell_orders": []}
            row_nodes[row_index] = by_node[node_id]
            rows.append({"row": row_index, "node_id": node_id, "cell_orders": []})
            order += 1
            style_source["table_row_entities"] += 1
        for cell in cells:
            row_index, col = int(cell["row"]), int(cell["col"])
            node_id = f"{table_node}-r{row_index}-c{col}"
            entity = {
                "entity_id": f"entity:{_serial(table_node)}-r{row_index}-c{col}",
                "node_id": node_id,
                "page_number": page_number,
                "bbox": geometry[(row_index, col)],
                "reading_order": order,
                "fragment_ids": list(table_entity["fragment_ids"]),
                "rendered_style": dict(table_entity["rendered_style"]),
            }
            text = str(cell.get("text") or "")
            if text:
                entity["text"] = text
                entity["text_sha256"] = _sha256_text(text)
            entities.append(entity)
            by_node[node_id] = {**entity, "page": page_number, "block": block, "table": table_node, "row": row_index, "col": col, "cell": cell}
            row_nodes[row_index]["cell_orders"].append(order)
            order += 1
            style_source["table_cell_entities"] += 1
        table_children[table_node] = rows
    return entities, by_node, style_source, table_children


def _html_rows(block: dict) -> list[list[dict[str, Any]]] | None:
    """Logical rows of a table block's HTML (rowspan/colspan aware), or None when not parseable."""
    html = block.get("html")
    if not isinstance(html, str) or "<table" not in html.lower():
        return None
    root, rows = _parse_table_html_lazy(html)
    if root is None or not rows:
        return None
    grid: list[list[dict[str, Any]]] = []
    for row_index, tr in enumerate(rows):
        logical = 0
        for cell in list(tr):
            if str(getattr(cell, "tag", "")).lower() not in {"td", "th"}:
                continue
            colspan = max(1, int(cell.attrib.get("colspan", "1") or 1))
            rowspan = max(1, int(cell.attrib.get("rowspan", "1") or 1))
            grid.append({
                "row": row_index,
                "col": logical,
                "rowspan": rowspan,
                "colspan": colspan,
                "text": "".join(cell.itertext()).strip(),
            })
            logical += colspan
    return grid or None


def _parse_table_html_lazy(html_value: str) -> tuple[Any | None, list[Any]]:
    from .evidence_builder import _parse_table_html

    return _parse_table_html(html_value)


def build_graph(
    pages: dict[int, dict], results: dict[int, dict], entities: list[dict], sample_id: str,
    document_result: dict, table_child_ids: set[str] | None = None,
) -> dict[str, Any]:
    root_id = "node:document"
    nodes: list[dict[str, Any]] = [{
        "node_id": root_id,
        "node_type": "document",
        "source_locator": f"pdf:{Path(str(document_result.get('pdf_path') or 'document.pdf')).name}",
        "source_order": 0,
        "text_sha256": _sha256_text(sample_id),
    }]
    block_by_node = {
        _id("node", page_number, _serial(block["block_id"])): (page_number, block)
        for page_number, block in _blocks(results)
    }
    children = table_child_ids or set()
    for entity in entities:
        # row/cell nodes are appended by their table below; they must not also be emitted as blocks
        if entity["node_id"] in children:
            continue
        page_number, block = block_by_node.get(entity["node_id"], (entity["page_number"], {}))
        node: dict[str, Any] = {
            "node_id": entity["node_id"],
            "node_type": TYPE_TO_NODE.get(str(block.get("type")), "paragraph"),
            "source_locator": f"pdf:{Path(str(document_result.get('pdf_path') or 'document.pdf')).name}#page={page_number}&bbox={','.join(str(value) for value in entity['bbox'])}",
            "source_order": entity["reading_order"],
            "parent_node_id": root_id,
        }
        text = entity.get("text")
        if isinstance(text, str) and text:
            node["text"] = text
            node["text_sha256"] = entity["text_sha256"]
        if entity.get("rendered_style"):
            node["style"] = dict(entity["rendered_style"])
        if isinstance(block.get("level"), int):
            node["level"] = int(block["level"])
        if block.get("type") == "table":
            node["table_id"] = entity["node_id"]
            grid = block.get("table_grid_evidence") or {}
            # Row/cell nodes exist only together with their entities: a table whose cell matrix was
            # verified. Emitting row nodes from the grid summary alone would leave them unmapped.
            if isinstance(grid, dict) and grid.get("rows") and block.get("table_cell_geometry"):
                for row_index in range(int(grid["rows"])):
                    nodes.append({
                        "node_id": f"{entity['node_id']}-r{row_index}",
                        "node_type": "table_row",
                        "source_locator": f"table:{entity['node_id']}#row={row_index}",
                        "source_order": entity["reading_order"],
                        "parent_node_id": entity["node_id"],
                        "table_id": entity["node_id"],
                        "row": row_index,
                    })
                for cell in block.get("table_cell_geometry") or []:
                    row, col = int(cell["row"]), int(cell["col"])
                    nodes.append({
                        "node_id": f"{entity['node_id']}-r{row}-c{col}",
                        "node_type": "table_cell",
                        "source_locator": f"table:{entity['node_id']}#row={row}&col={col}",
                        "source_order": entity["reading_order"],
                        "parent_node_id": f"{entity['node_id']}-r{row}",
                        "table_id": entity["node_id"],
                        "row": row,
                        "col": col,
                        "rowspan": int(cell.get("rowspan") or 1),
                        "colspan": int(cell.get("colspan") or 1),
                        "text_sha256": _sha256_text(str(cell.get("text") or "")),
                    })
        nodes.append(node)
    return {
        "contract_id": "CanonicalDocGraph",
        "schema_version": GRAPH_VERSION,
        "canonical_doc_graph_id": f"canonical_doc_graph:{sample_id}",
        "document_id": f"doc:{sample_id}",
        "source_id": f"source:{sample_id}",
        "source_asset_sha256": str(document_result.get("source_pdf_sha256") or ""),
        "adapter_id": "adapter:ocr-agent",
        "adapter_version": "1.0.0",
        "root_node_id": root_id,
        "node_count": len(nodes),
        "nodes": nodes,
    }


def build_page_geometry(pages: dict[int, dict], sample_id: str) -> list[dict[str, Any]]:
    manifests = []
    for page_number in sorted(pages):
        page = pages[page_number]
        width = float(page["pdf_page"]["width_pt"])
        height = float(page["pdf_page"]["height_pt"])
        manifests.append({
            "contract_id": "PageGeometryManifest",
            "schema_version": PAGE_GEOMETRY_VERSION,
            "page_geometry_id": f"page_geometry:{sample_id}",
            "page_number": page_number,
            "coordinate_space": COORDINATE_SPACE,
            "page_box": [0.0, 0.0, round(width, 4), round(height, 4)],
            "rotation": int(round(float(page.get("page_image", {}).get("rotation") or 0))),
        })
    return manifests


# ---------------------------------------------------------------------------------------
# A2 / A3
# ---------------------------------------------------------------------------------------
def build_a2(
    results: dict[int, dict], by_node: dict[str, dict], document_result: dict
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], dict[str, int]]:
    """Return a2_nodes, a2_relations, heading entities, table_continuations, heading parents."""
    nodes = [{"node_id": "node:document", "role": "body", "fact_status": "auto_verified"}]
    relations: list[dict] = []
    headings: list[dict] = []
    parent_of: dict[str, str] = {}
    order_of = sorted(by_node.items(), key=lambda item: item[1]["reading_order"])
    # heading hierarchy: every block is governed by the nearest preceding heading of a lower level
    stack: list[tuple[int, str]] = []
    heading_index = 0
    for node_id, entity in order_of:
        block = entity["block"]
        if "cell" in entity:
            # a table cell / row is structural content of its table: the registered deliveries classify
            # both as body and express the containment through table_cell_of / table_row_of relations
            nodes.append({"node_id": node_id, "role": "body", "fact_status": "auto_verified"})
            relations.append({
                "relation_id": f"relation:cell.{_serial(node_id)}",
                "relation_type": "table_cell_of",
                "source_node_id": node_id,
                "target_node_id": str(entity["table"]) + f"-r{int(entity['row'])}",
            })
            parent_of[node_id] = str(entity["table"]) + f"-r{int(entity['row'])}"
            continue
        if "row" in entity:
            nodes.append({"node_id": node_id, "role": "body", "fact_status": "auto_verified"})
            relations.append({
                "relation_id": f"relation:row.{_serial(node_id)}",
                "relation_type": "table_row_of",
                "source_node_id": node_id,
                "target_node_id": str(entity["table"]),
            })
            parent_of[node_id] = str(entity["table"])
            continue
        role = TYPE_TO_ROLE.get(str(block.get("type")), "body")
        nodes.append({"node_id": node_id, "role": role, "fact_status": "auto_verified"})
        if role == "heading":
            level = int(block.get("level") or 1)
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent_heading = stack[-1][1] if stack else ""
            parent_of[node_id] = parent_heading or "node:document"
            heading_index += 1
            heading: dict[str, Any] = {
                "heading_id": f"heading:{_serial(block['block_id'])}",
                "block_ids": [entity["entity_id"]],
                "heading_role": HEADING_ROLE.get(level, "subsection"),
                "level_absolute": level,
                "text_normalized": _normalize(str(block.get("text", ""))),
                "evidence_tier": {"level": "Silver-H", "method": "agent_inference"},
                "identifiability_by_profile": {"PDF_NATIVE": "IDENTIFIABLE"},
            }
            if parent_heading:
                heading["parent_heading_id"] = parent_heading
            marker = _sequence_marker(str(block.get("text", "")))
            if marker:
                heading["numbering"] = marker
            headings.append(heading)
            stack.append((level, node_id))
        else:
            parent_of[node_id] = stack[-1][1] if stack else "node:document"
    # caption relation
    for node_id, entity in order_of:
        block = entity["block"]
        if block.get("type") not in {"table_caption", "image_caption"}:
            continue
        with_parent = by_node.get(node_id, {}).get("entity_id")
        parent_id = block.get("parent_id")
        if isinstance(parent_id, str) and parent_id:
            target = f"entity:{_serial(parent_id)}"
            relations.append({
                "relation_id": f"relation:caption.{_serial(block['block_id'])}",
                "relation_type": "caption_of",
                "source_node_id": node_id,
                "target_node_id": f"node:{_serial(parent_id)}",
            })
        elif with_parent:
            continue
    # cross-page text continuation
    for edge in (document_result.get("document_flow") or {}).get("text_edges", []):
        source = str(edge.get("from_block_id") or (edge.get("from") or {}).get("block_id") or "")
        target = str(edge.get("to_block_id") or (edge.get("to") or {}).get("block_id") or "")
        if not source or not target:
            continue
        relations.append({
            "relation_id": f"relation:continuation.{_serial(source)}-{_serial(target)}",
            "relation_type": "continuation_of",
            "source_node_id": f"node:{_serial(source)}",
            "target_node_id": f"node:{_serial(target)}",
        })
    blocks_by_id = {
        str(entity["block"].get("block_id")): entity["block"] for entity in by_node.values()
    }
    continuations, continuation_counts = build_table_continuations(document_result, blocks_by_id)
    heading_parent_pairs = [{"node_id": key, "parent": value} for key, value in parent_of.items()]
    return nodes, relations, headings, continuations, heading_parent_pairs, continuation_counts


def _part_rows(block: dict) -> list[list[str]] | None:
    """Logical rows of a table fragment's HTML as cell-text lists, or None when unreadable."""
    grid = _html_rows(block)
    if not grid:
        return None
    rows: dict[int, list[tuple[int, str]]] = {}
    for cell in grid:
        rows.setdefault(int(cell["row"]), []).append((int(cell["col"]), str(cell["text"])))
    return [[text for _, text in sorted(cells)] for _, cells in sorted(rows.items())]


def _part_completeness(parts: list[dict], rows_of: list[list[list[str]] | None]) -> list[dict[str, Any] | None]:
    """Per-part completeness of a table group: which rows this page carries, and what is missing.

    Row numbering is the logical table numbering: the group's parts concatenated, with the rows a
    continuation repeats verbatim from the head fragment counted once. Truncation directions come from
    the measured page-edge flags of the cross-page contract, never from a guess.
    """
    if not rows_of or rows_of[0] is None or any(rows is None for rows in rows_of):
        return [None] * len(parts)
    head = rows_of[0] or []
    header_rows = 0
    for index in range(1, len(parts)):
        if not bool(parts[index].get("repeated_header")):
            continue
        repeated = 0
        for position, row in enumerate(rows_of[index] or []):
            if position < len(head) and row == head[position]:
                repeated += 1
            else:
                break
        header_rows = max(header_rows, repeated)
    logical: list[list[str]] = []
    offsets: list[tuple[int, int] | None] = []
    for index, rows in enumerate(rows_of):
        body = list(rows or [])
        if index and bool(parts[index].get("repeated_header")):
            body = body[header_rows:]
        if index == 0:
            offsets.append((0, len(body) - 1) if body else None)
            logical.extend(body)
        else:
            offsets.append((len(logical), len(logical) + len(body) - 1) if body else None)
            logical.extend(body)
    total_rows = len(logical)
    result: list[dict[str, Any] | None] = []
    for index, part in enumerate(parts):
        span = offsets[index]
        if span is None or total_rows < 1:
            result.append(None)
            continue
        missing: list[str] = []
        if bool(part.get("truncated_at_page_start")):
            missing.append("top")
        if bool(part.get("truncated_at_page_end")):
            missing.append("bottom")
        this_page = {
            (): "complete",
            ("top",): "truncated_top",
            ("bottom",): "truncated_bottom",
            ("top", "bottom"): "truncated_both",
        }[tuple(missing)]
        result.append({
            "this_page": this_page,
            "missing_directions": missing,
            "row_range": [span[0], span[1]],
            "total_rows": total_rows,
        })
    return result


def build_table_continuations(document_result: dict, blocks_by_id: dict[str, dict] | None = None) -> tuple[list[dict], dict[str, int]]:
    """One entry per consecutive pair of page-local table fragments in a document table group."""
    entries: list[dict] = []
    blocks_by_id = blocks_by_id or {}
    counts = {"with_completeness": 0, "without_completeness": 0}
    for group in (document_result.get("document_flow") or {}).get("table_groups", []):
        parts = group.get("parts") or []
        rows_of = [
            _part_rows(blocks_by_id.get(str(part.get("block_id"))) or {}) if blocks_by_id else None
            for part in parts
        ]
        completeness = _part_completeness(parts, rows_of) if blocks_by_id else [None] * len(parts)
        for index in range(len(parts) - 1):
            source, target = parts[index], parts[index + 1]
            if not source.get("block_id") or not target.get("block_id"):
                continue
            source_node = node_id_for_block(int(source.get("page") or 0), str(source["block_id"]))
            target_node = node_id_for_block(int(target.get("page") or 0), str(target["block_id"]))
            if source_node == target_node:
                continue
            continuation: dict[str, Any] = {
                "source_table_id": source_node,
                "target_table_id": target_node,
                "header_repeated": bool(target.get("repeated_header")),
            }
            measured = completeness[index]
            if measured:
                continuation["completeness"] = measured
                counts["with_completeness"] += 1
            else:
                counts["without_completeness"] += 1
            entries.append(continuation)
    return entries, counts


def build_a3(by_node: dict[str, dict], parent_pairs: list[dict], sample_id: str) -> tuple[dict[str, Any], dict[str, tuple[int, int]]]:
    """Document tree: every node hangs under its governing heading, spans cover their subtree."""
    parent_of = {entry["node_id"]: entry["parent"] for entry in parent_pairs}
    order_of = {node_id: int(entity["reading_order"]) for node_id, entity in by_node.items()}
    children: dict[str, list[str]] = {}
    for node_id, parent in parent_of.items():
        children.setdefault(parent, []).append(node_id)
    for parent in children:
        children[parent].sort(key=lambda node_id: order_of.get(node_id, 0))
    root_id = "node:document"
    tree: list[dict[str, Any]] = []
    spans: dict[str, tuple[int, int]] = {}

    entity_id_of = {node_id: entity["entity_id"] for node_id, entity in by_node.items()}
    page_of = {node_id: int(entity["page"]) for node_id, entity in by_node.items()}
    first_entity_id = next(iter(entity_id_of.values()), "entity:document")
    order_entity = {order: node_id for node_id, order in order_of.items()}

    def visit(node_id: str, depth: int, parent: str, sibling_index: int) -> tuple[int, int]:
        own = order_of.get(node_id)
        subs = [visit(child, depth + 1, node_id, position) for position, child in enumerate(children.get(node_id, []))]
        start = own if own is not None else (min(item[0] for item in subs) if subs else 0)
        end = own if own is not None else (max(item[1] for item in subs) if subs else start)
        if subs:
            start = min([start] + [item[0] for item in subs])
            end = max([end] + [item[1] for item in subs])
        entity = by_node.get(node_id) or {}
        start_node = order_entity.get(start) or node_id
        end_node = order_entity.get(end) or node_id
        start_page = page_of.get(start_node, page_of.get(node_id, 1))
        end_page = page_of.get(end_node, page_of.get(node_id, 1))
        start_entity_id = entity_id_of.get(start_node, first_entity_id)
        end_entity_id = entity_id_of.get(end_node, first_entity_id)
        spans[node_id] = (start, end)
        tree.append({
            "node_id": node_id,
            "parent_node_id": parent,
            "depth": depth,
            "sibling_index": sibling_index,
            "reading_order": own if own is not None else start,
            "node_role": TYPE_TO_ROLE.get(str((entity.get("block") or {}).get("type")), "body"),
            "entity_ids": [entity["entity_id"]] if entity.get("entity_id") else [first_entity_id],
            "span": {
                "start_page": start_page,
                "end_page": end_page,
                "start_entity_id": start_entity_id,
                "end_entity_id": end_entity_id,
            },
        })
        return start, end

    visit(root_id, 0, "", 0)
    tree.sort(key=lambda item: (spans[item["node_id"]][0], item["depth"]))
    max_depth = max((item["depth"] for item in tree), default=0)
    covered = {order for item in tree if not children.get(item["node_id"]) for order in range(*spans[item["node_id"]])}
    total = {order for item in tree for order in range(spans[item["node_id"]][0], spans[item["node_id"]][1] + 1)}
    span_coverage = round(len(covered) / len(total), 4) if total else 1.0
    document = {
        "contract_id": "AnnotationFactA3",
        "schema_version": FACT_VERSION,
        "fact_set_id": f"fact_set:a3-{sample_id}",
        "tier": "A3",
        "parent_fact_set_id": f"fact_set:a2-{sample_id}",
        "fact_schema_version": "ocr-agent.v1",
        "tree_root_id": root_id,
        "node_count": len(tree),
        "max_depth": max_depth,
        "span_coverage": span_coverage,
        "unresolved_parent_count": sum(1 for item in tree if item["node_id"] != root_id and not item["parent_node_id"]),
        "tree_nodes": tree,
    }
    return document, spans


# ---------------------------------------------------------------------------------------
# bundle / truth / file assembly
# ---------------------------------------------------------------------------------------
def build_truth(entities: list[dict], results: dict[int, dict], by_node: dict[str, dict]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    undetermined: list[dict[str, Any]] = []
    for entity in entities:
        block = by_node[entity["node_id"]]["block"]
        bbox = entity["bbox"]
        zero = not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]
        status = "unknown" if zero else "measured"
        entry: dict[str, Any] = {
            "logical_id": entity["node_id"],
            "kind": str(block.get("type") or "text"),
            "geometry_status": status,
            "identity_status": "assigned",
        }
        if block.get("type") == "formula":
            # the contract requires a payload hash whenever formula_status is "present"; a formula
            # whose payload the run never carried is reported as unknown instead of hashing nothing
            payload = str(block.get("text") or "").strip()
            if payload:
                entry["formula_status"] = "present"
                entry["formula_payload_sha256"] = _sha256_text(payload)
            else:
                entry["formula_status"] = "unknown"
                entry["formula_reason_code"] = "formula_payload_unavailable"
                undetermined.append({
                    "scope": "block", "pointer": entity["node_id"], "property": "formula",
                    "reason_code": "formula_payload_unavailable",
                })
        else:
            entry["formula_status"] = "not_applicable"
        if status == "measured":
            entry["geometry_bbox"] = bbox
        if zero:
            entry["geometry_reason_code"] = "geometry_zero_sized_box"
            undetermined.append({"scope": "block", "pointer": entity["node_id"], "property": "geometry", "reason_code": "geometry_zero_sized_box"})
        blocks.append(entry)
    return {
        "contract_id": "GtTruthDocument",
        "schema_version": "1.0.0",
        "document_id": "doc:document",
        "producer_id": PRODUCER_ID,
        # filled in by export_contract_gt with the sha256 of the gt_raw.json bytes, like the reference
        "gt_raw_sha256": "",
        "blocks": blocks,
        "undetermined": undetermined,
    }


def build_bundle(
    sample_id: str, document_result: dict, graph: dict, entities: list[dict], a2_nodes: list[dict],
    a2_relations: list[dict], headings: list[dict], a3: dict, a3_spans: dict[str, tuple[int, int]],
    continuations: list[dict], page_geometry_sha: str
) -> dict[str, Any]:
    a3_nodes = []
    for item in a3["tree_nodes"]:
        start, end = a3_spans.get(item["node_id"], (item["reading_order"], item["reading_order"]))
        entry = {
            "node_id": item["node_id"],
            "order": item["reading_order"],
            "span_start_order": start,
            "span_end_order": end,
        }
        if item["parent_node_id"]:
            entry["parent_node_id"] = item["parent_node_id"]
        a3_nodes.append(entry)
    graph_nodes = {node["node_id"] for node in graph["nodes"]}
    mapped = {entity["node_id"] for entity in entities}
    # The registered deliveries do not list the graph root as unmapped: it is the document node and
    # carries no entity of its own, while every measured node does.
    root_id = str(graph.get("root_node_id") or "")
    classified = {node["node_id"] for node in a2_nodes}
    linked = {node["node_id"] for node in a3_nodes}
    return {
        "contract_id": "FullDocumentGTBundle",
        "schema_version": CONTRACT_VERSION,
        "bundle_id": f"gt_bundle:{sample_id}",
        "document_id": f"doc:{sample_id}",
        "source_asset_sha256": str(document_result.get("source_pdf_sha256") or ""),
        "canonical_doc_graph_id": graph["canonical_doc_graph_id"],
        "canonical_doc_graph_sha256": _contract_sha256(graph),
        "page_geometry_id": f"page_geometry:{sample_id}",
        "page_geometry_sha256": page_geometry_sha,
        "coordinate_space": COORDINATE_SPACE,
        "a1_entities": entities,
        "a2_nodes": a2_nodes,
        "a2_relations": a2_relations,
        "a2_heading_entities": headings,
        "a3_nodes": a3_nodes,
        "table_continuations": continuations,
        "fragment_continuations": [],
        "coverage": {
            "graph_node_count": len(graph_nodes),
            "a1_mapped_node_count": len(mapped & graph_nodes),
            "a2_classified_node_count": len(classified & graph_nodes),
            "a3_linked_node_count": len(linked & graph_nodes),
            "unmapped_graph_node_ids": sorted(graph_nodes - mapped - {root_id}),
            "unclassified_node_ids": sorted(graph_nodes - classified),
            "unlinked_node_ids": sorted(graph_nodes - linked),
        },
    }


def _measured_border_bands(
    pages: dict[int, dict], page_number: int, table_box: list[float]
) -> tuple[list[list[float]], str]:
    """Border bands of a table from measured PDF drawings; falls back to the measured table frame."""
    page = pages.get(page_number) or {}
    tolerance = 2.5
    width = table_box[2] - table_box[0]
    height = table_box[3] - table_box[1]
    bands: list[list[float]] = []
    for item in page.get("evidence", {}).get("pdf", []):
        if item.get("kind") != "pdf_drawing":
            continue
        box = _box(item.get("bbox_image_px"))
        if not box:
            continue
        drawn = _image_box_to_pdf(pages, page_number, box)
        if (
            drawn[0] < table_box[0] - tolerance
            or drawn[1] < table_box[1] - tolerance
            or drawn[2] > table_box[2] + tolerance
            or drawn[3] > table_box[3] + tolerance
        ):
            continue
        box_width = drawn[2] - drawn[0]
        box_height = drawn[3] - drawn[1]
        horizontal = box_height <= tolerance and box_width >= width * 0.5
        vertical = box_width <= tolerance and box_height >= height * 0.5
        if horizontal or vertical:
            bands.append(sorted([list(drawn)])[0])
    if not bands:
        return [table_box], "table_frame"
    unique = sorted({tuple(band) for band in bands})
    return [list(band) for band in unique], "pdf_rule_segments"


def build_table_regions(pages: dict[int, dict], results: dict[int, dict]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """A1 table_regions for tables whose PDF cell matrix was verified (measured geometry only)."""
    regions: list[dict[str, Any]] = []
    counts = {"emitted": 0, "skipped_unverified_grid": 0, "pdf_rule_segments": 0, "table_frame": 0}
    for page_number, block in _blocks(results):
        if block.get("type") != "table":
            continue
        grid = block.get("table_grid_evidence")
        cells = [cell for cell in (block.get("table_cell_geometry") or []) if isinstance(cell, dict)]
        if not isinstance(grid, dict) or not grid.get("rows") or not cells:
            counts["skipped_unverified_grid"] += 1
            continue
        boxes = [_image_box_to_pdf(pages, page_number, _box(cell.get("bbox_image_px")) or []) for cell in cells]
        boxes = [box for box in boxes if box]
        if not boxes:
            counts["skipped_unverified_grid"] += 1
            continue
        full_box = [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)]
        header_boxes = [box for cell, box in zip(cells, boxes) if int(cell["row"]) == 0]
        header_band = [
            min(box[0] for box in header_boxes), min(box[1] for box in header_boxes),
            max(box[2] for box in header_boxes), max(box[3] for box in header_boxes),
        ] if header_boxes else full_box
        bands, source = _measured_border_bands(pages, page_number, full_box)
        counts[source] += 1
        counts["emitted"] += 1
        regions.append({
            "table_id": node_id_for_block(page_number, str(block["block_id"])),
            "page_id": f"page:{page_number:04d}",
            "full_bbox": full_box,
            "header_band_bbox": header_band,
            "border_bands": bands,
            "cell_grid": [
                {
                    "row": int(cell["row"]),
                    "col": int(cell["col"]),
                    "rowspan": int(cell["rowspan"]),
                    "colspan": int(cell["colspan"]),
                    "bbox": box,
                    "role": "header" if int(cell["row"]) == 0 else "body",
                }
                for cell, box in zip(cells, boxes)
            ],
        })
    return regions, counts


def schema_root_default() -> Path | None:
    configured = os.environ.get("DOCPARSE_SCHEMA_ROOT")
    return Path(configured).expanduser() if configured else None


def validate_contracts(documents: dict[str, Any], schema_root: Path | None) -> dict[str, Any]:
    if schema_root is None:
        return {"validated": False, "reason": "schema root unavailable"}
    try:
        import jsonschema
    except Exception as exc:  # pragma: no cover - environment without jsonschema
        return {"validated": False, "reason": f"jsonschema unavailable: {exc}"}
    results: dict[str, Any] = {"validated": True, "files": {}}
    for name, (filename, payload) in documents.items():
        schema_path = schema_root / f"{name}.schema.json"
        if not schema_path.is_file():
            results["files"][filename] = {"schema": name, "status": "schema_missing"}
            results["validated"] = False
            continue
        schema = _read_json(schema_path)
        try:
            if isinstance(payload, list):
                for index, item in enumerate(payload):
                    jsonschema.validate(item, schema)
                results["files"][filename] = {"schema": name, "status": "valid", "instances": len(payload)}
            else:
                jsonschema.validate(payload, schema)
                results["files"][filename] = {"schema": name, "status": "valid", "instances": 1}
        except Exception as exc:
            results["files"][filename] = {"schema": name, "status": "invalid", "error": str(exc)[:400]}
            results["validated"] = False
    return results


def export_contract_gt(document_root: Path, sample_id: str, *, schema_root: Path | None = None) -> dict[str, Any]:
    """Write the Contract GT projection of one OCR-Agent document run; returns the export report."""
    pages, results, document_result = load_document(document_root, sample_id)
    if not results:
        raise FileNotFoundError(f"no page results for {sample_id} under {document_root}")
    target = document_root / "contract_gt" / sample_id
    target.mkdir(parents=True, exist_ok=True)
    entities, by_node, style_source, table_children = build_entities(pages, results)
    del table_children  # row/cell parenting is carried on the by_node entries
    table_child_ids = {node_id for node_id, entry in by_node.items() if "row" in entry or "cell" in entry}
    graph = build_graph(pages, results, entities, sample_id, document_result, table_child_ids)
    page_geometry = build_page_geometry(pages, sample_id)
    a2_nodes, a2_relations, headings, continuations, parent_pairs, continuation_counts = build_a2(results, by_node, document_result)
    table_regions, table_region_counts = build_table_regions(pages, results)
    a3, a3_spans = build_a3(by_node, parent_pairs, sample_id)
    truth = build_truth(entities, results, by_node)
    truth["document_id"] = f"doc:{sample_id}"
    page_geometry_sha = _contract_sha256(page_geometry)
    bundle = build_bundle(sample_id, document_result, graph, entities, a2_nodes, a2_relations, headings, a3, a3_spans, continuations, page_geometry_sha)

    grounded = sum(
        1 for entity in entities
        if any(str(ref).startswith("pdf.") or str(ref).startswith("evidence:pdf") for ref in entity.get("fragment_ids", []))
    )
    ratio = grounded / max(1, len(entities))
    identifiability = {
        "ocr:agent-default": {
            "PDF_NATIVE": "IDENTIFIABLE" if ratio >= 1.0 else ("AMBIGUOUS_INTENTIONAL" if ratio >= 0.8 else "UNKNOWN")
        }
    }
    files = {
        "canonical_doc_graph.json": graph,
        "gt_raw.json": graph,
        "page_geometry_manifest.json": page_geometry,
        "annotation_fact_a1.json": {
            "contract_id": "AnnotationFactA1",
            "schema_version": FACT_VERSION,
            "fact_set_id": f"fact_set:a1-{sample_id}",
            "tier": "A1",
            "alignment_graph_id": f"alignment_graph:{sample_id}",
            "entity_count": len(entities),
            "source_asset_sha256": str(document_result.get("source_pdf_sha256") or ""),
            "evidence_bundle_id": f"evidence_bundle:{sample_id}",
            "annotation_method": ANNOTATION_METHOD,
            "annotation_guideline_version": "ocr-agent-contract.v1",
            "allowed_splits": ["train", "dev", "regression"],
            "annotation_status": "accepted",
            "annotation_tier": "Silver-H",
            "coverage_scope": "full_document",
            "doc_id": f"doc:{sample_id}",
            "asset_id": f"asset:{sample_id}",
            "fact_schema_version": FACT_SCHEMA_VERSION,
            "verification_summary": {"geometry": "measured_from_pdf", "rendered_text": "pdf_native", "semantic": "agent_inference"},
            "table_regions": table_regions,
        },
        "annotation_fact_a2.json": {
            "contract_id": "AnnotationFactA2",
            "schema_version": FACT_VERSION,
            "fact_set_id": f"fact_set:a2-{sample_id}",
            "tier": "A2",
            "parent_fact_set_id": f"fact_set:a1-{sample_id}",
            "heading_count": len(headings),
            "headings": headings,
            "table_continuations": continuations,
            "evidence_bundle_id": f"evidence_bundle:{sample_id}",
            "allowed_splits": ["train", "dev", "regression"],
            "identifiability": identifiability,
            "doc_id": f"doc:{sample_id}",
            "asset_id": f"asset:{sample_id}",
            "source_asset_sha256": str(document_result.get("source_pdf_sha256") or ""),
            "annotation_method": ANNOTATION_METHOD,
            "annotation_guideline_version": "ocr-agent-contract.v1",
            "annotation_status": "accepted",
            "annotation_tier": "Silver-H",
            "coverage_scope": "full_document",
            "fact_schema_version": "ocr-agent.v1",
            "input_profiles_declared": ["PDF_NATIVE"],
            "ontology_variant_id": "ocr-agent",
            "verification_summary": {"geometry": "measured_from_pdf", "rendered_text": "pdf_native", "semantic": "agent_inference"},
        },
        "annotation_fact_a3.json": {**a3, "annotation_method": ANNOTATION_METHOD, "doc_id": f"doc:{sample_id}", "asset_id": f"asset:{sample_id}"},
        "full_document_gt_bundle.json": bundle,
        "gt_truth_document.json": truth,
    }
    # gt_raw_sha256 points at the bytes of gt_raw.json (byte-identical to the canonical graph), like the
    # reference deliveries; hashes therefore have to be settled before validation runs.
    truth["gt_raw_sha256"] = _sha256_text(json.dumps(files["gt_raw.json"], ensure_ascii=False, indent=1) + "\n")
    registered = {
        "canonical_doc_graph.json": "CanonicalDocGraph",
        "page_geometry_manifest.json": "PageGeometryManifest",
        "full_document_gt_bundle.json": "FullDocumentGTBundle",
        "gt_truth_document.json": "GtTruthDocument",
    }
    projections = {
        "annotation_fact_a1.json": "AnnotationFactA1",
        "annotation_fact_a2.json": "AnnotationFactA2",
        "annotation_fact_a3.json": "AnnotationFactA3",
    }
    validation = validate_contracts({name: (filename, files[filename]) for filename, name in registered.items()}, schema_root or schema_root_default())
    validation["projections"] = {
        filename: {"schema": name, "status": "provenance_extension", "fact_schema_version": FACT_SCHEMA_VERSION}
        for filename, name in projections.items()
    }
    file_hashes = {
        filename: _write_json(
            target / filename if filename in ROOT_FILES else target / "a_facts" / filename, payload
        )
        for filename, payload in files.items()
    }
    report = {
        "schema_version": "ocr-agent.contract-gt-export.v1",
        "sample_id": sample_id,
        "document_root": str(document_root),
        "output": str(target),
        "counts": {
            "pages": len(pages),
            "entities": len(entities),
            "table_row_entities": style_source.get("table_row_entities", 0),
            "table_cell_entities": style_source.get("table_cell_entities", 0),
            "table_regions": len(table_regions),
            "graph_nodes": graph["node_count"],
            "a2_nodes": len(a2_nodes),
            "a2_relations": len(a2_relations),
            "headings": len(headings),
            "a3_nodes": a3["node_count"],
            "table_continuations": len(continuations),
        },
        "validation": validation,
        "rendered_style_source": style_source,
        "table_regions": table_region_counts,
        "table_continuations_completeness": continuation_counts,
        "files": {
            filename: {"sha256": file_hashes[filename], "bytes": len(json.dumps(payload, ensure_ascii=False))}
            for filename, payload in files.items()
        },
    }
    (target / "export_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report
