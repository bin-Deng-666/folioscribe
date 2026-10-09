import importlib.util
import json
from pathlib import Path
import pytest


SERVER_PATH = Path(__file__).parents[1] / "viewer" / "server.py"
spec = importlib.util.spec_from_file_location("ocr_agent_viewer_server", SERVER_PATH)
viewer_server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(viewer_server)
OCRAgentCatalog = viewer_server.OCRAgentCatalog


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_catalog(tmp_path: Path, status: str = "PASS") -> OCRAgentCatalog:
    root = tmp_path / "pilot"
    result = {
        "sample_id": "rd_test",
        "page": 1,
        "status": status,
        "task_count": 0,
        "revision": {"blocks": [], "field_reviews": {}, "review_items": []},
        "validation": {"error_count": 0, "review_count": 0, "unresolved_field_count": 0},
    }
    evidence = {
        "page_image": {"path": str(root / "page.png"), "width": 100, "height": 200},
        "pdf_page": {"width_pt": 50, "height_pt": 100},
        "evidence": {"pdf": [], "mineru": [], "paddle": [], "ovis": [], "anydoc": [], "tools": []},
    }
    write_json(root / "runs" / "rd_test" / "page_0001" / "result.json", result)
    write_json(root / "evidence" / "rd_test" / "page_0001.json", evidence)
    (root / "page.png").write_bytes(b"png")
    return OCRAgentCatalog(root)


def test_viewer_uses_automatic_status_without_override(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path, "CHECK")
    page = catalog.list_pages()["documents"][0]["pages"][0]
    assert page["status"] == "CHECK"


def test_viewer_sets_one_final_status(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path, "CHECK")
    result = catalog.set_status("rd_test", 1, {"status": "PASS", "note": "checked"})
    assert result["status"] == "PASS"
    assert catalog.list_pages()["documents"][0]["pages"][0]["status"] == "PASS"

    result = catalog.set_status("rd_test", 1, {"status": "FAIL", "note": "wrong type"})
    assert result["status"] == "FAIL"
    assert catalog.list_pages()["documents"][0]["pages"][0]["status"] == "FAIL"


def test_viewer_only_reviews_automatic_check(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path, "PASS")
    with pytest.raises(ValueError, match="automatic CHECK"):
        catalog.set_status("rd_test", 1, {"status": "FAIL", "note": ""})


def make_annotatable_catalog(tmp_path: Path) -> OCRAgentCatalog:
    catalog = make_catalog(tmp_path)
    root = catalog.root
    evidence = {
        "page_image": {"path": str(root / "page.png"), "width": 1000, "height": 2000},
        "pdf_page": {"width_pt": 500.0, "height_pt": 1000.0},
        "evidence": {"pdf": [], "mineru": [], "paddle": [], "ovis": [], "anydoc": [], "tools": []},
    }
    write_json(root / "evidence" / "rd_test" / "page_0001.json", evidence)
    return catalog


def test_annotation_round_trip_normalizes_geometry(tmp_path: Path) -> None:
    catalog = make_annotatable_catalog(tmp_path)
    saved = catalog.save_annotations("rd_test", 1, {
        "annotator": "reviewer",
        "blocks": [{
            "block_id": "manual.block.p1.0001", "type": "title", "level": 2, "text": "新增标题",
            "bbox_image_px": [100, 200, 400, 260], "reading_order": 0,
        }],
        "operations": [{"kind": "add", "block_id": "manual.block.p1.0001", "fields": ["type", "level", "text"]}],
    })
    assert saved["exists"] is True and saved["revision"] == 1
    assert saved["operations"][0]["kind"] == "add" and saved["operations"][0]["annotator"] == "reviewer"
    block = saved["blocks"][0]
    # the derived views are computed by the server from the page geometry
    assert block["bbox_pdf_pt"] == [50.0, 100.0, 200.0, 130.0]
    assert block["bbox_normalized"] == [0.1, 0.1, 0.4, 0.13]
    assert catalog.load_annotations("rd_test", 1)["annotated_blocks"] == 1


