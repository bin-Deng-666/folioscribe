# Third-party software and models

FolioScribe source code is licensed under Apache-2.0. Dependencies, model checkpoints and external tools retain their own licenses; the project license does not relicense them.

## Optional parsing dependencies

- **PyMuPDF** — PDF reading and rendering, used by the optional `annotation` extra. Upstream offers AGPL and commercial licensing: https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright
- **Pillow**, **lxml**, **jsonschema**, **OpenAI Python SDK**, **Playwright** and other installed packages are distributed by their respective projects. Their package metadata contains license and attribution information.

## External models

Deployment instructions reference these independently downloaded projects:

- MinerU: https://github.com/opendatalab/MinerU
- PaddleOCR / PaddleOCR-VL: https://github.com/PaddlePaddle/PaddleOCR
- OvisOCR2: https://huggingface.co/ATH-MaaS/OvisOCR2
- Qwen3.5: https://huggingface.co/Qwen/Qwen3.5-2B
- Logics-Parsing: https://github.com/alibaba/Logics-Parsing

No model weights or upstream inference implementations are bundled. Logics baseline instructions use a pinned checkout of the official repository. The Ovis client prompt follows the public model-card task format; its source is linked in the deployment guide.

Screenshots in this repository were generated from the included synthetic demo. Production documents, historical annotation screenshots, internal deployment notes and credentials are not distributed.
