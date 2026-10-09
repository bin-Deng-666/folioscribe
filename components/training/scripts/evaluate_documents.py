"""并发文档级评测器（vLLM OpenAI 兼容接口）。

与 eval_doc.py 同一套 doc_metrics（表折叠/行守恒/标题文本召回/层级/事件 P·R），
但推理改为 AsyncOpenAI 并发调用，支持整篇文档作为任务单元：
  - 每篇文档一个协程，逐页顺序推理（页间有依赖：state 自产上下文），
  - 多篇文档之间并发（互不依赖），vLLM 端持续吃满 batch。

用法:
  python scripts/evaluate_documents.py --endpoint http://127.0.0.1:8079/v1 \
      --model qwen35-2b --limit 60 --out reports/round2_doc_vllm.json
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sft.output_format import build_prompt, CONTRACT_VERSION, PROMPT_TEMPLATE_VERSION  # noqa: E402
HEAD = re.compile(r"^(#{1,6})\s+(.+)$", re.M)


def _norm(x: str) -> str:
    return re.sub(r"[\s\u00a0\u3000]+", " ", x).strip()


def document_metrics(state, gts: list[dict]) -> dict:
    """v3 文档级指标：模型块流（已过 DocState 合并） vs GT 投影块流。

    事件配对从"页级字符串"升级为"块级 truncated/continued 配对成功数"。
    """
    from sft.output_format import parse_blocks
    pred_blocks = state.assemble_blocks()
    from sft.document_state import DocState
    gt_state = DocState()
    gt_blocks_all = []
    for g in gts:
        gt_blocks_all.extend(parse_blocks(g["target"]))
        gt_state.ingest(g["page"], g["target"])
    gt_merged = gt_state.assemble_blocks()

    def tables(blocks):
        return [b for b in blocks if b["type"] == "table"]
    gt_t, pd_t = len(tables(gt_merged)), len(tables(pred_blocks))
    gt_rows = sum((_html_rows_count(b["content"])) for b in tables(gt_merged))
    pd_rows = sum((_html_rows_count(b["content"])) for b in tables(pred_blocks))

    gt_titles = {(b.get("level"), _norm(b.get("content") or ""))
                 for b in gt_blocks_all if b["type"] == "title"}
    pd_titles = {(b.get("level"), _norm(b.get("content") or ""))
                 for b in pred_blocks if b["type"] == "title"}
    gt_any = {t for _, t in gt_titles}
    pd_any = {t for _, t in pd_titles}

    # Compare events before merging, by page/type/event; merged blocks lose these flags.
    from collections import Counter
    def event_counts(pages):
        return Counter((page, block["type"], event)
                       for page, raw in pages
                       for block in parse_blocks(raw)
                       for event in ("truncated", "continued") if block.get(event))
    gc = event_counts((g["page"], g["target"]) for g in gts)
    pc = event_counts((trace["page"], raw) for trace, raw in zip(state.trace, state.pages))
    hit = sum((gc & pc).values())
    ev_p = round(hit / sum(pc.values()), 3) if pc else float(not gc)
    ev_r = round(hit / sum(gc.values()), 3) if gc else float(not pc)

    return {"tables_gt": gt_t, "tables_pred": pd_t,
            "table_fold": round(pd_t / gt_t, 3) if gt_t else 1.0,
            "row_keep": round(min(pd_rows, gt_rows) / gt_rows, 3) if gt_rows else 1.0,
            "title_text_recall": round(len(pd_any & gt_any) / len(gt_any), 3) if gt_any else 1.0,
            "title_recall": round(len(pd_titles & gt_titles) / len(gt_titles), 3) if gt_titles else 1.0,
            "level_ok": round(len(pd_titles & gt_titles) / max(1, len(pd_titles)), 3),
            "ev_p": ev_p, "ev_r": ev_r}


def _html_rows_count(html: str) -> int:
    return len(re.findall(r"<tr", html or "", flags=re.I))


async def infer_page(client, model: str, prompt: str, image_path: str, sem,
                     max_tokens: int = 2048) -> str:
    import base64
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    async with sem:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt}]}],
            max_tokens=max_tokens, temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    return resp.choices[0].message.content.strip()


async def run_doc(client, model: str, sid: str, samples: list[dict], sem) -> tuple[str, dict, list, list]:
    from sft.document_state import DocState
    state = DocState()
    preds, gts = [], []
    for item in samples:
        ctx = state.context_for(item["page"])
        prompt = build_prompt(item["page"] - 1, item["total_pages"], ctx["title_tree"], ctx["prev_page_tail"] or "")
        raw = await infer_page(client, model, prompt, item["image"], sem)
        preds.append({"page": item["page"], "raw": raw})
        gts.append({"page": item["page"], "target": item["target"], "head": item["head"], "tail": item["tail"]})
        state.ingest(item["page"], raw)   # v3: 状态机直接吃 DSL，内部解析/配对/合并
    metrics = document_metrics(state, gts)
    return sid, metrics, preds, gts


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-root", type=Path, required=True)
    ap.add_argument("--endpoint", default="http://127.0.0.1:8079/v1")
    ap.add_argument("--model", default="qwen35-2b")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--pages", type=int, default=40)
    ap.add_argument("--concurrency", type=int, default=16, help="并发文档数（页间仍串行）")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--save-preds", action="store_true")
    args = ap.parse_args()
    root = args.input_root
    if args.concurrency < 1 or args.pages < 1 or args.limit < 1:
        ap.error("concurrency, pages and limit must be positive")

    from openai import AsyncOpenAI
    from sft.projection import project
    client = AsyncOpenAI(base_url=args.endpoint, api_key="EMPTY")
    sem = __import__("asyncio").Semaphore(args.concurrency)

    ids = json.loads((root / "split.json").read_text())["test"][: args.limit]
    docs = []
    for sid in ids:
        space, doc = sid.split("/", 1)
        samples = project(root / "docs" / space / doc, space, doc, args.pages)
        missing = [g["image"] for g in samples if not Path(g["image"]).is_file()]
        if missing:
            raise FileNotFoundError(f"{sid}: missing page image {missing[0]}")
        if samples:
            docs.append((sid, samples))
    print(f"评测 {len(docs)} 篇 / {sum(len(s) for _, s in docs)} 页，并发 {args.concurrency}", flush=True)

    if not docs:
        await client.close()
        raise ValueError("No evaluation documents found")
    results, all_preds = {}, {}
    done = 0
    tasks = [run_doc(client, args.model, sid, samples, sem) for sid, samples in docs]
    for fut in asyncio.as_completed(tasks):
        sid, m, preds, _ = await fut
        results[sid] = m
        if args.save_preds:
            all_preds[sid] = preds
        done += 1
        if done % 10 == 0:
            print(f"  {done}/{len(docs)} 完成", flush=True)

    n = max(1, len(results))
    mean = {k: round(sum(m[k] for m in results.values()) / n, 3) for k in next(iter(results.values()))}
    await client.close()
    payload = {"n_docs": len(results), "mean": mean, "docs": results,
               "contract_version": CONTRACT_VERSION, "prompt_template_version": PROMPT_TEMPLATE_VERSION,
               "metric_version": "document-v3.1"}
    if args.save_preds:
        payload["preds"] = all_preds
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(json.dumps({"n_docs": len(results), **mean}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
