# Contract GT 投影

`ocr_agent.contract_gt` 将文档运行结果投影为文档图、几何和 A1/A2/A3 事实。标准 DocFusion schemas 是可选外部输入，本仓库不包含它们。

## 数据对应

| 目标 | 来源 |
| --- | --- |
| A1 物理与文本事实 | 页码、块 bbox、阅读顺序、文本、PDF 证据及字形信息 |
| A2 语义与关系 | 13 类块类型、标题层级、caption parent、跨页链接 |
| A3 文档树 | 标题层级与内容归属 |
| 跨页表格 | `document_flow.table_groups` 中的逐页片段 |

只有存在已验证表格栅格时才生成行/单元格几何事实。无法测量的属性保持缺省或未验证状态；生成文件不等同于通过外部 schema 校验。

## 运行

```bash
python components/annotation/scripts/export_contract_gt.py --document-root data/annotation
python components/annotation/scripts/export_contract_gt.py \
  --document-root data/annotation --schema-root /path/to/generated/jsonschema
```

也可设置 `DOCPARSE_SCHEMA_ROOT`。缺少 schema 目录或文件、校验失败时，报告的 `validated` 为 false。

输出目录为 `contract_gt/<sample>/`：

```text
canonical_doc_graph.json
page_geometry_manifest.json
full_document_gt_bundle.json
gt_truth_document.json
gt_raw.json
export_report.json
a_facts/
  annotation_fact_a1.json
  annotation_fact_a2.json
  annotation_fact_a3.json
```

A 文件保留 `fact_schema_version=ocr-agent.v1`，区分 PDF 证据与模型判断的生产方式，不冒充由确定性渲染生成的真值。完整 bundle 是主要交换产物。

## 约定

- 实体关联原始证据，通过 `fragment:<evidence_id>` 标识。
- 阅读顺序为 0-based；bbox 使用声明的坐标空间。
- 结构对象的 SHA-256 基于排序键、紧凑 UTF-8 JSON；原文件哈希基于文件字节。
- 文档根节点不对应页面实体，不计为未映射内容。
- 续表边连接不同页面表节点；只有行数可测量时才写入 completeness，重复表头只计一次。
- 导出主要消费自动运行结果；人工修订到标准 GT / SFT 的统一适配仍需实现。

当前训练投影要求 wiki2gt 的 `gt_raw.json.elements`，而这里的 `gt_raw.json` 是文档图副本。不能只改文件名后直接训练，详见[架构](../../../docs/architecture.md)。
