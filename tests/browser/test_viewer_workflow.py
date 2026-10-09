"""Real-browser regression tests over synthetic documents; no OCR service or private data."""
from __future__ import annotations

import json
import os
import runpy
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser_page(tmp_path):
    from ocr_agent_viewer.server import Handler, OCRAgentCatalog

    root = Path(__file__).parents[2]
    runpy.run_path(str(root / "examples/make_viewer_demo.py"))["create_demo"](tmp_path)
    Handler.catalog = OCRAgentCatalog(tmp_path)
    Handler._batch_catalogs = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("FOLIOSCRIBE_BROWSER") or None)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.wait_for_function("state.detail !== null && !state.navigating")
        page.locator('[data-mode="annotate"]').click()
        page.wait_for_function("ann.blocks.length === 3")
        yield page, tmp_path
        assert errors == []
        browser.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def edit_text(page, value):
    page.evaluate("value => applyLiveEdit(ann.blocks[1].block_id, {text: value}, 'text')", value)


def test_failed_save_blocks_navigation_and_retry_keeps_undo(browser_page):
    page, root = browser_page
    def fail_save(route):
        if route.request.method == "POST":
            route.fulfill(status=503, content_type="application/json", body='{"error":"offline test"}')
        else:
            route.continue_()
    page.route("**/api/annotations/**", fail_save)
    edit_text(page, "Keep my correction")
    page.locator("#nextPageBtn").click()
    page.locator("#saveFeedback").wait_for(state="visible")
    assert page.evaluate("state.detail.page") == 1
    assert page.evaluate("ann.blocks[1].text") == "Keep my correction"
    with page.expect_download() as download:
        page.locator("#exportDraft").click()
    payload = json.loads(Path(download.value.path()).read_text())
    assert payload["blocks"][1]["text"] == "Keep my correction"
    page.unroute("**/api/annotations/**", fail_save)
    page.locator("#retrySave").click()
    page.wait_for_function("!ann.dirty && !ann.saving")
    assert page.locator("#undoBtn").is_enabled()
    saved = json.loads((root / "annotations/demo_guide/page_0001.json").read_text())
    assert saved["blocks"][1]["text"] == "Keep my correction"
    page.locator("#undoBtn").click()
    assert page.evaluate("ann.blocks[1].text") != "Keep my correction"
    page.locator("#redoBtn").click()
    assert page.evaluate("ann.blocks[1].text") == "Keep my correction"
    page.locator("#nextPageBtn").click()
    page.wait_for_function("state.detail.page === 2 && !state.navigating")


def test_inflight_edit_is_saved_before_navigation(browser_page):
    page, root = browser_page
    posts = []
    def intercept(route):
        if route.request.method == "POST":
            posts.append(route.request.post_data_json)
            if len(posts) == 1:
                edit_text(page, "Newer edit while saving")
        route.continue_()
    page.route("**/api/annotations/**", intercept)
    edit_text(page, "First snapshot")
    page.locator("#nextPageBtn").click()
    page.wait_for_function("state.detail.page === 2 && !state.navigating")
    assert len(posts) == 2
    assert posts[0]["blocks"][1]["text"] == "First snapshot"
    saved = json.loads((root / "annotations/demo_guide/page_0001.json").read_text())
    assert saved["blocks"][1]["text"] == "Newer edit while saving"


def test_jump_pending_queue_scoping_and_boundaries(browser_page):
    page, _ = browser_page
    assert page.locator("#prevPageBtn").is_disabled()
    page.locator("#pageJump").fill("3")
    page.locator("#pageJump").press("Enter")
    page.wait_for_function("state.detail.page === 3 && !state.navigating")
    page.locator("#nextPending").click()
    page.wait_for_function("state.detail.sample_id === 'demo_notes' && !state.navigating")
    page.locator("#pageJump").fill("3")
    page.locator("#jumpPageBtn").click()
    page.wait_for_function("state.detail.page === 3 && !state.navigating")
    assert page.locator("#nextPageBtn").is_disabled()
    page.evaluate("localStorage.setItem('ocrAgentQueueKey', 'test-queue-key')")
    page.reload()
    page.wait_for_function("state.detail !== null && !state.navigating")
    url = page.evaluate("withQueueKey('/api/annotations?batch=demo&t=123456')[0]")
    assert "t=test-queue-key" in url
    assert "t=123456" not in url
