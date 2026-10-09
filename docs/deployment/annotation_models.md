# 标注侧解析器部署与产物接入

先阅读[公共准备](README.md)，所有相对路径都从 FolioScribe 仓库根解析。下面以 Linux、Python 3.12、NVIDIA GPU 为例；客户端新接入脚本尚未跑真实模型回归。

## MinerU

以下使用公开 **MinerU 2.5.4**，以匹配当前 content-list 读取器。其他版本或定制版本的产物需要重新验证兼容性。

```bash
python3.12 -m venv .model-envs/mineru
.model-envs/mineru/bin/python -m pip install 'mineru[core]==2.5.4'
.model-envs/mineru/bin/mineru-models-download -s huggingface -m vlm
# 国内也可使用：-s modelscope
```

下载工具会写入 MinerU 自己的本地模型配置。解析时使用 `--source local`，确保模型来源由刚才的下载明确确定。官方版本的 CLI 支持 `vlm-transformers`、`vlm-vllm-engine`、`vlm-http-client`；本例使用 Transformers，避免额外混装 vLLM。

```bash
.model-envs/mineru/bin/mineru \
  -p /absolute/path/to/document.pdf -o data/parsers/mineru-original \
  -b vlm-transformers --source local
```

需要 HTTP 服务时，这个版本也提供 `mineru-api`；先查看 `--help` 核对当前安装的启动参数：

```bash
.model-envs/mineru/bin/mineru-api --host 127.0.0.1 --port 8010
```

FolioScribe 消费文件产物，因此单文档 CLI 已足够，不要求长期启动服务。不要直接升级到 MinerU 4.x 后沿用 2.x 的 CLI 与输出目录。

## PaddleOCR-VL

权重下载使用公共准备里的 `download_models.py paddle`。服务端与客户端单独安装：

```bash
python3.12 -m venv .model-envs/paddle-server
.model-envs/paddle-server/bin/python -m pip install 'vllm==0.26.0'

PATH="$PWD/.model-envs/paddle-server/bin:$PATH" CUDA_VISIBLE_DEVICES=1 \
  bash scripts/deployment/serve_vlm.sh paddle data/models/PaddleOCR-VL-1.6
```

官方模型卡也提供其维护的 Docker 服务方案，可作为独立部署选项：

```bash
docker run --rm --gpus 'device=1' -p 127.0.0.1:8041:8080 \
  ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-genai-vllm-server:latest-nvidia-gpu \
  paddleocr genai_server --model_name PaddleOCR-VL-1.6-0.9B \
  --host 0.0.0.0 --port 8080 --backend vllm
```

容器会按官方逻辑下载模型。`latest` 不是复现锁，验收后记录实际 image digest。不要同时运行两条服务命令占用同一端口。

客户端运行 **官方 PaddleOCRVL pipeline**，CPU 做版面检测，识别调用上面的服务：

```bash
python3.12 -m venv .model-envs/paddle-client
.model-envs/paddle-client/bin/python -m pip install \
  'paddlepaddle==3.2.2' 'paddleocr[doc-parser]==3.7.0' PyMuPDF Pillow

.model-envs/paddle-client/bin/python components/annotation/scripts/parse_pages.py paddle \
  --pdf /absolute/path/to/document.pdf --short-id demo_doc \
  --endpoint http://127.0.0.1:8041/v1 --output data/parsers/paddle --limit 1
```

本客户端关闭方向分类与去畸变，让 `block_bbox` 与送入 pipeline 的整页图片坐标一致；使用 PyMuPDF 的 PDF 页旋转后图像。官方输出 JSON 如有外层 `res`，会规范化为根级 `parsing_res_list`。**不能用通用聊天接口直接识别整页来代替 pipeline**，因为会缺失有效的版面块与坐标。

产物：

```text
data/parsers/paddle/
  images/demo_doc/page_0001.png
  json/demo_doc/page_0001_res.json
  raw/demo_doc/page_0001.json
  demo_doc.manifest.json
```

## OvisOCR2

使用 **ATH-MaaS/OvisOCR2**，官方模型卡给出的运行时为 vLLM 0.22.1。不要替换为 Ovis2/Ovis2.5 通用视觉模型。

