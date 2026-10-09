# 模型下载与部署

本项目不会在安装 Python 包或启动 Viewer 时下载模型。下面的命令供 **Linux + NVIDIA GPU** 机器执行；CPU CI 只检查本地脚本和数据格式，不启动这些 GPU 服务。

## 模型与角色

| 角色 | 本项目对应模型 | 服务 / 产物 | 指南 |
| --- | --- | --- | --- |
| 布局与文字候选 | MinerU 2.5 系列 | 文档级 `content_list.json` | [标注模型](annotation_models.md#mineru) |
| 版面检测与区域识别 | PaddleOCR-VL-1.6 | 官方 pipeline 的 `parsing_res_list` + 页图 | [标注模型](annotation_models.md#paddleocr-vl) |
| 页级 Markdown 证据 | **ATH-MaaS/OvisOCR2** | 页 Markdown + 页图 | [标注模型](annotation_models.md#ovisocr2) |
| 多页 SFT 基座 | Qwen/Qwen3.5-2B | OpenAI-compatible 视觉推理接口 | [训练与对照](training_models.md#qwen) |
| 长文档对照 | Logics-MLLM/Logics-Parsing-V3 | 窗口 DSL、整档 Markdown | [训练与对照](training_models.md#logics) |
| 语义复核 | 用户配置的聊天模型 | `OCR_AGENT_*` API 配置 | [标注模型](annotation_models.md#review-provider) |

OvisOCR2 不是 `AIDC-AI/Ovis2.5-*`，不能因为名字接近而替换模型。不同 MinerU 版本的输出也需要单独验证。

## 目录和环境

从 FolioScribe 仓库根运行以下公共准备命令：

```bash
mkdir -p .model-envs data/models data/parsers data/deployment
python3.12 -m venv .model-envs/download
.model-envs/download/bin/python -m pip install 'huggingface_hub>=0.34'

.model-envs/download/bin/python scripts/deployment/download_models.py paddle --output data/models
.model-envs/download/bin/python scripts/deployment/download_models.py ovis --output data/models
.model-envs/download/bin/python scripts/deployment/download_models.py qwen --output data/models
.model-envs/download/bin/python scripts/deployment/download_models.py logics --output data/models
```

可一次只下载需要的模型。脚本解析上游 revision 为不可变 commit，再下载到 `data/models/<模型名>` 并写入 `folioscribe-source.json`；复现实验时使用 `--revision <commit>`。下载失败不会被当作部署成功。网络需要镜像时由你配置 `HF_ENDPOINT`，私有模型访问使用本地 `HF_TOKEN`。

模型目录、虚拟环境和推理产物不进入 Git。每种运行时单独建立环境，尤其不要把 PaddlePaddle、vLLM 与 SFT 训练依赖一起塞进项目 `.venv`。

启动模板：

```bash
# 激活所需运行时后调用；参数说明见各模型指南
CUDA_VISIBLE_DEVICES=0 bash scripts/deployment/serve_vlm.sh ovis data/models/OvisOCR2
```

`FOLIOSCRIBE_SERVE_HOST` 默认 `127.0.0.1`，`FOLIOSCRIBE_SERVE_PORT` 可覆盖端口，`FOLIOSCRIBE_MAX_MODEL_LEN` 默认 32768，`FOLIOSCRIBE_GPU_MEMORY` 默认 0.8，`FOLIOSCRIBE_TP_SIZE` 默认 1。KV cache 与图像预算影响显存，不承诺某张卡能跑所有配置；根据 OOM 信息降低上下文、像素预算或并发。

服务在前台运行，Ctrl+C 关闭。团队部署由你自己的进程管理器或容器服务托管。不要把无认证模型端口直接暴露到公共网络。

## 验证与运行记录

先检查服务模型清单，再处理一页真实/合成输入，最后检查本项目需要的结构文件。`/v1/models` 返回 200 只能说明接口可达，不能证明解析产物合格。

```bash
curl --fail http://127.0.0.1:8031/v1/models
python -m pip freeze > data/deployment/runtime.txt
nvidia-smi > data/deployment/gpu.txt
```

多解析器接入的完整命令与路径约定见[标注模型](annotation_models.md)。检查一页后再全量；记录模型 revision、运行时、PDF 哈希、页码与渲染分辨率，保持证据可追溯。

## 核对来源

- [MinerU 2.5.4 README](https://github.com/opendatalab/MinerU/tree/mineru-2.5.4-released)、[该版本 CLI](https://github.com/opendatalab/MinerU/blob/mineru-2.5.4-released/mineru/cli/client.py)、[模型下载工具](https://github.com/opendatalab/MinerU/blob/mineru-2.5.4-released/mineru/cli/models_download.py)。当前主分支已是不同的 CLI 代际，因此兼容路线明确锁定 2.5.4。
- [PaddleOCR-VL-1.6 模型卡](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)、[官方 pipeline 文档](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html)。模型卡通过镜像读取，内容链接仍指向官方来源。
- [OvisOCR2 官方模型卡](https://modelscope.cn/models/ATH-MaaS/OvisOCR2)、[Hugging Face](https://huggingface.co/ATH-MaaS/OvisOCR2)。模型卡说明其页级 Markdown 提取协议。
- [Qwen3.5-2B 模型卡](https://modelscope.cn/models/Qwen/Qwen3.5-2B)、[vLLM recipe](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html)。
- [Logics-Parsing 上游](https://github.com/alibaba/Logics-Parsing)，官方说明给出 vLLM 0.22.1 / Transformers 5.6.0。

这些是安装与接入参考，具体 GPU 运行验证仍需在目标机器完成。