def test_annotation_rejects_schema_violations(tmp_path: Path) -> None:
    catalog = make_annotatable_catalog(tmp_path)
    base = {"block_id": "x", "bbox_image_px": [10, 20, 110, 60], "reading_order": 0}
    with pytest.raises(ValueError, match="title requires level"):
        catalog.save_annotations("rd_test", 1, {"blocks": [{**base, "type": "title", "text": "t"}], "operations": []})
    with pytest.raises(ValueError, match="only title may contain level"):
        catalog.save_annotations("rd_test", 1, {"blocks": [{**base, "type": "text", "text": "t", "level": 2}], "operations": []})
    with pytest.raises(ValueError, match="requires text"):
        catalog.save_annotations("rd_test", 1, {"blocks": [{**base, "type": "text"}], "operations": []})
    with pytest.raises(ValueError, match="requires HTML table"):
        catalog.save_annotations("rd_test", 1, {"blocks": [{**base, "type": "table"}], "operations": []})
    with pytest.raises(ValueError, match="outside the page"):
        catalog.save_annotations("rd_test", 1, {"blocks": [{**base, "type": "image", "bbox_image_px": [0, 0, 5000, 60]}], "operations": []})
    with pytest.raises(ValueError, match="duplicate block_id"):
        catalog.save_annotations("rd_test", 1, {"blocks": [{**base, "type": "image"}, {**base, "type": "image"}], "operations": []})
    with pytest.raises(ValueError, match="unsupported kind"):
        catalog.save_annotations("rd_test", 1, {"blocks": [], "operations": [{"kind": "explode", "block_id": "x"}]})


def test_annotation_clear_and_summary(tmp_path: Path) -> None:
    catalog = make_annotatable_catalog(tmp_path)
    catalog.save_annotations("rd_test", 1, {
        "annotator": "a", "blocks": [{"block_id": "m1", "type": "image", "bbox_image_px": [10, 20, 110, 60], "reading_order": 0}],
        "operations": [{"kind": "add", "block_id": "m1"}],
    })
    summary = catalog.annotation_summary()
    assert summary["annotated_pages"] == 1
    assert summary["pages"][0]["operation_kinds"] == {"add": 1}
    cleared = catalog.clear_annotations("rd_test", 1)
    assert cleared["exists"] is False and catalog.annotation_summary()["annotated_pages"] == 0


def test_annotation_flags_a_stale_base(tmp_path: Path) -> None:
    catalog = make_annotatable_catalog(tmp_path)
    catalog.save_annotations("rd_test", 1, {"blocks": [{"block_id": "m1", "type": "image", "bbox_image_px": [10, 20, 110, 60], "reading_order": 0}], "operations": []})
    assert catalog.load_annotations("rd_test", 1)["stale"] is False
    # the chain result changes afterwards (e.g. the sample is rebuilt)
    result_path = catalog.root / "runs" / "rd_test" / "page_0001" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["revision"]["blocks"] = [{"block_id": "chain1", "type": "text", "text": "changed", "bbox_image_px": [1, 2, 3, 4], "reading_order": 0}]
    write_json(result_path, result)
    assert catalog.load_annotations("rd_test", 1)["stale"] is True


def test_annotation_rejects_a_bad_page_or_sample(tmp_path: Path) -> None:
    catalog = make_annotatable_catalog(tmp_path)
    with pytest.raises(ValueError, match="invalid sample or page"):
        catalog.annotation_path("../escape", 1)
    with pytest.raises(FileNotFoundError):
        catalog.save_annotations("rd_absent", 1, {"blocks": [], "operations": []})


def test_document_annotation_verdict(tmp_path: Path) -> None:
    catalog = make_annotatable_catalog(tmp_path)
    assert catalog.load_document_annotation("rd_test")["exists"] is False
    catalog.save_annotations("rd_test", 1, {"blocks": [{"block_id": "m1", "type": "image", "bbox_image_px": [10, 20, 110, 60], "reading_order": 0}], "operations": []})
    saved = catalog.set_document_annotation("rd_test", {"status": "PASS", "note": "层级正确", "annotator": "reviewer"})
    assert saved["status"] == "PASS" and saved["annotated_pages"] == 1
    summary = catalog.annotation_summary()
    assert summary["document_status"]["rd_test"]["status"] == "PASS"
    assert summary["documents"] == [{"sample_id": "rd_test", "annotated_pages": 1}]
    with pytest.raises(ValueError, match="document status"):
        catalog.set_document_annotation("rd_test", {"status": "MAYBE"})


