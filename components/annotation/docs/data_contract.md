# 数据约定

## 模型和环境配置

运行时从环境变量读取通用配置：

```text
OCR_AGENT_API_KEY
OCR_AGENT_BASE_URL
OCR_AGENT_MODEL
OCR_AGENT_VISION
OCR_AGENT_REASONING_EFFORT
OCR_AGENT_MAX_OUTPUT_TOKENS
OCR_AGENT_TIMEOUT_SECONDS
```

当前默认运行配置为：

```text
model: deepseek-v4-flash
base_url: http://127.0.0.1:8000/v1
vision: false
reasoning_effort: minimal
max_output_tokens: 16384
timeout_seconds: 60
```

`DS_V4_FLASH_*` 仅作为旧环境兼容 fallback，不应作为新运行配置。模型可替换，但必须先验证 `/models` 和 `/chat/completions` 能力，再进行 pipeline 回归。


示例见 [`examples/page_evidence.json`](../../../examples/page_evidence.json)。

### 坐标

证据包中的 `bbox_px` 一律是原始整页 PNG 的像素坐标：

```text
[x1, y1, x2, y2]
```

要求：

```text
0 <= x1 < x2 <= page_width
0 <= y1 < y2 <= page_height
```

局部裁图只能用于查看。局部工具返回的 bbox 必须先映射回原始整页坐标，才能加入证据包。

### required_coverage

`required_coverage=true` 表示该证据对应页面上必须进入最终结果的可见内容。首批主要用于可靠的 PDF 文字行和 PDF 图片对象。

检查结果要求每个 required evidence 恰好被一个最终块引用：

```text
0 次：遗漏
1 次：正确归属
2 次或更多：重复归属
```

损坏、隐藏或重复的 PDF 文字不能设为 required，必须先由 reader 标记质量。

### source_refs 和 evidence_refs

- `source_refs`：最终块实际包含哪些页面证据；
- `evidence_refs`：为什么执行某个修改操作。

两者作用不同。修改标题类型可以引用视觉裁图作为 `evidence_refs`，但裁图本身不一定成为 block 的 `source_refs`。

## 最终类型合同（v4）

最终 `block.type` 只允许 13 个值：

```text
text, title, table, table_caption, image, image_caption, chart,
seal, formula, header, footer, page_number, footnote
```

`list` 和 `toc` 不再是最终类型。编号/项目符号条目和目录条目都使用 `text`，原始可见标记保留在 `text` 字段。上游 evidence 仍可保留 `source_type=list` 或 `source_type=list_item` 以追溯模型来源，但消费者不得把它们提升为最终 `block.type`。

## Title type 和 level 合同

`title.type` 与 `title.level` 由同一个受约束 semantic task 联合判断。模型可见目标前后正文、当前页其它 block、可纠正的前文标题路径、邻近标题候选、上一页尾部、下一页头部，以及来源投票；模型不能修改 text、bbox 或创建 block。

整本结束后可以产生三类文档级复核 task：

```text
title_type_level_consistency：同一局部序列或重复栏目存在 type/level 冲突
parallel_layout_title_consistency：同父路径、同几何样式的平行栏目存在 type/level 差异
deep_title_precision：首轮为 L4 的 title 需要再次确认其确实命名并统领后续内容
missed_title_recall：首轮最终为 text，但存在独立结构证据支持候选回看
```

标题层级上限为 L4。L1 是文档标题，L2/L3 表示章节和小节，L4 是最深接受层。不会把更深的原始编号层级机械压成 L4；叶子要求项、属性项、列表项和完整操作指令应保留为 text。召回可以适度覆盖，但最终 title 必须经过语义判断；文档级复核允许将过度标注的 title 降回 text。每个 task 最多 4 项，每轮文档级复核最多覆盖 32 个唯一 block，并按“显式序列冲突、平行栏目冲突、局部层级碰撞、L4 精度、漏召回”排序。规则只负责检索冲突和校验输出，不强制把平行候选统一为 title 或同一 level。字面 Markdown `#` 标记不参与 title 判断或定级；强调包装仅可用于识别序列外形。无法确认时字段保持 `CHECK`。文本型 unknown 可以用 `text + type CHECK` 作为最终 ontology 兜底，但不能因此获得 PASS。

这是从 v3 到 v4 的 breaking change。v3 revision 必须重建或迁移后才能与 v4 数据合并。

## Word 原生表格证据

已绑定的 DOCX 可提供逻辑表的行列、`gridSpan`、`vMerge`、单元格文本和完整 HTML。Word 不提供发布 PDF 的最终分页和 bbox，因此跨页表按两层表示：

```text
Word logical table -> 完整结构
PDF table fragment -> 实际 page + bbox + PDF text span/drawing
```

只有 DOCX-PDF 文档级绑定通过，且 Word 行到 PDF 页片段的映射完整、无重复、无遗漏时，table HTML 才能自动 PASS。当前实现使用连续 Word 行匹配、PDF required text、PDF ruled region 和文档级 row coverage gate；不依赖文档名、页码或固定表名。

