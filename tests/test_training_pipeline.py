from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from sft.document_state import DocState


def load_script(name):
    path = Path(__file__).parents[1] / "components/training/scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_perfect_continuation_prediction_has_consistent_table_and_event_metrics():
    evaluate = load_script("evaluate_documents.py")
    gts = [
        {"page": 1, "target": "<|table|><table><tr><th>H</th></tr><tr><td>A</td></tr></table><|loc|>0<|truncated|>"},
        {"page": 2, "target": "<|continued|><|table|><table><tr><th>H</th></tr><tr><td>B</td></tr></table><|loc|>1"},
    ]
    state = DocState()
    for item in gts:
        state.ingest(item["page"], item["target"])
    metrics = evaluate.document_metrics(state, gts)
    assert metrics["tables_gt"] == metrics["tables_pred"] == 1
    assert metrics["row_keep"] == metrics["ev_p"] == metrics["ev_r"] == 1


def test_dataset_builder_rejects_document_leakage_and_preserves_existing_output(tmp_path):
    module = load_script("build_train_set.py")
    output = tmp_path / "output"
    output.mkdir()
    dataset = output / "wiki2gt_train.jsonl"
    dataset.write_text("existing dataset\n")
    (tmp_path / "split.json").write_text(json.dumps({"train": ["demo/doc"], "test": ["demo/doc"]}))
    with pytest.raises(ValueError, match="overlap"):
        module.build(tmp_path, output, "train")
    assert dataset.read_text() == "existing dataset\n"
    (tmp_path / "split.json").write_text(json.dumps({"train": ["missing/doc"], "test": []}))
    with pytest.raises(FileNotFoundError):
        module.build(tmp_path, output, "train")
    assert dataset.read_text() == "existing dataset\n"
    assert not list(output.glob(".dataset-*.tmp"))
