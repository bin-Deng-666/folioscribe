"""v3 DSL 输出的 viewer 服务器：逐页看 input prompt / output DSL / markdown 视图 / 状态。

数据源（按 sample 目录约定）：
  <runs-root>/<model-tag>/<sample>/pages/page_XXXX.json   # {"input": {...}, "output_dsl_lines": [...]}
  或 evaluate_documents.py --save-preds 产出的 round<N>_doc60.json 的 preds 字段
API:
  /api/models                          模型列表（--model 可重复）
  /api/samples?model=                  样本列表
  /api/page/<model>/<sample>/<page>    单页 {input_prompt, dsl, markdown, blocks}
  /api/document/<model>/<sample>       整档拼合视图
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sft.output_format import dsl_to_markdown, parse_blocks  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _load_preds(self, model: str):
        """从 runs-root 下找该模型的 preds 数据（目录或 jsonl）。"""
        roots = self.server.model_roots.get(model, [])
        for root in roots:
            root = Path(root)
            if root.is_file() and root.suffix == ".json":  # roundN_doc60.json 风格
                data = json.loads(root.read_text())
                return data.get("preds") or {}, data.get("docs") or {}
            if root.is_dir():
                samples = {}
                for sample_dir in sorted(root.iterdir()):
                    pages_dir = sample_dir / "pages"
                    if not pages_dir.is_dir():
                        continue
                    pages = {}
                    for pf in sorted(pages_dir.glob("page_*.json")):
                        try:
                            pages[int(pf.stem.split("_")[1])] = json.loads(pf.read_text())
                        except Exception:
                            continue
                    if pages:
                        samples[sample_dir.name] = pages
                if samples:
                    return samples, {}
        return {}, {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/api/models":
            return self._json({"models": list(self.server.model_roots.keys())})
        if path == "/api/samples":
            model = (query.get("model") or [""])[0]
            preds, _ = self._load_preds(model)
            listing = [{"sample": sid, "pages": sorted(p)} for sid, p in sorted(preds.items())]
            return self._json({"model": model, "samples": listing})
        match = re.match(r"^/api/page/([^/]+)/(.+)/(\d+)$", path)
        if match:
            model, sample, page = match.group(1), match.group(2), int(match.group(3))
            preds, _ = self._load_preds(model)
            page_data = (preds.get(sample) or {}).get(page)
            if page_data is None:
                return self._json({"error": f"page not found: {model}/{sample}/p{page}"}, 404)
            raw = page_data.get("output_raw") or page_data.get("raw") or ""
            if "output_dsl_lines" in page_data and "output_raw" not in page_data:
                raw = "\n".join(page_data["output_dsl_lines"])
            blocks = parse_blocks(raw)
            return self._json({
                "model": model, "sample": sample, "page": page,
                "input_prompt": page_data.get("input_prompt") or (page_data.get("input") or {}).get("prompt_window"),
                "dsl": raw,
                "markdown": dsl_to_markdown(blocks),
                "blocks": [{"type": b["type"], "level": b.get("level"), "page": b.get("page"),
                            "bbox": b.get("bbox"), "truncated": b.get("truncated"),
                            "continued": b.get("continued"),
                            "content": (b.get("content") or "")[:400]} for b in blocks],
            })
        if path == "/" or path == "/index.html":
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._json({"error": "not found"}, 404)


INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>SFT v3 Viewer</title>
<style>
body{font-family:system-ui;margin:0;display:flex;height:100vh}
#side{width:260px;border-right:1px solid #ddd;overflow:auto;padding:8px}
#main{flex:1;overflow:auto;padding:16px;font-family:ui-monospace,monospace;font-size:13px}
select,input{width:100%;margin:4px 0}
.item{padding:4px 6px;cursor:pointer;border-radius:4px}
.item:hover{background:#eef}
.item.sel{background:#dbe7ff}
h3{margin:8px 0 4px;font-size:13px;color:#555}
pre{white-space:pre-wrap;word-break:break-word;background:#f7f7f8;padding:8px;border-radius:6px}
.block{margin:6px 0;padding:6px 10px;border-left:3px solid #bbb;background:#fafafa;border-radius:0 6px 6px 0}
.mark{color:#0366d6;font-weight:600}
.t-title{border-color:#d6a419}.t-table{border-color:#2da44e}.t-image{border-color:#8250df}
.t-header,.t-footer{color:#777;border-color:#ccc;background:#f3f3f3}
.trunc{color:#cf222e;font-size:11px}.cont{color:#0550ae;font-size:11px}
</style></head><body>
<div id="side">
  <h3>model</h3><select id="model"></select>
  <h3>samples</h3><div id="samples"></div>
</div>
<div id="main"><div id="detail">选择左侧样本</div></div>
<script>
const $ = s => document.querySelector(s);
async function j(url){ return (await fetch(url)).json(); }
async function init(){
  const ms = (await j('/api/models')).models;
  $('#model').innerHTML = ms.map(m=>`<option>${m}</option>`).join('');
  $('#model').onchange = loadSamples;
  await loadSamples();
}
async function loadSamples(){
  const model = $('#model').value;
  const data = await j('/api/samples?model='+encodeURIComponent(model));
  $('#samples').innerHTML = data.samples.map(s=>
    `<div class="item" data-s="${s.sample}">${s.sample} <small>(${s.pages.length}p)</small></div>`).join('');
  document.querySelectorAll('.item').forEach(el=>el.onclick=()=>show(el.dataset.s));
}
async function show(sample){
  const model = $('#model').value;
  document.querySelectorAll('.item').forEach(e=>e.classList.toggle('sel', e.dataset.s===sample));
  const pages = (await j('/api/samples?model='+encodeURIComponent(model))).samples
                  .find(s=>s.sample===sample).pages;
  let html = `<h3>${sample}</h3>`;
  for(const p of pages){
    const d = await j(`/api/page/${encodeURIComponent(model)}/${encodeURIComponent(sample)}/${p}`);
    html += `<h4>Page ${p}</h4>`;
    html += d.blocks.map(b=>{
      const extra = (b.truncated?' <span class="trunc">⤷truncated</span>':'')
                  + (b.continued?' <span class="cont">⤷continued</span>':'')
                  + (b.level?` <small>L${b.level}</small>`:'')
                  + (b.bbox?` <small>bbox[${b.bbox.join(',')}]</small>`:'');
      return `<div class="block t-${b.type}"><span class="mark">&lt;|${b.type}|&gt;</span>${extra}<br>${(b.content||'').replace(/</g,'&lt;')}</div>`;
    }).join('');
    html += `<details><summary>markdown 视图</summary><pre>${d.markdown.replace(/</g,'&lt;')}</pre></details>`;
    html += `<details><summary>input prompt</summary><pre>${(d.input_prompt||'').replace(/</g,'&lt;')}</pre></details>`;
  }
  $('#detail').innerHTML = html;
}
init();
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8747)
    parser.add_argument("--model", action="append", default=[],
                        help="model=路径或 json 文件（可重复）：tag=path[:path2...]")
    args = parser.parse_args()
    model_roots: dict[str, list] = {}
    for spec in args.model:
        tag, _, roots = spec.partition("=")
        model_roots[tag] = roots.split(":")
    Handler.server = None
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.model_roots = model_roots
    print(f"v3 viewer on :{args.port} | models: {list(model_roots)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
