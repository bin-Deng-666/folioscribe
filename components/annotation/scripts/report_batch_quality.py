#!/usr/bin/env python3
"""Batch quality + coverage report for an OCR-Agent annotation batch.

Reads only what the batch already contains (registry, page results, evidence, contract GT exports)
and writes ``quality_report.json`` + ``QUALITY_REPORT.md`` next to the batch:

* documents / pages / per-cell coverage (scene card x layout group), with the cells that cannot be
  covered and why
* validator errors by code (these must be zero: they are pipeline invariants, not review hints)
* PASS / CHECK distribution and the field-review methods that produced it (how much of the GT is
  PDF-verified, how much rests on model text only)
* text-layer audit (glyph-noise documents) and exclusions
* Contract GT export coverage and registered-schema validation
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def page_results(document_root: Path, sample_id: str) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((document_root / "runs" / sample_id).glob("page_*/result.json")):
        try:
            rows.append(read_json(path))
        except Exception:
            continue
    return rows


def audit_records(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[str(record.get("sample_id"))] = record
    return out


def cell_of(document: dict[str, Any]) -> str:
    return str(document.get("cell_key") or "")


def build_report(document_root: Path, registry_path: Path, selection_path: Path | None, audit_path: Path | None) -> tuple[dict[str, Any], str]:
    registry = read_json(registry_path)
    documents = registry["documents"]
    excluded = registry.get("excluded", [])
    audit = audit_records(audit_path)

    statuses = Counter()
    errors = Counter()
    order_conf = Counter()
    order_columns = Counter()
    order_warnings = Counter()
    order_agreement: list[float] = []
    error_docs: dict[str, int] = {}
    unresolved = 0
    methods = Counter()
    cross_page = Counter()
    classes = Counter()
    pages_total = 0

    for document in documents:
        sample = document["sample_id"]
        for result in page_results(document_root, sample):
            pages_total += 1
            validation = result.get("validation") or {}
            statuses[str(validation.get("status") or result.get("status") or "?")] += 1
            unresolved += int(validation.get("unresolved_field_count") or 0)
            for error in validation.get("errors") or []:
                errors[str(error.get("code"))] += 1
                error_docs[sample] = error_docs.get(sample, 0) + 1
            evidence = read_json(document_root / "evidence" / sample / f"page_{int(result['page']):04d}.json") or {}
            order = evidence.get("reading_order") if isinstance(evidence.get("reading_order"), dict) else result.get("reading_order")
            if isinstance(order, dict):
                order_conf[str(order.get("confidence"))] += 1
                order_columns[int(order.get("columns") or 1)] += 1
                for warning in order.get("warnings") or []:
                    order_warnings[str(warning).split(":")[0]] += 1
                for value in (order.get("model_agreement") or {}).values():
                    if isinstance(value, (int, float)):
                        order_agreement.append(float(value))
            revision = result.get("revision") or {}
            for fields in (revision.get("field_reviews") or {}).get("blocks", {}).values():
                if isinstance(fields, dict):
                    for field in fields.values():
                        if isinstance(field, dict) and field.get("method"):
                            methods[str(field["method"])] += 1
            for relation in revision.get("cross_page_relations") or []:
                cross_page[str(relation.get("relation") or relation.get("type") or "?")] += 1
            for block in revision.get("blocks") or []:
                classes[str(block.get("type"))] += 1

    cells: dict[str, dict[str, Any]] = defaultdict(lambda: {"documents": 0, "pages": 0})
    for document in documents:
        cell = cells[cell_of(document)]
        cell["documents"] += 1
        cell["pages"] += int(document.get("pages") or 0)

    coverage: dict[str, Any] = {"selected_cells": len(cells)}
    if selection_path and selection_path.is_file():
        selection = read_json(selection_path)
        policy_cells = selection.get("cells") or []
        coverable = [cell for cell in policy_cells if cell.get("eligible")]
        uncovered = []
        for cell in coverable:
            key = f"{cell.get('scene_card')}#{cell.get('layout_group')}"
            if key in cells:
                continue
            uncovered.append({
                "cell_key": key,
                "eligible_candidates": int(cell.get("eligible") or 0),
                "top_exclusion_reasons": cell.get("top_exclusion_reasons"),
            })
        coverage.update({
            "coverable_cells": len(coverable),
            "covered_cells": len(coverable) - len(uncovered),
            "uncovered_cells": uncovered,
            "policy_cells": len(policy_cells),
        })

    contract_dirs = sorted(path.name for path in (document_root / "contract_gt").iterdir() if path.is_dir()) if (document_root / "contract_gt").is_dir() else []
    contract_ok = 0
    contract_failed = 0
    for sample in contract_dirs:
        report_path = document_root / "contract_gt" / sample / "export_report.json"
        if report_path.is_file():
            report = read_json(report_path)
            if (report.get("validation") or {}).get("validated") is False:
                contract_failed += 1
            else:
                contract_ok += 1
        else:
            contract_failed += 1

    glyph_noise = {sample: record for sample, record in audit.items() if record.get("verdict") == "glyph_noise"}
    report = {
        "schema_version": "ocr-agent.batch-quality-report.v1",
        "document_root": str(document_root),
        "documents": len(documents),
        "pages": pages_total,
        "registered_pages": sum(int(document.get("pages") or 0) for document in documents),
        "validation": {
            "status": dict(statuses),
            "validator_errors": dict(errors),
            "validator_error_pages": len(error_docs),
            "validator_error_documents": sorted(error_docs),
            "unresolved_fields": unresolved,
        },
        "field_review_methods": dict(methods.most_common()),
        "block_types": dict(classes.most_common()),
        "cross_page_relations": dict(cross_page),
        "reading_order": {
            "pages_with_evidence": sum(order_conf.values()),
            "columns": {str(key): value for key, value in sorted(order_columns.items())},
            "confidence": dict(order_conf),
            "warnings": dict(order_warnings.most_common()),
            "mean_model_agreement": round(sum(order_agreement) / len(order_agreement), 3) if order_agreement else None,
        },
        "coverage": coverage,
        "text_layer_audit": {
            "documents_audited": len(audit),
            "glyph_noise_documents": sorted(glyph_noise),
            "glyph_noise_with_image_canvas": sorted(
                sample for sample, record in glyph_noise.items()
                if float(record.get("image_coverage") or 0) >= 0.6
            ),
        },
        "excluded_documents": excluded,
        "contract_gt": {
            "exported": len(contract_dirs),
            "schema_valid": contract_ok,
            "schema_invalid_or_missing_report": contract_failed,
            "missing_export": sorted(set(document["sample_id"] for document in documents) - set(contract_dirs)),
        },
    }

    lines = [
        "# 批次质量报告",
        "",
        f"- 批次根：`{document_root}`",
        f"- 文档 / 页：**{report['documents']}** / **{report['pages']}**（登记页数 {report['registered_pages']}）",
        f"- 校验状态：{', '.join(f'{key} {value}' for key, value in sorted(statuses.items()))}",
        f"- **validator errors：{sum(errors.values())}**（涉及 {len(error_docs)} 篇）",
        f"- 未决字段：{unresolved}",
        "",
        "## 单元覆盖",
        "",
    ]
    if selection_path:
        lines += [
            f"- 可覆盖单元：**{coverage['covered_cells']} / {coverage['coverable_cells']}**"
            f"（策略单元总数 {coverage['policy_cells']}）",
            f"- 已入批单元：{coverage['selected_cells']}",
        ]
        for cell in coverage.get("uncovered_cells", []):
            lines.append(f"- 未覆盖：`{cell['cell_key']}`（合格候选 {cell['eligible_candidates']}，主因 {cell['top_exclusion_reasons']}）")
    lines += ["", "## 文本层审计", ""]
    lines.append(f"- 已审计 {report['text_layer_audit']['documents_audited']} 篇；glyph_noise {len(glyph_noise)} 篇")
    lines.append(f"- 其中整页图 + 乱码文字层（伪扫描）：{report['text_layer_audit']['glyph_noise_with_image_canvas']}")
    if excluded:
        lines += ["", "## 剔除文档", ""]
        for item in excluded:
            lines.append(f"- `{item.get('sample_id')}`（{item.get('cell_key')}）：{item.get('reason')} — {item.get('detail', '')[:160]}")
    lines += ["", "## 阅读顺序", ""]
    order_report = report["reading_order"]
    if order_report["pages_with_evidence"]:
        lines.append(f"- 带顺序证据的页：{order_report['pages_with_evidence']}"
                     f"（栏数分布 {order_report['columns']}，置信度 {order_report['confidence']}）")
        if order_report["mean_model_agreement"] is not None:
            lines.append(f"- 与模型自有顺序的平均一致率：{order_report['mean_model_agreement']}")
        if order_report["warnings"]:
            lines.append(f"- 告警：{order_report['warnings']}")
    else:
        lines.append("- 本批次尚未写入阅读顺序证据（旧的 runs 还没有重跑）")
    lines += [
        "",
        "## Contract GT",
        "",
        f"- 导出目录 {report['contract_gt']['exported']}，schema 通过 {report['contract_gt']['schema_valid']}，未通过/缺报告 {report['contract_gt']['schema_invalid_or_missing_report']}",
        "",
        "## 字段判定方法（前 12）",
        "",
    ]
    for method, count in list(methods.most_common(12)):
        lines.append(f"- `{method}`: {count}")

    return report, "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, help="defaults to <document-root>/v10_documents.json or batch_documents.json")
    parser.add_argument("--selection", type=Path, help="optional selection metadata")
    parser.add_argument("--text-audit", type=Path, help="optional PDF text-layer audit")
    args = parser.parse_args()

    registry = args.registry
    if registry is None:
        for candidate in ("v10_documents.json", "batch_documents.json"):
            path = args.document_root / candidate
            if path.is_file():
                registry = path
                break
    if registry is None:
        raise SystemExit("no registry found")

    report, markdown = build_report(args.document_root, registry, args.selection, args.text_audit)
    (args.document_root / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (args.document_root / "QUALITY_REPORT.md").write_text(markdown, encoding="utf-8")
    print(json.dumps({
        "documents": report["documents"],
        "pages": report["pages"],
        "validator_errors": sum(report["validation"]["validator_errors"].values()),
        "coverage": report["coverage"],
        "contract_gt": report["contract_gt"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
