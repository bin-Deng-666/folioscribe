#!/usr/bin/env python3
"""Measure the viewer's interactive latency and fail when it regresses.

Numbers this script produced on the 8×4090 host (batch v10, 500-document catalog):

  catalog, cold (server restarted)     0.9 s   <- reads every result.json once
  catalog, warm (repeated fetch)      0.01 s   <- gzip + signature-invalidated cache
  catalog payload                      25 KB   (444 KB before gzip)

  first screen, 240-document catalog   1.0 s
  page switch (detail + overlay)      55 ms
  drag frame (block move)           0.88 ms   (3.0 ms before the in-place DOM update)
  keystroke in the editor           0.02 ms

Usage:
  PLAYWRIGHT_BROWSERS_PATH=<dir with chromium> python3 scripts/viewer_perf.py [--base URL] [--batch NAME]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://127.0.0.1:8745")
parser.add_argument("--batch", default="ocr_agent_batch_v10")
parser.add_argument("--min-pages", type=int, default=5, help="pick a document with at least this many pages")
parser.add_argument("--budget-page-switch-ms", type=float, default=600.0)
parser.add_argument("--budget-drag-ms", type=float, default=4.0)
parser.add_argument("--budget-first-screen-s", type=float, default=6.0)
parser.add_argument("--json", type=Path, default=None, help="write the measurements here")
args = parser.parse_args()

PROBE = """() => {
  const time = (fn, n = 1) => { const started = performance.now(); for (let i = 0; i < n; i += 1) fn(); return (performance.now() - started) / n; };
  return {
    renderCatalog: time(() => renderCatalog()),
    renderOverlay: time(() => renderOverlay(), 5),
    renderBlockList: time(() => renderBlockList(), 5),
    renderDetail: time(() => renderDetail(), 5),
  };
}"""

DRAG = """() => {
  const overlay = els.overlay.getBoundingClientRect();
  const x = overlay.left + overlay.width * 0.5;
  const y = overlay.top + overlay.height * 0.55;
  const started = performance.now();
  els.overlay.dispatchEvent(new PointerEvent("pointerdown", {clientX: x, clientY: y, bubbles: true, pointerId: 1}));
  for (let i = 0; i < 60; i += 1) {
    els.overlay.dispatchEvent(new PointerEvent("pointermove", {clientX: x + (i % 20), clientY: y + (i % 20), bubbles: true, pointerId: 1}));
  }
  const perFrame = (performance.now() - started) / 60;
  els.overlay.dispatchEvent(new PointerEvent("pointerup", {clientX: x + 10, clientY: y + 10, bubbles: true, pointerId: 1}));
  return perFrame;
}"""

TYPING = """() => {
  const field = document.querySelector("#editorText");
  if (!field) return null;
  const started = performance.now();
  for (let i = 0; i < 20; i += 1) {
    field.value += "字";
    field.dispatchEvent(new Event("input", {bubbles: true}));
  }
  return (performance.now() - started) / 20;
}"""

failures: list[str] = []
result: dict[str, float] = {}


def check(label: str, value: float, budget: float, unit: str) -> None:
    mark = "OK  " if value <= budget else "FAIL"
    if value > budget:
        failures.append(f"{label}: {value:.2f}{unit} > {budget:.2f}{unit}")
    print(f"  {mark} {label:34s} {value:8.2f}{unit}")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1700, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)[:200]))

    # Pick a document that actually has a few pages, so page switching is measurable.
    catalog = json.loads(page.request.get(f"{args.base}/api/catalog?batch={args.batch}").text())
    documents = [item for item in catalog["documents"] if item["page_count"] >= args.min_pages]
    if not documents:
        raise SystemExit(f"no document with >= {args.min_pages} pages in {args.batch}")
    sample, pages = documents[0]["sample_id"], documents[0]["pages"]

    started = time.time()
    page.goto(f"{args.base}/?batch={args.batch}&sample={sample}&page={pages[0]['page']}", wait_until="networkidle")
    result["first_screen_s"] = round(time.time() - started, 2)
    check("first screen", result["first_screen_s"], args.budget_first_screen_s, "s")
    print(f"       catalog: {len(catalog['documents'])} documents, sample {sample} ({len(pages)} pages)")
    time.sleep(1.0)

    for label, value in page.evaluate(PROBE).items():
        result[label] = round(value, 2)
        print(f"       {label:34s} {value:8.2f}ms")

    target = pages[min(2, len(pages) - 1)]["page"]
    switch = page.evaluate("async ([sample, target]) => { const a = performance.now(); await selectPage(sample, target); return performance.now() - a; }", [sample, target])
    result["page_switch_ms"] = round(switch, 1)
    check("page switch", switch, args.budget_page_switch_ms, "ms")

    page.click('.mode-switch button[data-mode="annotate"]')
    time.sleep(0.8)
    page.click('#tabs button[data-tab="blocks"]')
    time.sleep(0.4)
    page.locator(".block-item.annotate").first.click()
    time.sleep(0.5)

    drag = page.evaluate(DRAG)
    result["drag_ms_per_frame"] = round(drag, 2)
    check("drag frame (block move)", drag, args.budget_drag_ms, "ms")

    typing = page.evaluate(TYPING)
    if typing is not None:
        result["typing_ms_per_keystroke"] = round(typing, 3)
        check("keystroke in editor", typing, 2.0, "ms")

    if errors:
        failures.append(f"console errors: {errors[:3]}")
        print(f"  FAIL console errors: {errors[:3]}")
    browser.close()

if args.json:
    args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n{'PASS' if not failures else 'FAIL'} ({len(failures)} budget violations)")
for item in failures:
    print(f"  - {item}")
sys.exit(1 if failures else 0)
