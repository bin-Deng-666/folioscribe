from __future__ import annotations

import json

import pytest

from ocr_agent.contract_gt import export_contract_gt, schema_root_default, validate_contracts
from test_contract_gt import _document_root


def test_missing_schema_never_reports_validated(tmp_path):
    result = validate_contracts({"Example": ("example.json", {})}, tmp_path)
    assert result["validated"] is False
    assert result["files"]["example.json"]["status"] == "schema_missing"


def test_schema_validation_accepts_valid_and_rejects_invalid(tmp_path):
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    (tmp_path / "Example.schema.json").write_text(json.dumps(schema))
    assert validate_contracts({"Example": ("example.json", {"value": 1})}, tmp_path)["validated"]
    assert not validate_contracts({"Example": ("example.json", {"value": "bad"})}, tmp_path)["validated"]


@pytest.mark.integration
def test_export_against_external_registered_schemas(tmp_path):
    schemas = schema_root_default()
    if schemas is None:
        pytest.skip("Set DOCPARSE_SCHEMA_ROOT to validate against external DocFusion schemas")
    report = export_contract_gt(_document_root(tmp_path), "rd_test", schema_root=schemas)
    assert report["validation"]["validated"] is True, report["validation"]
