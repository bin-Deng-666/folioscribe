"""把 LoRA adapter 合并进基座并导出（用于 vLLM 部署）。

用法:
  python components/training/scripts/merge_lora.py --adapter data/training/adapter --base data/models/Qwen3.5-2B --out data/models/merged
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ap = argparse.ArgumentParser()
ap.add_argument("--adapter", required=True)
ap.add_argument("--base", required=True)
ap.add_argument("--out", required=True)
args = ap.parse_args()

import os
os.environ["CUDA_VISIBLE_DEVICES"] = ""
import torch
from peft import PeftModel
from transformers import AutoProcessor, Qwen3_5ForConditionalGeneration

# CPU 上合并（GPU 训练占满时也能跑）
model = Qwen3_5ForConditionalGeneration.from_pretrained(
    args.base, dtype=torch.bfloat16, attn_implementation="sdpa", device_map="cpu")
model = PeftModel.from_pretrained(model, args.adapter)
model = model.merge_and_unload()
out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
model.save_pretrained(out, safe_serialization=True)
proc = AutoProcessor.from_pretrained(args.base, trust_remote_code=True)
proc.save_pretrained(out)
# vLLM 需要 tokenizer.json 与 chat template（基座目录直接拷贝）
import shutil
for f in ["tokenizer.json", "chat_template.jinja"]:
    src = Path(args.base) / f
    if src.is_file() and not (out / f).exists():
        shutil.copy(src, out / f)
print("merged ->", out)
