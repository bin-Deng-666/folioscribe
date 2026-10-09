# 架构与数据边界

FolioScribe 包含两个有独立数据协议的组件，以及共同的安装、测试与文档入口。

## 标注链路

`parser_assets.py` / `audit_evidence_assets.py` 在构建前检查实际 PDF 页数、逐页文件名、图片尺寸、bbox 和可用 manifest 的 PDF 哈希。测试用合成两页 PDF 与三路候选文件调用真正的 `build_document_evidence()`，不只验证 JSON 外形。

`build_document.py` → `evidence_builder.build_document_evidence()` 从 PDF、MinerU、Paddle、Ovis 与可选 Word/AnyDoc 输入构建页证据。`inventory_rules.py` 和 `reading_order.py` 收敛候选库存与顺序；`review_tasks.py`、`task_provider.py`、`decision_applier.py` 把模型限制在有证据的语义决策内；`validators.py` 产生字段及页面 PASS/CHECK。

`cross_page.py` 维护相邻页文本/表格关系，`title_consistency.py` 执行文档标题一致性复核。这里保留页面事实与链接，不把多页 bbox 直接合成一个页内框。

`viewer/server.py` 和静态前端负责浏览、人工纠正、跨页编辑、任务分配与复核记录。Viewer 的 HTTP 服务和静态资源一起进入 wheel，`folioscribe-viewer` 在安装后的环境中也可启动。

`contract_gt.py` 投影事实图、几何、A1/A2/A3 与完整 bundle。标准 DocFusion JSON Schema 是可选的外部依赖；导出与通过外部契约校验是两个不同状态。

## 训练链路

- `sft/output_format.py`：六类型 DSL、位置和续接标记、prompt 模板及预算。
- `sft/document_state.py`：摄入逐页 DSL、更新标题状态、续接文本/表格、生成下一页上下文。
- `sft/projection.py`：读取 wiki2gt 数据，按阅读顺序构造当页目标；先读取状态生成 prompt，再摄入目标。
- `sft/markdown.py`：文本归一化、坐标投影和文本拼接原语。
- `scripts/build_train_set.py`：验证 split、DSL 回读与图片存在性，输出 JSONL 和 manifest。
- `scripts/evaluate_documents.py`：调用远程视觉模型，并以同一 `DocState` 更新后续页面上下文。

当前页训练 prompt 只能包含前序页事实。合成三页表格测试会逐字比较投影 prompt 与推理状态机生成的 prompt，并检查最终表格内容守恒。

节点和表格单元格在投影开始时按页建立索引，消除了每页遍历全部文档节点、每张表扫描全部单元格的重复工作。标题历史状态的维护仍有优化空间。

## 两种 GT 不是同一个文件格式

| 维度 | 标注组件导出 | 当前训练投影输入 |
| --- | --- | --- |
| bundle | `full_document_gt_bundle.json` | `gt/gt_bundle.json` |
| 几何 | `page_geometry_manifest.json` | `gt/page_geometry.json` |
| raw | `gt_raw.json` 当前为图的副本 | `gt/gt_raw.json` 必须有 `elements` |
| 类型 | 13 类，含 caption/formula/chart 等 | 6 类 DSL |
| 标题层级 | 自动结果 1–4，Viewer 人工允许 1–6 | DSL 1–6 |
| 跨页 | 图关系及页面块链接 | 块级 continued/truncated |
| 人工纠正 | `annotations/` 补丁及审核记录 | 尚未直接消费 |

因此不能只重命名导出的 JSON 文件就拿来训练。下一步适配器必须明确：选用哪位标注者、哪份修订、是否接受 CHECK/FAIL、类型归并、跨页关系与坐标单位、训练/测试文档隔离，并附上 provenance。

## 工程不变量

- 当前页 prompt 只读取前序页状态，训练和推理共用文档状态机。
- DSL 先聚合多行块，再解析末尾位置与续接标记。
- 续接要求相邻页与相同类型；三页链、空页与缺页有独立回归覆盖。
- 图片节点不依赖非空文本；表格纯文本单元格需要 HTML 转义。
- 文档评测以相同合并口径比较 GT 与预测，事件从合并前的逐页块流统计。当前仍为页/类型级计数，不是关系级精度。
- Viewer 在保存成功后导航，保存中产生的新编辑也必须落盘。自动保存保留撤销历史，任务路由参数独立于缓存策略。

## 当前限制

测试使用合成输入覆盖协议、几何和持久化行为，不能替代真实语料精度评测或 GPU 训练基准。

表格合并仍主要基于 HTML 行与重复表头处理，复杂跨页 rowspan、异构列结构、无效 DSL 的严格拒绝机制需要进一步验证。Viewer 多进程/多标签并发的修订冲突检查尚未实现，原子落盘不等同于冲突解决。
