from __future__ import annotations

import runpy
from pathlib import Path

import pytest


def test_paddle_adapter_preserves_pipeline_geometry_and_rejects_plain_text():
    script = Path(__file__).parents[1] / "components/annotation/scripts/parse_pages.py"
    normalize = runpy.run_path(str(script))["normalize_paddle_result"]
    result = {"parsing_res_list": [{"block_bbox": [10, 20, 100, 200], "block_label": "text", "block_content": "hello"}]}
    assert normalize({"res": result}) == result
    assert normalize(result) == result
    assert normalize({"res": result}, (600, 800))["width"] == 600
    with pytest.raises(ValueError, match="differs"):
        normalize({**result, "width": 123}, (600, 800))
    with pytest.raises(ValueError, match="parsing_res_list"):
        normalize({"choices": [{"message": {"content": "plain OCR text"}}]})
    with pytest.raises(ValueError, match="geometry"):
        normalize({"parsing_res_list": [{"block_label": "text"}]})