```bash
python3.12 -m venv .model-envs/ovis-server
.model-envs/ovis-server/bin/python -m pip install 'vllm==0.22.1' Pillow

PATH="$PWD/.model-envs/ovis-server/bin:$PATH" CUDA_VISIBLE_DEVICES=2 \
  bash scripts/deployment/serve_vlm.sh ovis data/models/OvisOCR2 \
  --mm-processor-kwargs '{"images_kwargs":{"min_pixels":200704,"max_pixels":8294400}}'
```

模板包含官方 `gdn_prefill_backend=triton` 设置。客户端沿用官方模型卡的提取 prompt、HTML 表格和图片占位约定，并设置 `enable_thinking=False`；不删除图片标签，以便保留原始证据。

```bash
python -m pip install -e '.[annotation,inference]'
python components/annotation/scripts/parse_pages.py ovis \
  --pdf /absolute/path/to/document.pdf --short-id demo_doc \
  --endpoint http://127.0.0.1:8031/v1 --model ovis-ocr2 \
  --output data/parsers/ovis --limit 1
```

产物为 `markdown/demo_doc/page_0001.md`、`images/demo_doc/page_0001.png` 与 `raw/` 原始响应。达到输出长度限制或空响应时会失败，不把不完整文本当成成功页。使用认证服务时设置 `FOLIOSCRIBE_MODEL_API_KEY`。

## 对齐三路产物并构建证据

首次用 `--limit 1` 检查；整本文档运行时去掉 Paddle/Ovis 的 `--limit`，保持两者 `--dpi` 相同。

MinerU CLI 产生的文件名跟输入 PDF 文件名有关，本项目原 reader 则需要如下固定位置。选中本次解析得到的 **content list JSON**，复制到期望路径，不修改 JSON 中的 `page_idx` 或 bbox：

```bash
# 替换为 MinerU 本次真实输出文件，不能指向中间结果 JSON
MINERU_CONTENT_LIST=/absolute/path/to/document_content_list.json
FULL_ID=demo_document
SHORT_ID=demo_doc
mkdir -p "data/parsers/mineru/$FULL_ID/extracted/$FULL_ID/vlm"
cp "$MINERU_CONTENT_LIST" \
  "data/parsers/mineru/$FULL_ID/extracted/$FULL_ID/vlm/${FULL_ID}_content_list.json"

python components/annotation/scripts/audit_evidence_assets.py \
  --pdf /absolute/path/to/document.pdf --full-id "$FULL_ID" --short-id "$SHORT_ID" \
  --mineru-root data/parsers/mineru --paddle-root data/parsers/paddle --ovis-root data/parsers/ovis \
  --report data/reports/parser-assets.json

python components/annotation/scripts/build_document.py \
  --output data/annotation --sample-id demo_document \
  --full-id "$FULL_ID" --short-id "$SHORT_ID" \
  --pdf /absolute/path/to/document.pdf --all-pages \
  --mineru-root data/parsers/mineru --paddle-root data/parsers/paddle --ovis-root data/parsers/ovis
```

Reader 需要 MinerU bbox 为 `[0,1000]`、页号 `page_idx` 从 0 开始，Paddle bbox 为整页像素且 JSON 的 `width/height` 与页图相符；客户端从实际栅格补齐这两个字段并检查冲突。Paddle/Ovis 文件名从 `page_0001` 开始。

审计脚本直接读取 PDF 页数，逐项检查文件名、两路页图尺寸、结构、坐标边界，以及可用 manifest 中的 PDF 哈希。它返回非零退出码表示接入不满足要求。空白页可能没有识别内容，此类情况记录 warning，不伪造文本。没有 manifest 的历史产物会记录来源哈希不可验证。

`parse_pages.py` manifest 记录完成页和 PDF 哈希；不使用 `--resume` 跳过未知来源旧结果。更换模型/分辨率时使用新的输出目录。

## Review provider

受约束语义复核通过 OpenAI-compatible 文本模型完成，代码不内置某个商业服务凭据或私有 URL。按你的已部署模型填写：

```bash
export OCR_AGENT_API_KEY='YOUR_LOCAL_KEY'
export OCR_AGENT_BASE_URL='http://127.0.0.1:YOUR_PORT/v1'
export OCR_AGENT_MODEL='YOUR_SERVED_MODEL_NAME'
export OCR_AGENT_VISION=0
python components/annotation/scripts/run_document.py --document-root data/annotation --all-pages
folioscribe-viewer --data-root data/annotation
```

远程商业 provider 的权重和部署方式由其供应商决定，本仓库只配置接口，不包含这些模型或凭据。
