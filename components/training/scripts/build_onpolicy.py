"""整文档 on-policy 训练集构建（context 来自模型自身推理；只实现，待 gt 模式验证后再训）。

原理：沿推理路径逐窗口/逐页生成 —— DocState 摄入**模型输出**（非 GT），下一页的
title_tree / prev_page_tail 由此产生（训练样本 = 模型自产条件 + GT 目标），
消除 teacher forcing 与推理分布的曝光偏差（DAgger 式数据聚合）。

与 gt 模式的区别：
  gt:       state 演化吃 GT 投影块（单页独立，页间互不依赖，可并发）
  onpolicy: state 演化吃模型输出块（页间依赖，必须按文档串行）

用法:
  python components/training/scripts/build_onpolicy.py --input-root data/wiki2gt \
      --limit 20 --out data/datasets/onpolicy.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20, help="文档篇数")
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8067/v1",
                        help="复用已部署的 vLLM 服务（adapter 已合并进权重的场景）")
    parser.add_argument("--model", default="qwen35-2b-sft")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    from openai import OpenAI
    import base64
    from sft.document_state import DocState
    from sft.projection import project

    client = OpenAI(base_url=args.endpoint, api_key="EMPTY")
    root = args.input_root
    args.out.parent.mkdir(parents=True, exist_ok=True)
    ids = json.loads((root / "split.json").read_text())["train"][: args.limit]
    kept_docs, total = 0, 0
    with args.out.open("w") as fh:
        for sid in ids:
            space, name = sid.split("/", 1)
            samples = project(root / "docs" / space / name, space, name, args.max_pages)
            if any(not Path(g["image"]).is_file() for g in samples):
                raise FileNotFoundError(f"{sid}: missing page image")
            if not samples:
                continue
            state = DocState()
            for item in samples:
                ctx = state.context_for(item["page"])
                # 用推理路径同构的 prompt（build_prompt 是同一个函数）
                from sft.output_format import build_prompt
                prompt = build_prompt(item["page"] - 1, item["total_pages"], ctx["title_tree"],
                                      ctx["prev_page_tail"] or "")
                b64 = base64.b64encode(Path(item["image"]).read_bytes()).decode()
                try:
                    resp = client.chat.completions.create(model=args.model,
                        messages=[{"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            {"type": "text", "text": prompt}]}],
                        max_tokens=4096, temperature=0)
                    raw = resp.choices[0].message.content or ""
                except Exception as exc:
                    print(f"[INFER-FAIL] {sid} p{item['page']}: {exc}", flush=True)
                    break  # A failed page must not silently provide stale context to later pages.
                # 训练样本 = 模型自产条件 + GT 目标
                fh.write(json.dumps({
                    "messages": [{"role": "user", "content": "<image>" + prompt},
                                 {"role": "assistant", "content": item["target"]}],
                    "images": [item["image"]],
                    "sample_id": sid, "page": item["page"], "mode": "onpolicy",
                }, ensure_ascii=False) + "\n")
                state.ingest(item["page"], raw)   # 状态机吃模型输出 -> 下一页"带病"传播
                total += 1
            kept_docs += 1
            print(f"[{kept_docs}/{len(ids)}] {sid} pages={len(samples)}", flush=True)
    print(f"on-policy 样本 {total} 条来自 {kept_docs} 篇 -> {args.out}")


if __name__ == "__main__":
    main()
