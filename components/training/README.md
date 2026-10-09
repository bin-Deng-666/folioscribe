# 多页训练数据与评测

当前实现使用六类型 Markdown DSL，格式定义在 `sft/output_format.py`，文档状态由 `sft/document_state.py` 的 `DocState` 维护。训练投影在生成当前页 prompt 后才摄入当前页答案。

## 输入和格式

当前 projector 消费 wiki2gt-shaped 输入：

```text
<input-root>/
  registry.json
  split.json                       # train/test 文档 ID，必须不相交
  docs/<space>/<document>/
    page_images/page_0001.png
    gt/gt_raw.json                  # elements + table_structure/fragments
    gt/gt_bundle.json               # a1_entities
    gt/canonical_doc_graph.json
    gt/page_geometry.json
```

页面文件名与元数据 `page` 为 1-based，prompt 页号和 DSL `<|loc|>` 为 0-based。不要直接把 annotation 的 Contract GT 文件改名后交给 projector；结构差异见[架构](../../docs/architecture.md)。

## 可复现样例

从仓库根目录运行：

```bash
python examples/make_demo.py --output data/training-demo
folioscribe-project --input-root data/training-demo --roundtrip
python components/training/scripts/build_train_set.py \
  --input-root data/training-demo --output data/datasets --split train
```

输出 `wiki2gt_train.jsonl`、`wiki2gt_train.clean.jsonl` 和 `wiki2gt_train.manifest.json`。构建会检查 split 重叠、重复文档、页图缺失与 DSL 回读；验证失败不会发布部分主 JSONL。manifest 记录格式、prompt 和图像版本。

自己的 registry 可通过 `scripts/build_split.py --input-root ...` 建立文档级 split。当前切分保留原有的按桶/space 分组与字典序策略；不适合作为已验证的通用无偏抽样，近重复文档检测仍待补充。

## 推理与评测

```bash
python -m pip install -e '.[inference]'
python components/training/scripts/evaluate_documents.py \
  --input-root /path/to/wiki2gt --endpoint http://127.0.0.1:8000/v1 \
  --model YOUR_MODEL --out data/reports/evaluation.json --save-preds
```

文档之间并发，文档内按页推进状态。页图缺失会报错，不会跳过一页后继续使用过期上下文。当前指标是工程回归指标：标题、表数、表行数与页/类型级事件计数，不能代替关系级匹配或真实 OCR 精度。

查看预测：

```bash
python components/training/scripts/view_predictions.py \
  --model "experiment=data/reports/evaluation.json"
```

主标注 UI 是 `folioscribe-viewer`；这个轻量预测 viewer 暂时保留，统一 GT/预测对比尚未接入主 UI。

## 训练环境

本仓库当前提供数据构建与评测，不包含可直接复现全部历史实验的锁定 CUDA 环境或训练 launcher。原项目采用 Qwen 多模态模型和 ms-swift，外部训练器消费 `*.clean.jsonl` 的 `messages` 和 `images`。

GPU 训练、LoRA 合并、on-policy 数据生成和 Logics baseline 需要各自的模型/运行时。`merge_lora.py` 的基础模型路径由 `--base` 显式提供；`build_onpolicy.py` 使用已部署端点和 `--input-root`，不加载本地 adapter。

Logics baseline 使用[部署指南](../../docs/deployment/training_models.md)中的固定提交官方脚本，本仓库不包含其推理实现。

## 版本迁移

当前 `contract-v3.1` / `prompt-v7` 定义上下文、页码、多行表格与续接语义。修改协议后必须重建训练集并重跑评测，不同指标版本不能直接比较。