## Table HTML 质量合同

表格 HTML 不能仅因模型返回了合法 `<table>` 就标记为 PASS。当前自动确认分为以下证据路径：

```text
Word logical table + DOCX-PDF 强绑定 + 连续页行映射
PDF ruled cell rectangles 或横竖线段恢复的完整 cell matrix
MinerU/Paddle 双模型 cell topology 共识 + PDF 文本守恒
```

PDF 网格路径必须同时满足：

- 外框和内部边界可由 PDF drawing 证明；
- HTML 的 row/col/rowspan/colspan 与 PDF cell matrix 完全一致；
- 每条 PDF text line 唯一归属于一个 cell；
- PDF cell 文本与 HTML cell 文本逐格守恒；
- 无法确定 cell、拓扑不一致、跨页缺边或文本冲突时保持 `CHECK`。

整表字符相似度、单模型 HTML、物理 `<td>` 数量相同都不足以单独产生 PASS。

## 跨页关系（字段合同）

完整设计见 `docs/cross_page_contract.md`。两个块级字段都是可选的、带 `status`/`method`/`evidence_refs` 的受控结构。

### 文本跨页流 `cross_page_text`

```text
cross_page_text.continues_block_id     承接的上页尾块；null 表示已判定“不接”
cross_page_text.continued_by_block_id  承接本块的下页首块
cross_page_text.status                 PASS（已判定，含“不接”）| CHECK（未判定/失败）
cross_page_text.method                 判定方法
cross_page_text.confidence_basis       gate 判据：edge_geometry / same_column / tail_open / head_not_new_section
cross_page_text.evidence_refs          双方 PDF 证据
```

- 规则 gate 未通过时**不调用模型、不写字段**；通过后由模型在 `continuous|separate|uncertain` 中裁决。
- 只有 `continuous` 写 `continues_block_id`；`separate` 写 `null` 且 `status=PASS`；`uncertain` 写 `status=CHECK` 并落 `review_items`。
- 反向边由文档级 finalize 回填（`method=document_cross_page_text_flow_reverse`）。

### 表格跨页 `cross_page_table`

```text
document_table_identity   跨页表组身份
part_index / part_count   组内序号与总段数（文档级回填）
continues_block_id / continued_by_block_id
truncated_at_page_end     本页底部被页边裁切（表底到内容底且无闭合横线）
truncated_at_page_start   本页顶部是上页被裁切的续表
repeated_header           续表重复了表头行
has_closing_rule          表底存在闭合横线
status / method / evidence_refs
```

- continuation 以**规则为权威**：本页首表在页面顶部 + 与上页表列边界结构一致（`pdf_drawing` 竖线或文本左边界聚类，容差 ≤ 页宽 × 1%）。
- 模型的 `document_relation` 只作交叉验证：一致 → PASS；冲突 → `CHECK`（`cross_page_table_rule_model_conflict`）。
- 没有任何栅格/结构证据时不写字段（不产生假结论）。

### 文档级 `document_flow`

写在 `document_result.json` / `documents/<sample_id>.json`：

```text
document_flow.text_edges[]
  from {page, block_id} / to {page, block_id} / status / method / evidence_refs
document_flow.table_groups[]
  document_table_identity / part_count / parts[{page, block_id, truncated_at_page_end, truncated_at_page_start, repeated_header}]
document_flow.conflicts[]
```

finalize 只做回填与一致性校验，**不推断缺失边**；跨页不连续或指向非相邻页的链接降级为 `CHECK`。

## 表内图片

表内图片使用 table 内嵌证据表达：

```text
embedded_images[].image_ref
embedded_images[].row / col / rowspan / colspan
HTML data-image-ref
```

只有 PDF image object、PDF cell geometry 和 HTML cell marker 三者可以唯一绑定时才自动 PASS；不再生成与 table 重叠的顶层 image block。


每次修改生成一个新 revision，不覆盖输入证据包。revision 包含：

```text
schema_version
sample_id
page
revision
blocks
document_relations
cross_page_relations
review_items
operation_log
```

OCR-Agent 自动结果只有 `PASS` 和 `CHECK`。只有 `PASS` 可直接使用；`CHECK` 由人工检查后改为 `PASS` 或 `FAIL`。

## 对外契约 GT（A1/A2/A3 + FullDocumentGTBundle）

除本目录定义的 revision 之外，每次文档运行还会在 `contract_gt/<sample_id>/` 派生一份 DocFusion
公共契约 GT：`full_document_gt_bundle.json`（主交付）、`canonical_doc_graph.json`、
`page_geometry_manifest.json`、`gt_truth_document.json` 通过注册 schema 校验；A1/A2/A3 以同形状
投影写在 `a_facts/` 并声明 `fact_schema_version=ocr-agent.v1`。字段对应、校验口径、跨页续表语义与
合成线的覆盖现状见 [`contract_gt.md`](contract_gt.md)。
