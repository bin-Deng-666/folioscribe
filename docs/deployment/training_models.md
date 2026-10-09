# 训练基座与长文档对照模型

参见[公共下载命令](README.md)。以下命令部署推理；GPU SFT 训练需要独立的 ms-swift / torch 环境，目前没有经过验收的训练 launcher。

## Qwen

基座为 `Qwen/Qwen3.5-2B`，下载后位于 `data/models/Qwen3.5-2B`。使用 Linux + NVIDIA GPU，独立安装支持 Qwen3.5 的 vLLM：

```bash
python3.12 -m venv .model-envs/qwen-server
.model-envs/qwen-server/bin/python -m pip install 'vllm==0.26.0'
PATH="$PWD/.model-envs/qwen-server/bin:$PATH" CUDA_VISIBLE_DEVICES=0 \
  bash scripts/deployment/serve_vlm.sh qwen data/models/Qwen3.5-2B
```

视觉解析不能使用 `--language-model-only`。模板默认 32768 上下文，显存还受图像预算、输出长度和并发影响，需要在目标机器测试。

从 FolioScribe 环境评测：

```bash
python -m pip install -e '.[inference]'
curl --fail http://127.0.0.1:8067/v1/models
python components/training/scripts/evaluate_documents.py \
  --input-root /absolute/path/to/wiki2gt \
  --endpoint http://127.0.0.1:8067/v1 --model qwen35-2b \
  --limit 1 --pages 3 --concurrency 1 --save-preds \
  --out data/reports/qwen-smoke.json
```

目录需要测试 split 和真实页图。脚本设置 `enable_thinking=False`，目标是受约束 DSL。LoRA 合并后可将服务模型目录改为合并产物，注意 checkpoint 对应的格式与 prompt 版本。

## Logics

正式模型名称为 **Logics-Parsing-V3**。FolioScribe 不复制维护其推理实现，直接使用固定提交的[官方仓库](https://github.com/alibaba/Logics-Parsing)。

从 FolioScribe 根目录执行：

```bash
mkdir -p data/upstream
git clone https://github.com/alibaba/Logics-Parsing.git data/upstream/logics-parsing
git -C data/upstream/logics-parsing checkout d75d42dbd92c518a4def19ac5895792828138d35

python3.10 -m venv .model-envs/logics
.model-envs/logics/bin/python -m pip install \
  'vllm==0.22.1' 'transformers==5.6.0' Pillow PyMuPDF
.model-envs/logics/bin/python -m pip check
```

权重下载见公共指南的 `download_models.py logics`。按官方脚本解析 PDF 或图片目录：

```bash
CUDA_VISIBLE_DEVICES=0 .model-envs/logics/bin/python \
  data/upstream/logics-parsing/inference_v3.py \
  --model_path data/models/Logics-Parsing-V3 \
  --input_path /absolute/path/to/document.pdf \
  --output_dir data/baselines/logics-document
```

产物包含 `markdown.md`、`hierarchy.md`、`raw_output.dsl` 与 `result.json`，多页默认使用两页窗口。官方环境采用 Python 3.10，无需在其中安装要求 Python 3.11+ 的 FolioScribe 主包。

需要 OpenAI-compatible 模型服务时，可使用已有启动模板：

```bash
PATH="$PWD/.model-envs/logics/bin:$PATH" CUDA_VISIBLE_DEVICES=0 \
  bash scripts/deployment/serve_vlm.sh logics data/models/Logics-Parsing-V3
```

这个端点不自动提供官方窗口状态和后处理逻辑。直接生成完整 baseline 产物时使用上面的官方推理脚本；不要把自身六类型 DSL 的评测 prompt 当成 Logics 的原生协议。

## 训练环境与验证边界

推理命令与环境版本依据上游模型卡/官方示例整理。当前 CPU CI 不启动 GPU 服务，也不验证真实模型质量。在扩大训练或评测前，记录模型 commit、数据 manifest、依赖版本、GPU 配置和 smoke 结果。

待补齐的训练配置应包含 launcher、依赖锁、随机种子、图像预算、上下文长度与资源参数，详见[路线图](../../ROADMAP.md)。
