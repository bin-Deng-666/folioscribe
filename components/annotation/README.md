# 标注与复核组件

使用入口见仓库根 [README](../../README.zh-CN.md)。文档构建需要显式指定输入路径。

## 已有产物的人工复核

```bash
folioscribe-viewer --data-root /path/to/batch
```

完整交互说明见 [Viewer](viewer/README.md)。服务默认只监听本机；需要团队内访问时显式传入 `--host`。

## 证据构建与模型复核

```bash
python -m pip install -e '.[annotation]'
python components/annotation/scripts/build_document.py \
  --output data/annotation --sample-id example --full-id FULL_ID --short-id SHORT_ID \
  --pdf /path/to/document.pdf --all-pages \
  --mineru-root /path/to/mineru --paddle-root /path/to/paddle --ovis-root /path/to/ovis
```

输入是解析器已有产物，不会自动安装或运行 MinerU/Paddle/Ovis。full/short id 必须与解析器产物命名匹配。可通过 `--source-docx` 提供 Word 表格证据；`--anydoc-outline` 可传入已物化的 outline。直接读取 AnyDoc 需要用户自行安装相应包或设置 `ANYDOC_PACKAGE_DIR`。

需要模型语义复核时：

```bash
cp .env.example .env
# 编辑 .env 中自己的 API 配置，然后加载：
set -a
source .env
set +a
python components/annotation/scripts/run_document.py --document-root data/annotation --all-pages
```

API 费用与数据发送由你配置的端点决定。本地合成 Viewer 演示不调用模型。

## GT 导出

```bash
python components/annotation/scripts/export_contract_gt.py --document-root data/annotation
```

标准 JSON Schema 未包含在当前仓库；需要严格校验时使用 `--schema-root` 或环境变量 `DOCPARSE_SCHEMA_ROOT`。未提供、缺少文件或校验失败均不会被标为已验证。

人工补丁存于 `annotations/`，当前 GT 导出链主要基于自动运行结果；人工终稿到训练样本的适配仍需要实现和测试，不能假定 Viewer 保存后已自动进入 SFT。

## 阅读顺序

1. `src/ocr_agent/evidence_builder.py`：PDF/解析器/Word 证据到候选块。
2. `inventory_rules.py`、`reading_order.py`：确定性库存和阅读顺序。
3. `review_tasks.py`、`task_agent.py`、`task_provider.py`、`decision_applier.py`：受约束模型复核。
4. `cross_page.py`、`title_consistency.py`：跨页关系及标题一致性。
5. `schemas.py`、`validators.py`、`contract_gt.py`：内部检查和外部投影。

## 批处理

以 [`examples/annotation_batch.json`](../../examples/annotation_batch.json) 为模板，填入真实 PDF、解析器目录和页数。相对路径以清单文件所在目录解析；所有样本必须显式提供三路解析器根目录，不会自动搜索机器上的其他数据。

```bash
python components/annotation/scripts/run_document_batch.py \
  --registry examples/annotation_batch.json --output data/annotation-batch \
  --workers 2 --build-workers 2
```

模型阶段会使用当前 `OCR_AGENT_*` 配置。重复样本 ID、缺少路径或不合法标识符会在开始写入前被拒绝。

数据合同说明在 `docs/`，可复用的语料筛选规则位于 `src/ocr_agent/corpus_selection.py`；调用方负责提供自己的资产与 profile。
