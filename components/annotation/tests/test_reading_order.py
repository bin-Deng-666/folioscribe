"""Reading order: column detection, furniture policy, and model-order corroboration."""
from __future__ import annotations

from ocr_agent.reading_order import audit_order, build_page_order, detect_layout, order_blocks, pairwise_concordance

PAGE_W = 1200.0
PAGE_H = 1600.0


def block(block_id: str, box: list[float], kind: str = "text") -> dict:
    return {"block_id": block_id, "bbox_image_px": box, "type": kind}


def two_column_page() -> list[dict]:
    """Left column fully read before the right column, with a full-width title and running furniture."""
    return [
        block("header", [200, 20, 1000, 45], "header"),
        block("page", [1080, 20, 1130, 45], "page_number"),
        block("title", [80, 120, 1120, 170], "title"),
        block("authors", [150, 190, 1050, 215], "text"),          # full-width separator
        block("left1", [70, 300, 560, 600]),
        block("left2", [70, 620, 560, 900]),
        block("right1", [640, 300, 1130, 620]),
        block("right2", [640, 640, 1130, 950]),
        block("footer", [70, 1540, 560, 1565], "footer"),
        block("footer_url", [640, 1540, 1130, 1565], "footer"),
    ]


def test_two_columns_are_read_left_then_right():
    blocks = two_column_page()
    layout = detect_layout(blocks, PAGE_W, PAGE_H)
    assert layout.columns == 2 and layout.method == "gutter_columns"
    order = order_blocks(blocks, layout, PAGE_W, PAGE_H)
    assert order == ["header", "page", "title", "authors", "left1", "left2", "right1", "right2", "footer", "footer_url"]


def test_furniture_is_first_and_last_even_when_models_list_it_at_the_end():
    """MinerU appends header/footer at the end of its page list; the policy still pins them."""
    blocks = two_column_page()
    result = build_page_order(blocks, PAGE_W, PAGE_H, model_orders={"mineru": ["left1", "left2", "right1", "right2", "title", "authors", "header", "page", "footer", "footer_url"]})
    order = result["order"]
    assert order[0] == "header" and order[1] == "page", order
    assert order[-2:] == ["footer", "footer_url"], order
    assert order.index("left1") < order.index("right1")


def test_single_column_page_is_left_alone():
    blocks = [
        block("h", [100, 30, 1100, 55], "header"),
        block("a", [100, 200, 1100, 300]),
        block("b", [100, 320, 1100, 420]),
        block("c", [100, 440, 1100, 540]),
        block("f", [100, 1500, 1100, 1525], "footer"),
    ]
    layout = detect_layout(blocks, PAGE_W, PAGE_H)
    assert layout.columns == 1 and layout.confidence == "PASS"
    assert order_blocks(blocks, layout, PAGE_W, PAGE_H) == ["h", "a", "b", "c", "f"]


def test_side_by_side_blocks_in_a_band_read_left_to_right():
    blocks = [
        block("a", [100, 200, 400, 300]),
        block("b", [420, 205, 700, 300]),
        block("c", [100, 400, 700, 500]),
        block("d", [100, 600, 700, 700]),
        block("e", [100, 800, 700, 900]),
    ]
    layout = detect_layout(blocks, PAGE_W, PAGE_H)
    assert order_blocks(blocks, layout, PAGE_W, PAGE_H) == ["a", "b", "c", "d", "e"]


def test_model_disagreement_downgrades_confidence():
    blocks = two_column_page()
    agreeing = build_page_order(blocks, PAGE_W, PAGE_H, model_orders={"mineru": [b["block_id"] for b in blocks]})
    assert agreeing["layout"]["confidence"] == "PASS"
    assert agreeing["layout"]["model_agreement"]["mineru"] >= 0.8
    # A parser that interleaves the columns disagrees with the measured structure.
    scrambled = build_page_order(blocks, PAGE_W, PAGE_H, model_orders={"mineru": ["left1", "right1", "left2", "right2", "header", "page", "title", "authors", "footer", "footer_url"]})
    assert scrambled["layout"]["confidence"] == "CHECK"
    assert any(w.startswith("model_order_disagreement") for w in scrambled["layout"]["warnings"])


def test_interleaved_order_is_reported_by_the_audit():
    blocks = two_column_page()
    layout = detect_layout(blocks, PAGE_W, PAGE_H)
    interleaved = ["header", "page", "title", "authors", "left1", "right1", "left2", "right2", "footer", "footer_url"]
    warnings = audit_order(blocks, interleaved, layout, PAGE_W)
    assert any(w.startswith("column_interleave") for w in warnings)
    assert not audit_order(blocks, order_blocks(blocks, layout, PAGE_W, PAGE_H), layout, PAGE_W)


def test_pairwise_concordance():
    assert pairwise_concordance(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert pairwise_concordance(["a", "b", "c"], ["c", "b", "a"]) == 0.0
    assert 0.4 < pairwise_concordance(["a", "b", "c"], ["b", "a", "c"]) < 0.7
