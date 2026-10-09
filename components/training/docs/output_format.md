# 训练与推理输出格式

权威实现：`sft/output_format.py`。当前格式 `contract-v3.1`，prompt `prompt-v7`，图像规范 `image-v1`。

## 块类型

| 类型 | DSL | 内容 |
| --- | --- | --- |
| 页眉 | `<\|header\|>` | 文本 |
| 页脚 | `<\|footer\|>` | 文本，包含页码 |
| 标题 | `<\|title\|><\|level\|>N<\|text\|>` | 标题文本，层级 1–6 |
| 正文 | `<\|text\|>` | Markdown，包含列表、公式和 caption |
| 表格 | `<\|table\|>` | HTML table，可多行 |
| 图片 | `<\|image\|>` | 无文本负载，使用 bbox |

`<|loc|>page[,x1,y1,x2,y2]` 位于内容后。page 为 0-based；训练样本元数据和页图文件名为 1-based。table/image 的目标格式要求几何坐标，其余块通常带页号。

文本/表格跨页时，前块末尾 `<|truncated|>`，下一页块开头 `<|continued|>`。例如：

```text
<|table|><table>
<tr><th>Item</th></tr>
<tr><td>Alpha</td></tr>
</table><|loc|>0,10,20,950,990<|truncated|>
```

解析器先按类型标记聚合块，再解析末尾元数据，因此多行表格中的定位和续接标记不会变成单元格内容。未知/无效输出的严格 schema 拒绝仍未全面实现，不能把宽容解析成功当作模型正确。

## 输入状态与执行顺序

1. 从前一页结束后的 `DocState` 获取 `title_tree` 和 `prev_page_tail`。
2. 用 `build_prompt(page_id, page_count, ...)` 生成当前页输入。
3. 训练侧构造当前页 GT DSL，推理侧生成当前页模型 DSL。
4. 将 DSL 摄入同一 `DocState`，供下一页使用。

`title_tree` 包含 `open_tree`（最多 8 条）和 `closed_recent`（最多 3 条）。`prev_page_tail` 通常保留前页截断块或最后块的尾部，预算 400 字/6 行；首页为空。它不包含当前页答案。

当前 projector 的标题树推导保持原实现语义：空树从 1 开始，较深 raw level 最多提升两级，较浅层级查找已有祖先。不能把它描述为已实现完整语义层级预测器或严格逐级 clamp。

## 跨页合并

`document_state.py` 只接受相邻页、同类型、当前页第一个非家具块的显式 continued/truncated 配对。页眉/页脚不打断这种边界配对；缺页、空页、前置正文或类型不匹配保留独立块。

三页以上链条用当前片段的 truncated 状态更新原合并块；最后片段闭合后清空 open state。Markdown 输出来自已合并块。当前表格处理会去重相同首行表头，复杂 rowspan、列数冲突与多个并行跨页链仍需扩展测试和协议。

## 版本要求

更改 prompt、截断策略或目标语义后重建数据与评测集，并在 manifest 中记录版本。不同协议或指标版本的实验结果不能直接横向比较。
