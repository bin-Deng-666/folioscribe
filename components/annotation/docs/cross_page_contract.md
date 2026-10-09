# 跨页标注合同

跨页关系采用“规则选候选、模型受约束判定、文档级收敛”的流程。每页保留自己的文本、HTML 与 bbox；关联只通过块 ID、表身份和证据引用表达。

权威实现：`src/ocr_agent/cross_page.py`、`review_tasks.py` 和 `task_agent.py`。

## 文本续接

可选块字段 `cross_page_text`：

```json
{
  "continues_block_id": "candidate.block.p1.0003",
  "continued_by_block_id": null,
  "status": "PASS",
  "method": "model_cross_page_text_flow",
  "evidence_refs": ["pdf.line.p1.0003", "pdf.line.p2.0000"]
}
```

`continues_block_id` 指向上页被分页打断的块，`continued_by_block_id` 指向下页承接块。中间片段可以同时包含两个方向。已判定“不接”可以使用 null；未定或冲突为 CHECK。

规则检查页序连续、上页处理完整、页底/页首位置、同栏重叠，以及正文是否具有句终或新章节标记。候选通过后，模型只能在 `continuous`、`separate`、`uncertain` 中判断。不确定和模型失败不能自动升级为 PASS。

## 表格续接

可选块字段 `cross_page_table`：

```json
{
  "document_table_identity": "demo.table.0001",
  "part_index": 2,
  "part_count": 3,
  "continues_block_id": "candidate.block.p1.0004",
  "continued_by_block_id": "candidate.block.p3.0000",
  "truncated_at_page_start": true,
  "truncated_at_page_end": true,
  "repeated_header": true,
  "status": "PASS",
  "method": "pdf_ruled_grid_cross_page_continuation",
  "evidence_refs": []
}
```

- `document_table_identity` 关联同一逻辑表，`part_index/part_count` 在文档级回填。
- `truncated_at_page_start/end` 描述页面边界上的截断，不等于表内任何位置的缺失。
- `repeated_header` 记录续页表头是否重复；原页面 HTML 保留。
- PDF 列边界、页面几何与 Word 表身份/连续行范围提供证据。可用的 Word 行证据按表受理，不因全文匹配较弱就整体丢弃。
- Word 与几何证据冲突时保留 CHECK；缺少可校验依据时不推断新链接。

## 文档级收敛与人工编辑

整本文档完成后检查反向链接、页序、块存在性与表组一致性，产出 `document_flow`。这个阶段回填已建立关系，不凭空补链。失败页必须出现在文档结果中，避免把部分成功计成全文完成。

Viewer 可人工创建/撤回相邻页链接，并同步反向字段。人工修改保存在 annotation 补丁中，自动链路结果保留供比较。

训练侧使用另一种表示：块头 `<|continued|>` 与块尾 `<|truncated|>`。将标注关系映射到训练 DSL 需要专门适配器，不能直接把字段名替换为标记。

## 回归边界

测试覆盖文本边界 gate、表格几何/Word 冲突、三页续接、缺页和链路回填。复杂跨页 rowspan、多个同时打开的续接流仍需要专项数据与进一步验证。
