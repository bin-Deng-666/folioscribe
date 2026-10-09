from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from sft.output_format import blocks_to_dsl, build_prompt, parse_blocks
from sft.projection import project, roundtrip_check, table_html
from sft.document_state import DocState


def test_multiline_dsl_roundtrip_preserves_metadata_and_blank_lines():
    blocks = [
        {"type": "text", "content": "one\n\ntwo", "page": 0, "truncated": True},
        {"type": "table", "content": "<table>\n<tr><td>A</td></tr>\n</table>",
         "page": 1, "bbox": [0, 10, 900, 950], "continued": True, "truncated": True},
    ]
    raw = blocks_to_dsl(blocks)
    parsed = parse_blocks(raw)
    assert blocks_to_dsl(parsed) == raw
    assert parsed[1]["page"] == 1
    assert parsed[1]["continued"] and parsed[1]["truncated"]
    assert "<|loc|>" not in parsed[1]["content"]


def test_three_page_continuation_survives_middle_page_and_closes():
    state = DocState()
    for page, content in enumerate(["甲", "乙", "丙"], 1):
        state.ingest(page, blocks_to_dsl([
            {"type": "header", "content": "header", "page": page - 1},
            {"type": "text", "content": content, "page": page - 1,
             "continued": page > 1, "truncated": page < 3},
        ]))
        assert (state.open_block is not None) == (page < 3)
    assert [b["content"] for b in state.blocks if b["type"] == "text"] == ["甲乙丙"]
    assert state.context_for(4)["prev_page_tail"] == "丙"


@pytest.mark.parametrize("interruption", ["gap", "empty", "body", "type"])
def test_continuations_require_adjacent_pages_and_matching_first_body(interruption):
    state = DocState()
    state.ingest(1, "<|text|>first<|loc|>0<|truncated|>")
    page, raw = 2, "<|continued|><|text|>second<|loc|>1"
    if interruption == "gap":
        page = 3
    elif interruption == "empty":
        state.ingest(2, "")
        page = 3
    elif interruption == "body":
        raw = "<|text|>intervening<|loc|>1\n" + raw
    else:
        raw = "<|continued|><|table|><table><tr><td>second</td></tr></table><|loc|>1"
    record = state.ingest(page, raw)
    assert not any(action.startswith("merge_") for action in record["actions"])
    assert state.blocks[0]["content"] == "first"
    assert "second" in state.assemble_markdown()


def test_projection_uses_previous_state_without_answer_leakage(tmp_path):
    path = Path(__file__).parents[1] / "examples" / "make_demo.py"
    spec = importlib.util.spec_from_file_location("make_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    doc = module.create_demo(tmp_path)
    samples = project(doc, "demo", "three_pages", pages_limit=40)
    assert len(samples) == 3  # Clamp to real document length.
    assert roundtrip_check(samples) == []
    assert samples[0]["prev_tail"] == ""
    assert samples[0]["title_tree"] == {"open_tree": [], "closed_recent": []}
    assert "Demo document" not in samples[0]["prompt"]
    assert any(b["type"] == "image" for b in parse_blocks(samples[0]["target"]))
    state = DocState()
    for item in samples:
        ctx = state.context_for(item["page"])
        assert item["prompt"] == build_prompt(
            item["page"] - 1, item["total_pages"], ctx["title_tree"], ctx["prev_page_tail"]
        )
        state.ingest(item["page"], item["target"])
    tables = [b for b in state.blocks if b["type"] == "table"]
    assert len(tables) == 1
    for word in ("Item", "Alpha", "Beta", "Gamma"):
        assert tables[0]["content"].count(word) == 1
    assert state.open_block is None


def test_table_text_is_escaped_and_whole_header_row_is_marked():
    struct = {"cells": [
        {"row": 0, "col": 0, "text": "a < b", "boxes": [{"page_idx": 0}]},
        {"row": 0, "col": 1, "text": "x & y", "boxes": [{"page_idx": 0}]},
    ]}
    result, _, _ = table_html(struct, 0, None)
    assert "<th>a &lt; b</th><th>x &amp; y</th>" in result