def _viewer_colors() -> dict:
    """Parse the type → colour table straight out of the shipped app.js (single source of truth)."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "viewer" / "static" / "app.js").read_text(encoding="utf-8")
    block = source[source.index("const COLORS = {"):source.index("};", source.index("const COLORS = {"))]
    return {name: value for name, value in re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', block)}


def _lab(hex_color: str) -> tuple[float, float, float]:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(int(hex_color[i:i + 2], 16)) for i in (1, 3, 5))
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _hue(hex_color: str) -> float:
    import colorsys

    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[0] * 360


def test_viewer_block_colors_stay_distinguishable():
    """The bbox colours must stay far apart: annotators identify a type by its colour."""
    import itertools
    import math

    colors = _viewer_colors()
    # only the block-type entries: pdf_* / alias roles may legitimately reuse another type's colour
    types = ["text", "title", "table", "image", "chart", "table_caption", "image_caption", "header",
             "footer", "page_number", "footnote", "formula", "seal", "unknown"]
    assert all(name in colors for name in types), sorted(colors)
    palette = {name: colors[name] for name in types}
    assert len(set(palette.values())) == len(palette)      # no two types share a colour
    lab = {name: _lab(value) for name, value in palette.items()}
    distances = {
        (a, b): math.dist(lab[a], lab[b])
        for a, b in itertools.combinations(palette, 2)
    }
    worst_pair, worst = min(distances.items(), key=lambda item: item[1])
    assert worst >= 18.0, f"{worst_pair} are only ΔE {worst:.1f} apart"
    # colours that are close in ΔE must still differ clearly in hue (or in lightness)
    for (a, b), distance in distances.items():
        if distance >= 35.0:
            continue
        gap = abs(_hue(palette[a]) - _hue(palette[b])) % 360
        gap = min(gap, 360 - gap)
        lightness = abs(lab[a][0] - lab[b][0])
        assert gap >= 14.0 or lightness >= 12.0, f"{a}/{b}: ΔE {distance:.1f}, hue {gap:.1f}°, L* {lightness:.1f}"


def test_viewer_shortcut_bar_matches_implemented_keys():
    """The legend is documentation: it must not promise keys the app does not implement."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "viewer" / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "viewer" / "static" / "app.js").read_text(encoding="utf-8")
    bar = html[html.index('id="annotateHint"'):]
    bar = bar[:bar.index("</div>")]
    # every key the legend advertises is actually handled
    # the legend is deliberately short: page flipping, framing, editing, delete/reset, undo/save
    for key in ["<kbd>B</kbd>", "<kbd>Enter</kbd>", "<kbd>Esc</kbd>", "<kbd>Delete</kbd>", "<kbd>R</kbd>",
                "<kbd>Ctrl+Z</kbd>", "<kbd>Ctrl+S</kbd>", "<kbd>Q</kbd>",
                "<kbd>E</kbd>", "<kbd>A</kbd>", "<kbd>D</kbd>", "<kbd>Alt</kbd>", "<kbd>Shift</kbd>"]:
        assert key in bar, f"legend lost {key}"
    # "drag empty space to create" is no longer true: creation only happens in framing mode (B)
    assert "拖拽空白处" not in bar
    assert "框选" in bar
    # the legend must not describe an "insert after" pick without the matching key binding
    for token in ['key === "b"', 'key === "o"', 'key === "r"', 'key === "delete"', 'event.key === "Enter"']:
        assert token in script, f"keyboard handler lost {token}"


def test_viewer_accepts_deeper_title_levels(tmp_path):
    """Humans may mark L5/L6 even though the pipeline contract stops at 4."""
    catalog = make_catalog(tmp_path)
    # the fixture page is 100x200 image px / 50x100 pt, so the box must live inside it
    block = {
        "block_id": "candidate.block.p1.0000",
        "type": "title",
        "level": 6,
        "text": "第 1.1.1.1.1 条",
        "bbox_image_px": [1.0, 1.0, 45.0, 20.0],
        "reading_order": 0,
        "source_refs": [],
        "support_refs": [],
    }
    stored = catalog.save_annotations("rd_test", 1, {"annotator": "tester", "blocks": [block], "operations": []})
    assert stored["blocks"][0]["level"] == 6
    assert stored["max_title_level"] == 6


def test_pipeline_still_rejects_level_above_contract(tmp_path):
    catalog = make_catalog(tmp_path)
    geometry, _ = catalog._page_geometry("rd_test", 1)
    from ocr_agent.schemas import SchemaError, validate_block

    from ocr_agent.schemas import add_coordinate_views

    block = {"block_id": "b", "type": "title", "level": 5, "text": "x", "bbox_image_px": [1.0, 1.0, 10.0, 10.0], "reading_order": 0}
    add_coordinate_views(block, geometry)                        # derived views are the server's job
    with pytest.raises(SchemaError):
        validate_block(block, page=geometry)                     # pipeline contract keeps the cap at 4
    validate_block(block, page=geometry, max_title_level=6)      # the annotation layer relaxes it explicitly
