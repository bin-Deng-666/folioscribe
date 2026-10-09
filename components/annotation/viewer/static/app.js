"use strict";

const state = {
  queue: null,                 // this annotator's assignment (never displayed as a person)
  queueSet: new Set(),
  imageUrls: new Map(),
  pageFlags: new Map(),
  catalog: null,
  batch: "",
  pages: [],
  detail: null,
  selectedKey: null,
  expandedDocuments: new Set(),
  layer: "final",
  zoom: 0.45,
  naturalWidth: 0,
  naturalHeight: 0,
  mode: "browse",
  annotated: new Set(),
  navigating: false,
};

async function loadAnnotationIndex() {
  try {
    const summary = await requestJson(`/api/annotations?batch=${encodeURIComponent(state.batch)}&t=${Date.now()}`);
    // Merge, never replace: a shared server means someone else's save can arrive at any moment, and a
    // refresh that drops what this annotator just saved looked like the app discarding their work.
    for (const row of summary.pages || []) state.annotated.add(`${row.sample_id}:${row.page}`);
    for (const [sampleId, record] of Object.entries(summary.document_status || {})) {
      ann.docStatus[sampleId] = record.status;
    }
  } catch (error) {
    // A failed refresh must never erase what the annotator already did: the "已标注" tags come from
    // this index, so clearing it on a transient error looked like the app discarding their work.
    console.warn("annotation index refresh failed, keeping the previous state", error);
  }
}

// Annotation mode keeps its own working copy of the page blocks. Nothing here is written to runs/:
// every edit is posted to /api/annotations and stored next to the batch as an auditable patch.
const ann = {
  blocks: [], deleted: new Map(), ops: [], undo: [], redo: [],
  selectedId: null, drawing: null, drag: null, snapPreview: null,
  quickType: "text", snap: true, dirty: false, saving: false, exists: false,
  savePromise: null, saveError: "", editVersion: 0,
  revision: 0, updatedAt: null, stale: false, annotator: "", serial: 0, autosaveTimer: null,
 showFlow: true, dragKey: null, dropIndex: null,
  orderPick: null,   // {blockId, hoverId} while picking the predecessor of a block
  createMode: false, // armed by B / 新建框: the canvas dims and a drag defines a draft box
  draft: null,       // {box, refs} awaiting ✓ / ✗
  savedAt: null, savingTimer: null, batch: {annotated: 0, total: 0},
  linkPick: null,      // {origin:{sampleId,page,blockId}, direction:"prev"|"next"}
  pendingLink: null,   // applied after navigating back to the origin page
  docStatus: {},       // sample_id -> PASS/FAIL recorded at the document boundary
  cardOpen: new Set(["content"]),   // which inline-editor sections are expanded
  cardPinned: null,                 // {left, top} when the annotator dragged the card out of the way
  multi: [],                        // block ids selected together (Ctrl+click / Shift+drag) for merging
  band: null,                       // rubber-band rectangle while dragging to multi-select
  flag: null,                       // "too_hard" when the annotator flagged this page
};

const COLORS = {
  // Curated so the 14 block types stay distinguishable on a busy page image: content types take
  // saturated hues, furniture types take desaturated tones, unknown stays neutral. Minimum pairwise
  // distance is CIEDE2000-ish ΔE 20.9 with a hue gap of 17.6° (tests/test_viewer.py locks this).
  title: "#dc2626", text: "#1d4ed8", table: "#15803d", image: "#7c3aed",
  chart: "#ea580c", formula: "#0e7490", seal: "#9f1239",
  table_caption: "#0f766e", image_caption: "#c026d3",
  header: "#7c5c43", footer: "#475569", page_number: "#4d7c0f", footnote: "#a16207",
  unknown: "#8b95a6", default: "#8b95a6",
  pdf_line: "#0284c7", pdf_image: "#d97706",
  heading: "#dc2626", body: "#1d4ed8", caption: "#0f766e", figure: "#7c3aed",
  equation: "#0e7490", contract: "#a16207",
};

const $ = (id) => document.getElementById(id);
const els = {
  pageIdentity: $("pageIdentity"), machineStatus: $("machineStatus"), reviewActions: $("reviewActions"),
  approveBtn: $("approveBtn"), rejectBtn: $("rejectBtn"),
  searchInput: $("searchInput"), statusFilter: $("statusFilter"),
  catalogSummary: $("catalogSummary"), batchFilter: $("batchFilter"), pageList: $("pageList"), layerControl: $("layerControl"),
  overlayImageToggle: $("overlayImageToggle"), labelToggle: $("labelToggle"),
  zoomOut: $("zoomOut"), zoomIn: $("zoomIn"), zoomFit: $("zoomFit"), zoomValue: $("zoomValue"),
  canvasScroll: $("canvasScroll"), emptyState: $("emptyState"), pageFrame: $("pageFrame"),
  pageImage: $("pageImage"), overlay: $("overlay"), legend: $("legend"), pageSummary: $("pageSummary"),
  tabs: $("tabs"), issuesPanel: $("issuesPanel"), tasksPanel: $("tasksPanel"),
  blockSearch: $("blockSearch"), blockList: $("blockList"), detail: $("detail"),
  reviewNote: $("reviewNote"), reviewNoteInput: $("reviewNoteInput"), reviewUpdated: $("reviewUpdated"),
  modeSwitch: $("modeSwitch"), annotateBar: $("annotateBar"), annotateHint: $("annotateHint"),
  undoBtn: $("undoBtn"), redoBtn: $("redoBtn"), saveAnnotations: $("saveAnnotations"),
  newBoxBtn: $("newBoxBtn"), deleteBoxBtn: $("deleteBoxBtn"), snapToggle: $("snapToggle"),
flowToggle: $("flowToggle"), orderPickBtn: $("orderPickBtn"),
  prevPageBtn: $("prevPageBtn"), nextPageBtn: $("nextPageBtn"), pageStepperLabel: $("pageStepperLabel"),
  batchProgress: $("batchProgress"),
  dirtyChip: $("dirtyChip"), annotatorName: $("annotatorName"), clearAnnotations: $("clearAnnotations"),
  annotationPanel: $("annotationPanel"), textPanel: $("textPanel"), toast: $("toast"),
  pickBanner: $("pickBanner"), pickBannerTitle: $("pickBannerTitle"), pickBannerText: $("pickBannerText"), pickCancel: $("pickCancel"),
  docDialog: $("docDialog"), docDialogSample: $("docDialogSample"), docDialogProgress: $("docDialogProgress"),
  docDialogHint: $("docDialogHint"), docDialogNote: $("docDialogNote"), docDialogPass: $("docDialogPass"),
  docDialogFail: $("docDialogFail"), docDialogLater: $("docDialogLater"),
  draftActions: $("draftActions"), draftSize: $("draftSize"), draftConfirm: $("draftConfirm"), draftCancel: $("draftCancel"),
  blockPopover: $("blockPopover"),
  resetDialog: $("resetDialog"), resetDialogWhere: $("resetDialogWhere"), resetDialogHint: $("resetDialogHint"),
  resetDialogCancel: $("resetDialogCancel"), resetDialogConfirm: $("resetDialogConfirm"),
  annotateMenu: $("annotateMenu"), annotateMenuBtn: $("annotateMenuBtn"),
  hardFlagBtn: $("hardFlagBtn"), flowToggleBrowse: $("flowToggleBrowse"),
};

const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;"}[char]));
const badge = (value) => `<span class="badge ${esc(value)}">${esc(value)}</span>`;
const colorOf = (type) => COLORS[type] || COLORS.default;
const colorAlpha = (type, alpha) => {
  const hex = colorOf(type).replace("#", "");
  const value = Number.parseInt(hex, 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
};
const shortText = (value, limit = 100) => {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
};
const shortBlockId = (value) => String(value ?? "").split(".").pop();
/** Visual mark for a block that continues across pages.
 *  A second, offset dashed ring plus a corner tag, coloured by kind (table vs running text) so the
 *  two cases are told apart at a glance instead of blending into the normal type-coloured outline. */
function crossPageMarkup(item, box, unit, pageWidth = 0, pageHeight = 0) {
  const text = item.cross_page_text || {};
  const table = item.cross_page_table || {};
  const isTable = item.type === "table" || Object.keys(table).length > 0;
  const continues = Boolean(text.continues_block_id || table.continues_block_id);
  const continuedBy = Boolean(text.continued_by_block_id || table.continued_by_block_id);
  const parts = Number(table.part_count);
  if (!continues && !continuedBy && !(Number.isFinite(parts) && parts > 1)) return "";
  const kind = isTable ? "table" : "text";
  const arrows = [];
  if (continues) arrows.push("↑承接上页");
  if (continuedBy) arrows.push("↓下页承接");
  const label = isTable ? `续表${Number.isFinite(parts) ? ` ${table.part_index || "?"}/${parts}` : ""}` : "跨页段落";
  const tag = `${label} ${arrows.join(" ")}`.trim();
  const [x1, y1, x2, y2] = box;
  const pad = 5 * unit;
  const fontSize = 11 * unit;
  const tagHeight = 16 * unit;
  const padX = 6 * unit;
  const tagWidth = tag.length * fontSize * 0.58 + padX * 2;
  // Tag placement: above the ring when there is room, otherwise tucked inside the top-left corner.
  // Horizontally it follows the block but is clamped so it cannot run off the page.
  const above = y1 - pad - tagHeight - 1 * unit;
  const tagTop = above >= 0 ? above : y1 + pad + 1 * unit;
  const rawLeft = x1 - pad;
  const maxLeft = pageWidth > 0 ? Math.max(0, pageWidth - tagWidth) : rawLeft;
  const tagLeft = Math.max(0, Math.min(rawLeft, maxLeft));
  return `<g class="overlay-cross-group" pointer-events="none">
    <rect class="overlay-cross-casing" x="${x1 - pad}" y="${y1 - pad}" width="${Math.max(0, x2 - x1) + pad * 2}" height="${Math.max(0, y2 - y1) + pad * 2}" rx="${4 * unit}"/>
    <rect class="overlay-cross-ring ${kind}" x="${x1 - pad}" y="${y1 - pad}" width="${Math.max(0, x2 - x1) + pad * 2}" height="${Math.max(0, y2 - y1) + pad * 2}" rx="${4 * unit}"/>
    <rect class="overlay-cross-tag ${kind}" x="${tagLeft}" y="${tagTop}" width="${tagWidth}" height="${tagHeight}" rx="${3 * unit}"/>
    <text class="overlay-cross-tag-text" x="${tagLeft + padX}" y="${tagTop + tagHeight * 0.72}" font-size="${fontSize}">${esc(tag)}</text>
  </g>`;
}

const crossPageTags = (block) => {
  const text = block.cross_page_text || {};
  const table = block.cross_page_table || {};
  const tags = [];
  if (block.repeated_page_header) tags.push("页眉表");
  if (text.continues_block_id) tags.push(`⇡续 ${shortBlockId(text.continues_block_id)}`);
  if (text.continued_by_block_id) tags.push(`⇣接 ${shortBlockId(text.continued_by_block_id)}`);
  if (Number.isFinite(table.part_count) && table.part_count > 1) tags.push(`续表 ${table.part_index || "?"}/${table.part_count}`);
  if (table.truncated_at_page_end) tags.push("页底截断");
  if (table.truncated_at_page_start) tags.push("页顶续接");
  return tags;
};

/* ----------------------------------------------------------------------------------------------
 * Annotation queue key
 *
 * Several annotators share one batch, so each person is handed a link carrying `?t=<key>`. The key
 * selects that person's workload and scopes their patches on the server. The viewer never displays
 * it, never names the person, and never reveals that some documents are annotated twice (blind QC):
 * every document simply looks like any other document.
 * ---------------------------------------------------------------------------------------------- */
const queueKey = (() => {
  try {
    const fromUrl = new URLSearchParams(location.search).get("t");
    if (fromUrl) {
      localStorage.setItem("ocrAgentQueueKey", fromUrl);
      return fromUrl;
    }
    return localStorage.getItem("ocrAgentQueueKey") || "";
  } catch (_) {
    return "";
  }
})();

/** Endpoints whose answer depends on *which batch* and *who* is asking.
 *
 *  Both travel with the request: one server serves every annotator and every open tab, so a save must
 *  be scoped to the batch the client is looking at instead of whatever batch another client selected. */
const QUEUE_SCOPED_PATHS = ["/api/queue", "/api/page/", "/api/image/", "/api/annotations", "/api/page-flags", "/api/document-annotations", "/api/status"];

function withQueueKey(url, options) {
  if (!QUEUE_SCOPED_PATHS.some((prefix) => url.startsWith(prefix))) return [url, options];
  const scoped = new URL(url, location.origin);
  if (state.batch) scoped.searchParams.set("batch", state.batch);
  // `t` is the queue key, never a cache-busting timestamp. Replace any old value.
  scoped.searchParams.delete("t");
  if (queueKey) scoped.searchParams.set("t", queueKey);
  let nextOptions = options;
  if (queueKey && options && typeof options.body === "string") {
    try {
      const payload = JSON.parse(options.body);
      if (payload && typeof payload === "object" && !Array.isArray(payload)) {
        payload.queue_key = queueKey;
        nextOptions = { ...options, body: JSON.stringify(payload) };
      }
    } catch (_) {
      /* not JSON: the query parameters are enough for the server to scope the write */
    }
  }
  return [scoped.pathname + scoped.search, nextOptions];
}

async function requestJson(url, options) {
  const [requestUrl, requestOptions] = withQueueKey(url, options);
  const response = await fetch(requestUrl, {...requestOptions, cache: "no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

/** The annotator's own document list (ids and counts only - the server never sends an identity). */
async function loadQueue(batch) {
  state.queue = null;
  state.queueSet = new Set();
  if (!queueKey) return;
  try {
    const payload = await requestJson(`/api/queue?batch=${encodeURIComponent(batch)}`);
    if (payload && payload.exists) {
      state.queue = payload;
      state.queueSet = new Set(payload.sample_ids || []);
    }
  } catch (_) {
    state.queue = null;
  }
}

async function loadCatalog(batch = new URLSearchParams(location.search).get("batch") || "") {
  state.catalog = await requestJson(`/api/catalog?batch=${encodeURIComponent(batch)}&t=${Date.now()}`);
  state.batch = state.catalog.batch || batch;
  state.pages = state.catalog.documents.flatMap((doc) => doc.pages);
  await loadQueue(state.batch);
  await loadAnnotationIndex();
  renderBatchFilter();
  renderCatalog();
  const query = new URLSearchParams(location.search);
  const wantedSample = query.get("sample");
  const wantedPage = Number(query.get("page"));
  const first = state.pages.find((item) => item.sample_id === wantedSample && item.page === wantedPage) || state.pages[0];
  if (first) await selectPage(first.sample_id, first.page);
}

function renderBatchFilter() {
  els.batchFilter.innerHTML = (state.catalog?.batches || []).map((batch) => `<option value="${esc(batch.name)}">${esc(batch.name)}</option>`).join("");
  els.batchFilter.value = state.batch;
}

async function switchBatch(batch) {
  if (!batch || batch === state.batch || state.navigating) return;
  if (!(await flushAnnotations())) {
    els.batchFilter.value = state.batch;
    return;
  }
  state.annotated.clear();
  state.pageFlags.clear();
  ann.docStatus = {};
  state.expandedDocuments.clear();
  state.detail = null;
  await loadCatalog(batch);
}


function navigatePage(delta) {
  if (!state.catalog || !state.detail) return;
  const documents = state.catalog.documents.filter((document) => document.pages?.length);
  const documentIndex = documents.findIndex((document) => document.sample_id === state.detail.sample_id);
  if (documentIndex < 0) return;
  const document = documents[documentIndex];
  const pageIndex = document.pages.findIndex((item) => item.page === state.detail.page);
  if (pageIndex < 0) return;
  let targetDocumentIndex = documentIndex;
  let targetPageIndex = pageIndex + delta;
  const leavesDocument = targetPageIndex >= document.pages.length;
  if (targetPageIndex < 0) {
    targetDocumentIndex = documentIndex - 1;
    if (targetDocumentIndex < 0 || ann.linkPick) return;
    targetPageIndex = documents[targetDocumentIndex].pages.length - 1;
  } else if (leavesDocument) {
    targetDocumentIndex = documentIndex + 1;
    if (targetDocumentIndex >= documents.length || ann.linkPick) return;
    targetPageIndex = 0;
  }
  const target = documents[targetDocumentIndex].pages[targetPageIndex];
  if (!target) return;
  // Finishing a document is a checkpoint: ask whether the annotation of this document succeeded.
  if (leavesDocument && state.mode === "annotate" && !ann.docStatus[document.sample_id]) {
    flushAnnotations().then((saved) => {
      if (saved) openDocumentDialog(() => selectPage(target.sample_id, target.page));
    });
    return;
  }
  selectPage(target.sample_id, target.page);
}

function navigateDocument(delta) {
  if (!state.catalog || !state.detail) return;
  const index = state.catalog.documents.findIndex((document) => document.sample_id === state.detail.sample_id);
  const target = state.catalog.documents[index + delta];
  if (target?.pages?.length) selectPage(target.sample_id, target.pages[0].page);
}

/** Only the selection changed: touch two elements instead of rebuilding every document row. */
function updateCatalogSelection() {
  const list = els.pageList;
  list.querySelectorAll(".document-item.active, .page-item.active").forEach((element) => element.classList.remove("active"));
  const sampleId = state.detail?.sample_id;
  if (!sampleId) return;
  const documentNode = list.querySelector(`.document-item[data-document="${cssEscapeId(sampleId)}"]`);
  if (documentNode) documentNode.classList.add("active");
  const pageNode = list.querySelector(`.page-item[data-sample="${cssEscapeId(sampleId)}"][data-page="${state.detail.page}"]`);
  if (pageNode) pageNode.classList.add("active");
}

/** The annotation dot of one page appears the moment that page is saved. */
function markCatalogAnnotated(sampleId, page) {
  const pageNode = els.pageList.querySelector(`.page-item[data-sample="${cssEscapeId(sampleId)}"][data-page="${page}"] .page-number`);
  if (pageNode && !pageNode.querySelector(".annotation-dot")) pageNode.insertAdjacentHTML("beforeend", '<span class="annotation-dot" title="已有人工标注">●</span>');
}

function isPageAnnotated(item) {
  return Boolean(state.annotated?.has(`${item.sample_id}:${item.page}`));
}

/** The navigator answers a different question per mode: pipeline verdict (browse) vs annotation state. */
function renderCatalogFilters() {
  const annotating = state.mode === "annotate";
  const mine = annotating && state.queue ? [["mine", "我的任务"]] : [];
  const options = annotating
    ? [...mine, ["", "全部标注状态"], ["done", "已标注"], ["todo", "未标注"]]
    : [["", "全部状态"], ["PASS", "PASS"], ["CHECK", "CHECK"], ["FAIL", "FAIL"]];
  const previous = els.statusFilter.value;
  els.statusFilter.innerHTML = options.map(([value, label]) => `<option value="${value}">${esc(label)}</option>`).join("");
  // An annotator with a queue starts (and stays) on their own documents: "" is a valid option, so the
  // default has to be decided before falling back to "previous still exists".
  const preferMine = annotating && state.queue && (previous === "" || previous === "mine");
  els.statusFilter.value = preferMine ? "mine" : (options.some(([value]) => value === previous) ? previous : "");
}

function renderCatalog() {
  const query = els.searchInput.value.trim().toLowerCase();
  const machine = els.statusFilter.value;
  const annotating = state.mode === "annotate";
  const queryMatches = (document) => {
    const text = `${document.sample_id} ${document.pages.map((item) => item.page).join(" ")}`.toLowerCase();
    if (query && !text.includes(query)) return false;
    if (machine === "mine") return state.queueSet.has(document.sample_id);
    if (!machine) return true;
    if (annotating) return document.pages.some((item) => isPageAnnotated(item) === (machine === "done"));
    return document.pages.some((item) => item.status === machine);
  };
  const counts = state.pages.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
  // "Which document am I on?" is asked constantly while annotating a 100+ document part, so the
  // position is part of the summary line (it follows the same filter as the list below it).
  const visibleDocuments = state.catalog.documents.filter(queryMatches);
  const currentDocumentIndex = visibleDocuments.findIndex((doc) => doc.sample_id === state.detail?.sample_id);
  const documentPosition = currentDocumentIndex >= 0
    ? `第 ${currentDocumentIndex + 1} / ${visibleDocuments.length} 篇 · `
    : `共 ${visibleDocuments.length} 篇 · `;
  if (annotating) {
    const donePages = state.pages.filter(isPageAnnotated).length;
    const verdicts = Object.values(ann.docStatus || {});
    const mineDocuments = state.queue ? state.catalog.documents.filter((doc) => state.queueSet.has(doc.sample_id)) : [];
    const mineDone = mineDocuments.filter((doc) => doc.pages.length && doc.pages.every(isPageAnnotated)).length;
    const minePart = state.queue ? `我的任务 ${mineDone}/${mineDocuments.length} 篇 · ` : "";
    els.catalogSummary.textContent = `${documentPosition}${minePart}已标注 ${donePages} 页 · 通过 ${verdicts.filter((v) => v === "PASS").length} 文档`;
  } else {
    els.catalogSummary.textContent = `${documentPosition}${state.pages.length} 页 · PASS ${counts.PASS || 0} · CHECK ${counts.CHECK || 0} · FAIL ${counts.FAIL || 0}`;
  }
  els.pageList.innerHTML = state.catalog.documents.filter(queryMatches).map((document) => {
    const active = state.detail?.sample_id === document.sample_id;
    const expanded = state.expandedDocuments.has(document.sample_id);
    const documentCounts = document.status_counts || {};
    const visiblePages = document.pages.filter((item) => {
      if (machine === "mine") return true;
      if (!machine) return true;
      return annotating ? isPageAnnotated(item) === (machine === "done") : item.status === machine;
    });
    const donePages = document.pages.filter(isPageAnnotated).length;
    const docState = ann.docStatus?.[document.sample_id];
    const docBadge = docState ? `<span class="doc-badge ${docState === "PASS" ? "ok" : "bad"}" title="本文档标注结论">${docState === "PASS" ? "✓" : "✗"}</span>` : "";
    const pages = expanded ? visiblePages.map((item) => {
      const pageActive = active && state.detail?.page === item.page;
      const annotated = isPageAnnotated(item);
      const hardFlag = state.pageFlags?.has(`${item.sample_id}:${item.page}`) ? '<span class="hard-flag" title="已标记：问题太大、标注难度大">⚠</span>' : "";
      const marker = annotating
        ? `<span class="ann-chip ${annotated ? "done" : ""}" title="${annotated ? "本页已有人工标注" : "本页还没有标注"}">${annotated ? "● 已标注" : "未标注"}</span>`
        : badge(item.status);
      return `<button type="button" class="page-item ${pageActive ? "active" : ""}" data-sample="${esc(item.sample_id)}" data-page="${item.page}">
        <span class="page-number">P${item.page}${hardFlag}</span>
        <span>${annotating ? "" : badge(item.status)}<span class="page-meta">${item.block_count} blocks${annotating ? "" : ` · ${item.unresolved_field_count} unresolved`}</span></span>
        ${marker}
      </button>`;
    }).join("") : "";
    const subtitle = annotating
      ? `${document.page_count} 页 · 已标注 ${donePages}/${document.page_count}`
      : `${document.page_count} 页 · PASS ${documentCounts.PASS || 0} · CHECK ${documentCounts.CHECK || 0}`;
    const rightBadge = annotating
      ? (docState ? docBadge : `<span class="ann-chip ${donePages ? "done" : ""}">${donePages}/${document.page_count}</span>`)
      : (documentCounts.FAIL ? badge("FAIL") : documentCounts.CHECK ? badge("CHECK") : badge("PASS"));
    return `<section class="document-group ${expanded ? "expanded" : ""}">
      <button type="button" class="document-item ${active ? "active" : ""}" data-document="${esc(document.sample_id)}" aria-expanded="${expanded}">
        <span class="document-chevron" aria-hidden="true">${expanded ? "▾" : "▸"}</span>
        <span class="document-main"><strong>${esc(document.sample_id)}${annotating ? docBadge : ""}</strong><span>${subtitle}</span></span>
        ${rightBadge}
      </button>
      <div class="document-pages">${pages}</div>
    </section>`;
  }).join("") || '<div class="empty-state">没有符合筛选条件的文档</div>';
  state.renderedExpanded = new Set(state.expandedDocuments);
  state.renderedQuery = `${query}|${machine}|${annotating}`;
}

async function selectPage(sampleId, page) {
  if (state.navigating) return;
  state.navigating = true;
  try {
    if (!(await flushAnnotations())) return;
    await loadSelectedPage(sampleId, page);
  } catch (error) {
    toast(`加载失败：${error.message}`, true);
  } finally {
    state.navigating = false;
  }
}

async function loadSelectedPage(sampleId, page) {
  const detail = await requestJson(`/api/page/${encodeURIComponent(sampleId)}/${page}?_=${Date.now()}`);
  const patch = state.mode === "annotate"
    ? await requestJson(`/api/annotations/${encodeURIComponent(sampleId)}/${page}?_=${Date.now()}`) : null;
  ann.orderPick = null;
  updateModeBanner();
  state.selectedKey = null;
  ann.selectedId = null;
  state.expandedDocuments.add(sampleId);
  state.detail = detail;
  history.replaceState(null, "", `?batch=${encodeURIComponent(state.batch)}&sample=${encodeURIComponent(sampleId)}&page=${page}`);
  els.pageIdentity.textContent = `${sampleId} · Page ${page}`;
  els.reviewActions.hidden = false;
  els.reviewActions.hidden = state.mode === "annotate";
  if (state.mode === "annotate") {
    applyPatch(patch);
    if (ann.pendingLink && ann.pendingLink.origin.sampleId === sampleId && ann.pendingLink.origin.page === page) {
      const pick = ann.pendingLink;
      ann.pendingLink = null;
      const fieldName = pick.direction === "prev" ? "continues_block_id" : "continued_by_block_id";
      const originBlock = ann.blocks.find((block) => block.block_id === pick.origin.blockId);
      if (originBlock) {
        const scope = originBlock.type === "table" ? "cross_page_table" : "cross_page_text";
        updateBlockFields(pick.origin.blockId, {
          [scope]: {...(originBlock[scope] || {}), [fieldName]: pick.targetBlockId, status: "PASS", method: "manual_annotation_link"},
        }, "update");
        selectAnnotationBlock(pick.origin.blockId);
        flushAnnotations().then((saved) => {
          if (saved) writeReciprocalLink(pick, pick.targetBlockId, pick.origin.blockId);
        });
      }
    }
  } else {
    resetAnnotationState();
  }
  loadPageFlag(sampleId, page);
  els.reviewNote.hidden = false;
  els.reviewNoteInput.value = state.detail.review.note || "";
  els.reviewUpdated.textContent = state.detail.review.updated_at ? `更新：${state.detail.review.updated_at}` : "尚未人工改判";
  renderReviewStatus();
  // The first load renders the catalog before this page is known, so the document may still be
  // collapsed in the DOM; rebuild only in that case, otherwise just move the selection.
  if (state.renderedExpanded && [...state.expandedDocuments].every((id) => state.renderedExpanded.has(id))) {
    updateCatalogSelection();
  } else {
    renderCatalog();
  }
  renderPageSummary();
  renderCurrentLayer();
  renderIssues();
  renderTasks();
  els.emptyState.hidden = true;
  els.pageFrame.hidden = false;
  updatePageStepper();
  renderBatchProgress();
  updateImage();
}

async function loadPageFlag(sampleId, page) {
  try {
    const record = await requestJson(`/api/page-flags/${encodeURIComponent(sampleId)}/${page}?t=${Date.now()}`);
    const previous = state.pageFlags.get(`${sampleId}:${page}`) || null;
    if (record.flag) state.pageFlags.set(`${sampleId}:${page}`, record.flag);
    else state.pageFlags.delete(`${sampleId}:${page}`);
    updateHardFlagButton();
    if (Boolean(previous) !== Boolean(record.flag)) renderCatalog();
  } catch (error) {
    updateHardFlagButton();
  }
}

function updateHardFlagButton() {
  if (!els.hardFlagBtn) return;
  const flagged = Boolean(state.detail && state.pageFlags.get(`${state.detail.sample_id}:${state.detail.page}`));
  els.hardFlagBtn.classList.toggle("active", flagged);
  els.hardFlagBtn.textContent = flagged ? "⚠ 难度过大（已标记）" : "标注难度过大";
  els.hardFlagBtn.title = flagged ? "点一下取消该标记" : "这一页太难、无法可靠标注：标记后目录里会显示 ⚠，便于后续复核或跳过";
}

async function toggleHardFlag() {
  if (!state.detail) return;
  const sampleId = state.detail.sample_id;
  const page = state.detail.page;
  const key = `${sampleId}:${page}`;
  const flagged = state.pageFlags.has(key);
  try {
    const record = await requestJson(`/api/page-flags/${encodeURIComponent(sampleId)}/${page}`, {
      method: flagged ? "DELETE" : "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({flag: flagged ? null : "too_hard", annotator: ann.annotator}),
    });
    if (record.flag) state.pageFlags.set(key, record.flag);
    else state.pageFlags.delete(key);
    updateHardFlagButton();
    renderCatalog();
    renderAnnotationPanel();
    toast(record.flag ? "已标记「标注难度过大」，目录里会显示 ⚠" : "已取消「难度过大」标记");
  } catch (error) {
    toast(`标记失败：${error.message}`, true);
  }
}

function renderReviewStatus() {
  const status = state.detail.status;
  els.machineStatus.innerHTML = badge(status);
  els.approveBtn.disabled = status !== "CHECK";
  els.rejectBtn.disabled = status !== "CHECK";
  els.approveBtn.classList.toggle("active", status === "PASS");
  els.rejectBtn.classList.toggle("active", status === "FAIL");
}

async function saveReview(status) {
  if (!state.detail) return;
  try {
    const review = await requestJson(`/api/status/${encodeURIComponent(state.detail.sample_id)}/${state.detail.page}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status, note: els.reviewNoteInput.value}),
    });
    state.detail.review = review;
    state.detail.status = review.status;
    const catalogPage = state.pages.find((item) => item.sample_id === state.detail.sample_id && item.page === state.detail.page);
    if (catalogPage) catalogPage.status = review.status;
    els.reviewUpdated.textContent = `更新：${review.updated_at}`;
    renderReviewStatus();
    renderCatalog();
  } catch (error) {
    toast(`保存失败：${error.message}`, true);
  }
}

function renderPageSummary() {
  const validation = state.detail.validation;
  const annotating = state.mode === "annotate";
  const annotated = state.annotated?.has(`${state.detail.sample_id}:${state.detail.page}`);
  const headline = annotating
    ? `<span class="ann-chip ${annotated ? "done" : ""}">${annotated ? "● 本页已标注" : "本页未标注"}</span>${ann.dirty ? ' <span class="ann-chip">未保存改动</span>' : ""}`
    : badge(state.detail.status);
  els.pageSummary.innerHTML = `
    <div class="summary-line">${headline}</div>
    <div class="summary-metrics">
      <div class="metric"><strong>${state.detail.blocks.length}</strong><span>blocks</span></div>
      <div class="metric"><strong>${validation.claimed_required_evidence ?? 0}/${validation.required_evidence ?? 0}</strong><span>PDF coverage</span></div>
      <div class="metric"><strong>${validation.unresolved_field_count ?? 0}</strong><span>unresolved</span></div>
    </div>`;
}

function layerItems() {
  if (!state.detail) return [];
  if (state.mode === "annotate") return annotationItems();
  if (state.layer === "final") {
    return state.detail.blocks.map((block, index) => ({...block, _key: `final:${index}`, _index: index, _type: block.type, _text: block.text || block.html || ""}));
  }
  if (state.layer === "contract") {
    // Contract GT layer: one item per projected entity (same geometry as the revision block, so the
    // canvas highlight and the block list stay comparable across layers).
    return state.detail.blocks
      .filter((block) => block.contract_gt)
      .map((block, index) => {
        const contract = block.contract_gt;
        const heading = contract.heading || {};
        const role = contract.role || "body";
        const badge = heading.heading_id ? `${role} · L${heading.level_absolute ?? "-"}` : role;
        return {
          ...block,
          _key: `contract:${index}`,
          _index: index,
          _type: role,
          _text: `${contract.entity_id || ""} → ${contract.node_id || ""}${badge !== role ? ` · ${badge}` : ""}`,
        };
      });
  }
  return (state.detail.evidence[state.layer] || [])
    .filter((item) => Array.isArray(item.bbox_image_px))
    .map((item, index) => ({
      ...item, _key: `${state.layer}:${index}`, _index: index,
      _type: item.type_candidate || item.source_type || item.kind || "unknown",
      _text: item.text || item.html || "",
    }));
}

function renderCurrentLayer() {
  renderOverlay();
  renderBlockPopover(true);
  renderLegend();
  renderBlockList();
  renderDetail();
  renderAnnotationPanel();
  renderTextPanel();
}

/** A solid type chip pinned to the top-left corner of a box.
 *  Sized in user units divided by the current zoom so it keeps a constant on-screen size, and it
 *  carries the title level because "which heading level is this" is the error annotators look for. */
function overlayChip({x1, x2, y1, text, color, unit, state: editState = "", selected = false, pageWidth = 0, pageHeight = 0}) {
  const fontSize = 10.5 * unit * (selected ? 1.06 : 1);
  const padX = 5 * unit;
  const height = 15 * unit;
  const stateTag = editState && editState !== "chain" ? ` +${editState}` : "";
  const label = `${text}${stateTag}`;
  const width = label.length * fontSize * 0.62 + padX * 2;
  const gap = 3 * unit;
  // Beside the box, not on top of it: the chip starts at the right edge and grows downward from the
  // box's top. When the page edge is too close it flips to the inside of the top-left corner.
  // Placement order: beside the box on the right, then on its left, then inside the top-left corner.
  // Vertically it is clamped to the page so a box at the very bottom cannot push the chip off-page.
  const fitsRight = pageWidth > 0 && x2 + gap + width <= pageWidth;
  const fitsLeft = x1 - gap - width >= 0;
  const left = fitsRight ? x2 + gap : fitsLeft ? x1 - gap - width : x1 + gap;
  const rawTop = y1 + gap * 0.5;
  const top = pageHeight > 0 ? Math.min(Math.max(rawTop, gap * 0.5), Math.max(gap * 0.5, pageHeight - height - gap)) : rawTop;
  return `<g class="overlay-chip-group" pointer-events="none">
    <rect class="overlay-chip-bg" x="${left}" y="${top}" width="${width}" height="${height}" rx="${3.5 * unit}" fill="${color}"/>
    <text class="overlay-chip-text" x="${left + padX}" y="${top + height * 0.72}" fill="#fff" font-size="${fontSize}" font-weight="700">${esc(label)}</text>
  </g>`;
}

/** Reading-order polyline: white casing, coloured dashes, arrow heads and a ring at every stop. */
function flowMarkup(points, width, height) {
  if (points.length < 2) return "";
  const path = points.map(([x, y]) => `${Math.round(x)},${Math.round(y)}`).join(" ");
  const parts = [`<polyline class="overlay-flow-casing" points="${path}"/>`, `<polyline class="overlay-flow" points="${path}"/>`];
  const dotRadius = Math.max(4, Math.min(width, height) * 0.007);
  points.forEach(([x, y], index) => {
    if (index === 0) return;
    const [px, py] = points[index - 1];
    const dx = x - px, dy = y - py;
    const length = Math.hypot(dx, dy) || 1;
    const tipX = x - (dx / length) * dotRadius * 1.6;
    const tipY = y - (dy / length) * dotRadius * 1.6;
    const size = dotRadius * 1.5;
    const nx = -dy / length, ny = dx / length;
    parts.push(`<polygon class="overlay-flow-arrow" points="${tipX},${tipY} ${tipX - (dx / length) * size + nx * size * 0.7},${tipY - (dy / length) * size + ny * size * 0.7} ${tipX - (dx / length) * size - nx * size * 0.7},${tipY - (dy / length) * size - ny * size * 0.7}"/>`);
    parts.push(`<circle class="overlay-flow-dot" cx="${Math.round(x)}" cy="${Math.round(y)}" r="${dotRadius}"/>`);
  });
  return parts.join("");
}

function overlayUnit(width) {
  // user units per screen pixel: the SVG scales user units by rectWidth/width, so dividing by it
  // keeps the chip the same size on screen at every zoom level.
  const rectWidth = els.overlay.getBoundingClientRect().width || width;
  return rectWidth > 0 ? width / rectWidth : 1;
}

function renderOverlay() {
  if (!state.detail || els.overlayImageToggle.checked) {
    els.overlay.innerHTML = "";
    return;
  }
  const width = state.detail.page_image.width;
  const height = state.detail.page_image.height;
  els.overlay.setAttribute("viewBox", `0 0 ${width} ${height}`);
  if (state.mode === "annotate") {
    els.overlay.innerHTML = renderAnnotationOverlay(width, height);
    return;
  }
  const items = layerItems();
  const unit = overlayUnit(width);
  const flow = ann.showFlow && items.length > 1
    ? flowMarkup(items.map((item) => [(item.bbox_image_px[0] + item.bbox_image_px[2]) / 2, (item.bbox_image_px[1] + item.bbox_image_px[3]) / 2]), width, height)
    : "";
  els.overlay.innerHTML = flow + items.map((item) => {
    const [x1, y1, x2, y2] = item.bbox_image_px;
    const color = colorOf(item._type);
    const selected = item._key === state.selectedKey ? "selected" : "";
    const label = els.labelToggle.checked
      ? overlayChip({x1, x2, y1, text: `${item._type}${item.level ? ` L${item.level}` : ""}`, color, unit, selected: Boolean(selected), state: "", pageWidth: width, pageHeight: height})
      : "";
    const cross = state.layer === "final" ? crossPageMarkup(item, item.bbox_image_px, unit, width, height) : "";
    return `<g data-key="${item._key}"><rect class="overlay-block ${selected}" style="--c:${color}" x="${x1}" y="${y1}" width="${x2 - x1}" height="${y2 - y1}" stroke="${color}" fill="${colorAlpha(item._type, 0.10)}"/>${label}${cross}</g>`;
  }).join("");
}

function renderLegend() {
  const types = [...new Set(layerItems().map((item) => item._type))].sort();
  els.legend.innerHTML = types.map((type) => `<span class="legend-item"><span class="legend-swatch" style="background:${colorOf(type)}"></span>${esc(type)}</span>`).join("");
}

function unresolvedFor(block) {
  if (state.layer !== "final") return [];
  const fields = state.detail.field_reviews.blocks?.[block.block_id] || {};
  return Object.entries(fields).filter(([, review]) => review.status !== "PASS");
}

function renderBlockList() {
  const query = els.blockSearch.value.trim().toLowerCase();
  const items = layerItems().filter((item) => !query || `${item._type} ${item._text} ${item.evidence_id || item.block_id || ""}`.toLowerCase().includes(query));
  if (state.mode === "annotate") {
    const dropIndex = ann.dropIndex;
    els.blockList.innerHTML = items.map((item) => {
      const tag = item._state === "chain" ? "" : `<span class="edit-tag ${esc(item._state)}">${esc(EDIT_LABEL[item._state] || item._state)}</span>`;
      const dragging = ann.dragKey === item.block_id ? "dragging" : "";
      const dropBefore = dropIndex === item._index ? "drop-before" : "";
      return `<div class="block-item annotate ${state.selectedKey === item._key ? "active" : ""} edit-${esc(item._state)} ${dragging} ${dropBefore}" data-key="${item._key}" data-index="${item._index}" draggable="true" style="--block-color:${colorOf(item._type)}" role="button" tabindex="0">
        <span class="drag-handle" title="拖拽调整阅读顺序">⠿</span>
        <span class="block-order">${item._index + 1}</span>
        <span class="block-main"><span class="block-title">${esc(item._type)}${tag}</span><span class="block-text">${esc(shortText(item._text) || item.block_id || "无文本")}</span></span>
      </div>`;
    }).join("") || '<div class="muted">当前页没有 block</div>';
    return;
  }
  els.blockList.innerHTML = items.map((item) => {
    const unresolved = unresolvedFor(item);
    return `<button type="button" class="block-item ${state.selectedKey === item._key ? "active" : ""}" data-key="${item._key}" style="--block-color:${colorOf(item._type)}">
      <span class="block-order">${item._index}</span>
      <span><span class="block-title">${esc(item._type)}${crossPageTags(item).map((tag) => ` <span class="cross-badge">${esc(tag)}</span>`).join("")}</span><span class="block-text">${esc(shortText(item._text) || item.evidence_id || "无文本")}</span></span>
      <span class="block-field-count">${unresolved.length ? `${unresolved.length} review` : ""}</span>
    </button>`;
  }).join("") || '<div class="muted">当前层没有 bbox 项</div>';
}

function selectBlock(key) {
  state.selectedKey = key;
  renderOverlay();
  renderBlockList();
  renderDetail();
}

function safeTableHtml(raw) {
  const source = new DOMParser().parseFromString(String(raw || ""), "text/html").querySelector("table");
  if (!source) return '<div class="muted">没有可渲染的 table HTML</div>';
  const table = document.createElement("table");
  table.className = "table-preview";
  source.querySelectorAll("tr").forEach((sourceRow) => {
    const row = document.createElement("tr");
    sourceRow.querySelectorAll(":scope > th, :scope > td").forEach((sourceCell) => {
      const cell = document.createElement(sourceCell.tagName.toLowerCase());
      for (const attr of ["rowspan", "colspan"]) {
        const value = Number.parseInt(sourceCell.getAttribute(attr) || "1", 10);
        if (Number.isFinite(value) && value > 1 && value <= 1000) cell.setAttribute(attr, String(value));
      }
      sourceCell.childNodes.forEach((node) => {
        if (node.nodeType === Node.TEXT_NODE) {
          node.textContent.split(/\n/).forEach((part, index) => {
            if (index) cell.append(document.createElement("br"));
            cell.append(document.createTextNode(part));
          });
        } else if (node.nodeType === Node.ELEMENT_NODE && node.tagName.toLowerCase() === "br") {
          cell.append(document.createElement("br"));
        } else if (node.nodeType === Node.ELEMENT_NODE && node.tagName.toLowerCase() === "img") {
          const ref = node.getAttribute("data-image-ref");
          if (ref) {
            const holder = document.createElement("span");
            holder.innerHTML = cellImageMarkup(ref);
            cell.append(holder.firstElementChild);
          }
        }
      });
      row.append(cell);
    });
    if (row.children.length) table.append(row);
  });
  return table.outerHTML;
}

function tableStats(raw) {
  const table = new DOMParser().parseFromString(String(raw || ""), "text/html").querySelector("table");
  if (!table) return "HTML unavailable";
  const rows = table.querySelectorAll("tr").length;
  const cells = table.querySelectorAll("th, td").length;
  const merged = [...table.querySelectorAll("th, td")].filter((cell) => Number(cell.getAttribute("rowspan") || 1) > 1 || Number(cell.getAttribute("colspan") || 1) > 1).length;
  return `${rows} rows · ${cells} cells · ${merged} merged`;
}

function renderDetail() {
  const item = layerItems().find((candidate) => candidate._key === state.selectedKey);
  if (!item) {
    els.detail.hidden = true;
    return;
  }
  // Editing lives in the card next to the box; the right panel stays a read-only inspector so the
  // annotator never has two competing forms for the same block.
  els.detail.hidden = false;
  const id = item.block_id || item.evidence_id || `${state.layer}:${item._index}`;
  const refs = [...(item.source_refs || []), ...(item.support_refs || [])];
  let fieldRows = "";
  if (state.layer === "final") {
    const fields = state.detail.field_reviews.blocks?.[item.block_id] || {};
    fieldRows = Object.entries(fields).map(([field, review]) => `<tr><td>${esc(field)}</td><td><span class="field-status ${esc(review.status)}">${esc(review.status)}</span></td><td>${esc(review.method || "")}</td></tr>`).join("");
  }
  const wordEvidence = (state.detail.evidence.word || []).find((entry) => entry.evidence_id === item.word_table_ref);
  els.detail.innerHTML = `
    <div class="detail-head"><h3>${esc(id)}</h3><span class="badge">${esc(item._type)}</span></div>
    <dl class="detail-grid">
      <dt>bbox image</dt><dd>${esc((item.bbox_image_px || []).join(", "))}</dd>
      <dt>bbox PDF</dt><dd>${esc((item.bbox_pdf_pt || []).join(", ") || "-")}</dd>
      <dt>reading order</dt><dd>${esc(item.reading_order ?? "-")}</dd>
      <dt>level</dt><dd>${esc(item.level ?? item.level_candidate ?? "-")}</dd>
      <dt>source type</dt><dd>${esc(item.source_type || "-")}</dd>
    </dl>
    ${contractGtDetail(item)}
    ${item._type === "table"
      ? `<div class="table-compare-label">当前页 HTML</div><div class="table-meta">${esc(tableStats(item.html || item._text))}</div><div class="table-html-wrap">${safeTableHtml(item.html || item._text)}</div><details class="raw-html"><summary>当前页原始 HTML</summary><pre>${esc(item.html || item._text)}</pre></details>${wordEvidence ? `<div class="table-compare-label">Word 逻辑表 · ${esc(wordEvidence.rows)}x${esc(wordEvidence.cols)} · match ${esc(item.word_table_match_score)}</div><div class="table-html-wrap word-table">${safeTableHtml(wordEvidence.html)}</div><details class="raw-html"><summary>Word 原始 HTML</summary><pre>${esc(wordEvidence.html)}</pre></details>` : ""}`
      : item._text ? `<div class="text-box">${esc(item._text)}</div>` : ""}
    ${crossPageDetail(item)}
    ${item.embedded_images?.length ? `<div class="embedded-image-list">${item.embedded_images.map((image) => `<div><span class="table-image-token">▧</span> ${esc(image.image_ref)} · row ${esc(image.row)} col ${esc(image.col)} · span ${esc(image.rowspan)}x${esc(image.colspan)} · ${badge(image.status)}</div>`).join("")}</div>` : ""}
    ${fieldRows ? `<table class="field-table"><thead><tr><th>字段</th><th>状态</th><th>方法</th></tr></thead><tbody>${fieldRows}</tbody></table>` : ""}
    ${refs.length ? `<ul class="evidence-list">${refs.map((ref) => `<li>${esc(ref)}</li>`).join("")}</ul>` : ""}`;
}

function crossPageDetail(item) {
  const text = item.cross_page_text;
  const table = item.cross_page_table;
  if (!text && !table) return "";
  const rows = [];
  if (text) {
    rows.push(`<tr><td>文本流向</td><td>${esc(text.continues_block_id ? `承接 ${text.continues_block_id}` : "判定不承接上页尾")}</td><td>${esc(text.continued_by_block_id ? `被 ${text.continued_by_block_id} 承接` : "-")}</td></tr>`);
    rows.push(`<tr><td>判据</td><td colspan="2">${esc(Object.entries(text.confidence_basis || {}).map(([key, flag]) => `${key}=${flag}`).join(", "))}</td></tr>`);
  }
  if (table) {
    rows.push(`<tr><td>续表</td><td>${esc(table.part_index ? `${table.part_index}/${table.part_count}` : "-")}</td><td>${esc(table.document_table_identity || "-")}</td></tr>`);
    rows.push(`<tr><td>承接</td><td>${esc(table.continues_block_id || "-")}</td><td>${esc(table.continued_by_block_id || "-")}</td></tr>`);
    rows.push(`<tr><td>截断</td><td>${esc(`页底=${table.truncated_at_page_end ?? "未知"}`)}</td><td>${esc(`页顶=${table.truncated_at_page_start ?? "未知"} · 重复表头=${table.repeated_header ?? "未知"} · 闭合线=${table.has_closing_rule ?? "未知"}`)}</td></tr>`);
  }
  const method = (text || table).method || "-";
  const status = (text || table).status || "-";
  const refs = [...((text || {}).evidence_refs || []), ...((table || {}).evidence_refs || [])];
  return `<div class="cross-page-panel"><div class="table-compare-label">跨页 · <span class="field-status ${esc(status)}">${esc(status)}</span> · ${esc(method)}</div>
    <table class="field-table"><tbody>${rows.join("")}</tbody></table>
    ${refs.length ? `<div class="muted">evidence: ${refs.slice(0, 8).map((ref) => esc(ref)).join(", ")}</div>` : ""}</div>`;
}

function contractGtDetail(item) {
  const contract = item.contract_gt;
  const page = state.detail?.contract_gt || {};
  if (!contract) return page.bundle_id ? contractGtSummary(page) : "";
  const tree = contract.tree || {};
  const heading = contract.heading || {};
  const rows = [
    `<tr><td>A1 entity</td><td colspan="2">${esc(contract.entity_id || "-")}</td></tr>`,
    `<tr><td>A1 node</td><td colspan="2">${esc(contract.node_id || "-")}</td></tr>`,
    `<tr><td>A2 role</td><td colspan="2">${esc(contract.role || "-")}</td></tr>`,
    `<tr><td>A3 tree</td><td>${esc(tree.parent_node_id ? `parent ${tree.parent_node_id}` : "root")}</td><td>${esc(`order ${tree.order ?? "-"} · span ${tree.span_start_order ?? "-"}–${tree.span_end_order ?? "-"}`)}</td></tr>`,
  ];
  if (heading.heading_id) {
    rows.push(`<tr><td>A2 heading</td><td>${esc(`${heading.heading_role || "-"} · L${heading.level_absolute ?? "-"}`)}</td><td>${esc((heading.numbering || {}).raw_marker || "-")}</td></tr>`);
  }
  return `<div class="table-compare-label">Contract GT（A1/A2/A3）</div><table class="field-table"><tbody>${rows.join("")}</tbody></table>${contractGtSummary(page)}`;
}

function contractGtSummary(page) {
  if (!page || !page.bundle_id) return "";
  const coverage = page.coverage || {};
  const continuations = (page.table_continuations || [])
    .map((entry) => `${esc(shortBlockId(entry.source_table_id))}→${esc(shortBlockId(entry.target_table_id))}${entry.header_repeated ? "（重复表头）" : ""}`)
    .join("、");
  return `<div class="contract-gt-summary muted">${esc(page.bundle_id)} · schema ${esc(page.schema_version || "-")} · ${esc(page.coordinate_space || "-")}<br>覆盖 graph ${esc(coverage.graph_node_count ?? "-")} / A1 ${esc(coverage.a1_mapped_node_count ?? "-")} / A2 ${esc(coverage.a2_classified_node_count ?? "-")} / A3 ${esc(coverage.a3_linked_node_count ?? "-")}${continuations ? `<br>跨页续表：${continuations}` : ""}</div>`;
}

function renderDocumentFlow() {
  const flow = state.detail?.document_flow || {};
  const edges = flow.text_edges || [];
  const groups = flow.table_groups || [];
  if (!edges.length && !groups.length) return "";
  const edgeRows = edges.map((edge) => `<div class="issue-row relation-row"><strong>⇅ 文本 ${esc(edge.from?.page)}:${esc(shortBlockId(edge.from?.block_id))} → ${esc(edge.to?.page)}:${esc(shortBlockId(edge.to?.block_id))}</strong><p>${badge(edge.status || "-")} ${esc(edge.method || "")}</p></div>`);
  const groupRows = groups.map((group) => `<div class="issue-row relation-row"><strong>▦ 续表 ${esc(group.part_count)} 段</strong><p>${group.parts.map((part) => `${esc(part.page)}:${esc(shortBlockId(part.block_id))}${part.repeated_header ? " 重复表头" : ""}`).join(" → ")}</p></div>`);
  return `<div class="flow-section"><div class="table-compare-label">文档跨页关系 · ${edges.length} 文本 / ${groups.length} 表组</div>${edgeRows.concat(groupRows).join("")}</div>`;
}

function renderIssues() {
  if (!state.detail) return;
  const validationErrors = state.detail.validation.errors || [];
  const reviews = state.detail.review_items || [];
  const unresolved = state.detail.validation.unresolved_fields || [];
  const relations = state.detail.document_relations || [];
  els.issuesPanel.innerHTML = [
    renderDocumentFlow(),
    contractGtSummary(state.detail.contract_gt || {}),
    ...relations.map((item) => `<div class="issue-row relation-row"><strong>跨页关系 · ${esc(item.relation)}</strong><p>${esc(item.previous_block_id || "-")} -> ${esc((item.current_block_ids || []).join(", ") || "-")}</p><p>${esc(item.reason || "")}</p></div>`),
    ...validationErrors.map((item) => `<div class="issue-row"><strong>${badge("CHECK")} ${esc(item.code)}</strong><p>${esc(item.message)}</p></div>`),
    ...reviews.map((item) => `<div class="issue-row"><strong>${badge("CHECK")} ${esc(item.field || "page")} ${esc(item.target || "")}</strong><p>${esc(item.issue)}</p></div>`),
    ...unresolved.map((item) => `<div class="issue-row"><strong>${badge("CHECK")} ${esc(item.block_id)}</strong><p>${esc(item.field)} · ${esc(item.status)}</p></div>`),
  ].join("") || '<div class="muted">没有问题</div>';
}

function renderTasks() {
  if (!state.detail) return;
  const decisions = new Map(state.detail.decisions.map((entry) => [String(entry.content.task_id || entry.name).replace(/\.retry$|\.error$/, ""), entry]));
  els.tasksPanel.innerHTML = state.detail.tasks.map((task) => {
    const taskId = task.content.task_id || task.name;
    const decision = decisions.get(taskId);
    return `<div class="task-row"><strong>${esc(task.name)}</strong><p>${esc(task.content.task_type || "task")} · ${(task.content.items || []).length || 1} item(s)</p>
      <details><summary>查看 task</summary><pre>${esc(JSON.stringify(task.content, null, 2))}</pre></details>
      ${decision ? `<details><summary>查看 decision</summary><pre>${esc(JSON.stringify(decision.content, null, 2))}</pre></details>` : ""}
    </div>`;
  }).join("") || '<div class="muted">本页没有模型任务</div>';
}

/** One URL per page and kind: revisits reuse the browser cache instead of re-downloading the PNG. */
function pageImageUrl(sampleId, page, kind) {
  if (!state.imageUrls) state.imageUrls = new Map();
  // The batch belongs in the URL: without it the server answers from its default batch, and an
  // annotator working in another part gets the wrong picture (or nothing at all).
  const key = `${state.batch}:${sampleId}:${page}:${kind}`;
  if (!state.imageUrls.has(key)) {
    const batch = state.batch ? `&batch=${encodeURIComponent(state.batch)}` : "";
    state.imageUrls.set(key, `/api/image/${encodeURIComponent(sampleId)}/${page}/${kind}?v=${Date.now()}${batch}`);
  }
  return state.imageUrls.get(key);
}

function preloadNeighbourPages() {
  const document_ = state.catalog?.documents.find((entry) => entry.sample_id === state.detail?.sample_id);
  if (!document_) return;
  const index = document_.pages.findIndex((entry) => entry.page === state.detail.page);
  if (index < 0) return;
  const kind = els.overlayImageToggle.checked ? "overlay" : "source";
  const warm = () => {
    for (const offset of [1, -1, 2]) {
      const neighbour = document_.pages[index + offset];
      if (!neighbour || neighbour.sample_id !== state.detail?.sample_id) continue;
      const image = new Image();
      image.decoding = "async";
      image.src = pageImageUrl(neighbour.sample_id, neighbour.page, kind);
    }
  };
  if (window.requestIdleCallback) window.requestIdleCallback(warm, {timeout: 1500});
  else window.setTimeout(warm, 200);
}

function updateImage() {
  if (!state.detail) return;
  const kind = els.overlayImageToggle.checked ? "overlay" : "source";
  els.pageImage.src = pageImageUrl(state.detail.sample_id, state.detail.page, kind);
  els.overlay.hidden = els.overlayImageToggle.checked;
  renderOverlay();
  preloadNeighbourPages();
}

function applyZoom() {
  if (!state.detail) return;
  state.zoom = Math.max(.2, Math.min(3, state.zoom));
  els.pageFrame.style.width = `${state.naturalWidth * state.zoom}px`;
  els.pageFrame.style.height = `${state.naturalHeight * state.zoom}px`;
  els.zoomValue.textContent = `${Math.round(state.zoom * 100)}%`;
  renderOverlay();
  positionBlockPopover(true);
}

function fitZoom() {
  if (!state.naturalWidth) return;
  const available = Math.max(250, els.canvasScroll.clientWidth - 40);
  state.zoom = Math.min(1.5, available / state.naturalWidth);
  applyZoom();
}

function switchTab(name) {
  ["blocks", "issues", "annotation", "text", "tasks"].forEach((tab) => {
    $(`${tab}Panel`).hidden = tab !== name;
    els.tabs.querySelector(`[data-tab="${tab}"]`).classList.toggle("active", tab === name);
  });
}

els.pageList.addEventListener("click", (event) => {
  const pageTarget = event.target.closest(".page-item");
  if (pageTarget) {
    selectPage(pageTarget.dataset.sample, Number(pageTarget.dataset.page));
    return;
  }
  const documentTarget = event.target.closest(".document-item");
  if (documentTarget) {
    const sampleId = documentTarget.dataset.document;
    if (state.expandedDocuments.has(sampleId)) state.expandedDocuments.delete(sampleId);
    else state.expandedDocuments.add(sampleId);
    renderCatalog();
  }
});
els.layerControl.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-layer]");
  if (!button) return;
  state.layer = button.dataset.layer;
  state.selectedKey = null;
  els.layerControl.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
  renderCurrentLayer();
});
els.overlay.addEventListener("click", (event) => {
  const group = event.target.closest("g[data-key]");
  if (group) selectBlock(group.dataset.key);
});
els.blockList.addEventListener("dragstart", (event) => {
  if (state.mode !== "annotate") return;
  const item = event.target.closest(".block-item");
  if (!item) return;
  const key = String(item.dataset.key || "");
  ann.dragKey = key.startsWith("ann:") ? key.slice(4) : null;
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
});

els.blockList.addEventListener("dragover", (event) => {
  if (state.mode !== "annotate" || !ann.dragKey) return;
  const item = event.target.closest(".block-item");
  if (!item) return;
  event.preventDefault();
  const index = Number(item.dataset.index);
  const box = item.getBoundingClientRect();
  ann.dropIndex = event.clientY > box.top + box.height / 2 ? index + 1 : index;
  renderBlockList();
});

els.blockList.addEventListener("dragleave", (event) => {
  if (state.mode !== "annotate") return;
  if (!els.blockList.contains(event.relatedTarget)) {
    ann.dropIndex = null;
  }
});

els.blockList.addEventListener("drop", (event) => {
  if (state.mode !== "annotate" || !ann.dragKey) return;
  event.preventDefault();
  const sourceIndex = ann.blocks.findIndex((block) => block.block_id === ann.dragKey);
  const target = ann.dropIndex ?? Number(event.target.closest(".block-item")?.dataset.index ?? ann.blocks.length);
  const adjusted = target > sourceIndex ? target - 1 : target;
  const dragged = ann.dragKey;
  ann.dragKey = null;
  ann.dropIndex = null;
  if (sourceIndex >= 0) moveBlockTo(dragged, adjusted);
  else renderBlockList();
});

els.blockList.addEventListener("dragend", () => {
  ann.dragKey = null;
  ann.dropIndex = null;
  renderBlockList();
});

function nudgeOrder(blockId, delta) {
  moveBlockInOrder(blockId, delta);
}

function jumpToPage() {
  const doc = state.catalog?.documents.find((entry) => entry.sample_id === state.detail?.sample_id);
  const number = Number($("pageJump").value);
  if (!doc || !Number.isInteger(number) || number < 1 || number > doc.pages.length) {
    toast("请输入本文档范围内的页码", true);
    return;
  }
  selectPage(doc.sample_id, doc.pages[number - 1].page);
}

function nextPendingPage() {
  const current = state.pages.findIndex((item) => item.sample_id === state.detail?.sample_id && item.page === state.detail?.page);
  const target = state.pages.slice(current + 1).find((item) => {
    if (state.queue && !state.queueSet.has(item.sample_id)) return false;
    return state.mode === "annotate" ? !isPageAnnotated(item) : item.status === "CHECK";
  });
  if (target) selectPage(target.sample_id, target.page);
  else toast(state.mode === "annotate" ? "后续页面均已标注" : "后续没有待复核页面");
}

$("pageJump")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); jumpToPage(); }
});
$("jumpPageBtn")?.addEventListener("click", jumpToPage);
$("nextPending")?.addEventListener("click", nextPendingPage);
$("retrySave")?.addEventListener("click", () => saveAnnotations(false));
$("exportDraft")?.addEventListener("click", exportLocalAnnotation);

els.blockList.addEventListener("keydown", (event) => {
  if (state.mode !== "annotate") return;
  const item = event.target.closest(".block-item");
  if (!item) return;
  const key = String(item.dataset.key || "");
  const blockId = key.startsWith("ann:") ? key.slice(4) : null;
  if (!blockId) return;
  if (event.key === "ArrowUp" && event.altKey) { event.preventDefault(); nudgeOrder(blockId, -1); }
  if (event.key === "ArrowDown" && event.altKey) { event.preventDefault(); nudgeOrder(blockId, 1); }
});

els.blockList.addEventListener("click", (event) => {
  const target = event.target.closest(".block-item");
  if (!target) return;
  const key = String(target.dataset.key || "");
  const blockId = key.startsWith("ann:") ? key.slice(4) : null;
  if (state.mode === "annotate" && ann.orderPick) {
    if (blockId) applyOrderPick(blockId);
    return;
  }
  if (state.mode === "annotate" && ann.linkPick) {
    if (blockId) applyLinkPick(blockId);
    else cancelLinkPick();
    return;
  }
  if (state.mode === "annotate") selectAnnotationBlock(blockId);
  else selectBlock(key);
});
els.tabs.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (button) switchTab(button.dataset.tab);
});
els.pageImage.addEventListener("load", () => {
  state.naturalWidth = state.detail?.page_image?.width || els.pageImage.naturalWidth;
  state.naturalHeight = state.detail?.page_image?.height || els.pageImage.naturalHeight;
  state.zoom = 0.45;
  applyZoom();
});
els.searchInput.addEventListener("input", renderCatalog);
els.batchFilter.addEventListener("change", () => switchBatch(els.batchFilter.value));
els.statusFilter.addEventListener("change", renderCatalog);
els.blockSearch.addEventListener("input", renderBlockList);
els.labelToggle.addEventListener("change", renderOverlay);
els.overlayImageToggle.addEventListener("change", updateImage);
els.zoomOut.addEventListener("click", () => { state.zoom /= 1.2; applyZoom(); });
els.zoomIn.addEventListener("click", () => { state.zoom *= 1.2; applyZoom(); });
els.zoomFit.addEventListener("click", fitZoom);
els.approveBtn.addEventListener("click", () => saveReview("PASS"));
els.rejectBtn.addEventListener("click", () => saveReview("FAIL"));
window.addEventListener("keydown", (event) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
  if (!els.docDialog.hidden) {
    if (event.key === "Escape") { closeDocumentDialog(); return; }
    if (event.key === "Enter" && !typing) { event.preventDefault(); submitDocumentDialog("PASS"); return; }
    return;
  }
  if (els.resetDialog && !els.resetDialog.hidden) {
    if (event.key === "Escape") { event.preventDefault(); closeResetDialog(); return; }
    if (event.key === "Enter") { event.preventDefault(); confirmResetDialog(); return; }
    return;
  }
  if (typing) return;
  const key = event.key.toLowerCase();
  // annotation shortcuts exist only in annotation mode: browse mode keeps its read-only navigation
  if (state.mode !== "annotate") {
    if (key === "q" || event.key === "ArrowLeft") navigatePage(-1);
    if (key === "e" || event.key === "ArrowRight") navigatePage(1);
    if (key === "a") navigateDocument(-1);
    if (key === "d") navigateDocument(1);
    return;
  }
  if (event.ctrlKey || event.metaKey) {
    if (key === "z" && !event.shiftKey) { event.preventDefault(); undo(); return; }
    if (key === "z" || key === "y") { event.preventDefault(); redo(); return; }
    if (key === "s") { event.preventDefault(); saveAnnotations(false); return; }
    return;
  }
  if (key === "delete" || key === "backspace") {
    event.preventDefault();
    if (ann.multi.length >= 2) { deleteSelectedBlocks(); return; }
    const block = selectedAnnotationBlock();
    if (block) deleteBlock(block.block_id);
    else toast("先选中一个框再删除", true);
    return;
  }
  if (event.key === "Escape") {
    if (!els.docDialog.hidden) { closeDocumentDialog(); return; }
    if (ann.orderPick) { cancelOrderPick(); return; }
    if (ann.linkPick) { cancelLinkPick(); return; }
    if (ann.createMode) { exitCreateMode(); return; }
    ann.drawing = null;
    ann.drag = null;
    ann.snapPreview = null;
    selectAnnotationBlock(null);
    updateModeBanner();
    return;
  }
  if (key === "o") {
    startOrderPick();
    return;
  }
  if (key === "r") {
    openResetDialog();
    return;
  }
  if (key === "b") {
    if (ann.createMode) exitCreateMode();
    else enterCreateMode();
    return;
  }
  if (event.key === "Enter" && ann.draft) {
    event.preventDefault();
    confirmDraft();
    return;
  }
  if (event.key.startsWith("Arrow")) {
    const block = selectedAnnotationBlock();
    if (block) {
      event.preventDefault();
      const step = event.shiftKey ? 10 : 1;
      const dx = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
      const dy = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
      updateBlockFields(block.block_id, {bbox_image_px: [block.bbox_image_px[0] + dx, block.bbox_image_px[1] + dy, block.bbox_image_px[2] + dx, block.bbox_image_px[3] + dy]}, "move");
    }
    return;
  }
  if (key === "[" || key === "]") {
    const block = selectedAnnotationBlock();
    if (block) {
      event.preventDefault();
      moveBlockInOrder(block.block_id, key === "[" ? -1 : 1);
    }
    return;
  }
  const quickIndex = Number.parseInt(event.key, 10);
  if (Number.isInteger(quickIndex) && quickIndex >= 1 && quickIndex <= QUICK_TYPES.length) {
    ann.quickType = QUICK_TYPES[quickIndex - 1];
    updateAnnotationChrome();
    const block = selectedAnnotationBlock();
    if (block) retypeBlock(block.block_id, ann.quickType);
    return;
  }
  if (key === "q") navigatePage(-1);
  if (key === "e") navigatePage(1);
  if (key === "a") navigateDocument(-1);
  if (key === "d") navigateDocument(1);
});

loadCatalog().catch((error) => {
  els.emptyState.textContent = `加载失败：${error.message}`;
});

/* ============================================================================
 * Annotation mode
 * ----------------------------------------------------------------------------
 * Edits never touch runs/: they are stored as an append-only patch under
 * <batch>/annotations/<sample>/page_XXXX.json. The server re-validates every
 * saved block with the same schema the pipeline uses (type/level/text/html/
 * parent/bbox), so an exported annotation is a legal revision block.
 * ========================================================================== */

const EDIT_LABEL = {chain: "", added: "新增", updated: "修改", deleted: "删除"};
const ANNOTATION_TEXT_TYPES = new Set(["text", "title", "table_caption", "image_caption", "formula", "header", "footer", "page_number", "footnote"]);
const ANNOTATION_CAPTION_TYPES = new Set(["table_caption", "image_caption"]);
const ANNOTATION_MEDIA_TYPES = new Set(["image", "chart", "seal"]);
const QUICK_TYPES = ["text", "title", "table", "image", "chart", "table_caption", "image_caption", "header", "footer", "page_number", "formula", "seal", "footnote"];
// Annotators may mark deeper headings than the pipeline contract (which flattens >4): the annotation
// layer keeps the human level, the export clamps it back to MAX_TITLE_LEVEL.
const MAX_TITLE_LEVEL_UI = 6;
const ALL_BLOCK_TYPES = ["text", "title", "table", "table_caption", "image", "image_caption", "chart", "seal", "formula", "header", "footer", "page_number", "footnote"];
const AUTOSAVE_MS = 2500;

function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", Boolean(isError));
  els.toast.hidden = false;
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => { els.toast.hidden = true; }, isError ? 9000 : 2600);
}

const cloneValue = (value) => JSON.parse(JSON.stringify(value));
const area = (box) => Math.max(0, box[2] - box[0]) * Math.max(0, box[3] - box[1]);
const insideBox = (box, point) => point.x >= box[0] && point.x <= box[2] && point.y >= box[1] && point.y <= box[3];
function intersectionArea(a, b) {
  const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
  const h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
  return w > 0 && h > 0 ? w * h : 0;
}
const ioa = (inner, outer) => (area(inner) > 0 ? intersectionArea(inner, outer) / area(inner) : 0);

function annotationItems() {
  return ann.blocks.map((block, index) => ({
    ...block,
    _key: `ann:${block.block_id}`,
    _index: index,
    _type: block.type,
    _text: block.text || block.html || "",
    _state: block._state || "chain",
    _bbox: block.bbox_image_px,
  }));
}

/** The selected annotation block - derived from the single selection key so the canvas, the list and
 *  the keyboard can never disagree about what is selected. */
function selectedAnnotationBlock() {
  const key = String(state.selectedKey || "");
  const blockId = key.startsWith("ann:") ? key.slice(4) : ann.selectedId;
  return ann.blocks.find((block) => block.block_id === blockId) || null;
}

function selectAnnotationBlock(blockId, {additive = false} = {}) {
  if (liveEdit.blockId && liveEdit.blockId !== blockId) commitLiveEdit();
  if (additive && blockId) {
    ann.multi = ann.multi.includes(blockId) ? ann.multi.filter((id) => id !== blockId) : [...ann.multi, blockId];
    if (!ann.multi.length) { ann.selectedId = null; state.selectedKey = null; }
    else { ann.selectedId = ann.multi[ann.multi.length - 1]; state.selectedKey = `ann:${ann.selectedId}`; }
  } else if (blockId) {
    ann.multi = ann.multi.includes(blockId) && ann.multi.length > 1 ? ann.multi : [blockId];
    ann.selectedId = blockId;
    state.selectedKey = `ann:${blockId}`;
  } else {
    ann.multi = [];
    ann.selectedId = null;
    state.selectedKey = null;
  }
  state.selectedKey = blockId ? `ann:${blockId}` : null;
  renderOverlay();
  renderBlockList();
  renderDetail();
  renderAnnotationPanel();
  renderTextPanel();
  renderBlockPopover();
}

function resetAnnotationState() {
  ann.blocks = [];
  ann.deleted = new Map();
  ann.ops = [];
  ann.undo = [];
  ann.redo = [];
  ann.selectedId = null;
  ann.drawing = null;
  ann.drag = null;
  ann.snapPreview = null;
  ann.dirty = false;
  ann.exists = false;
  ann.revision = 0;
  ann.updatedAt = null;
  ann.stale = false;
  ann.saveError = "";
  ann.savedAt = null;
  ann.serial = 0;
  window.clearTimeout(ann.autosaveTimer);
}

function renumber() {
  ann.blocks.forEach((block, index) => { block.reading_order = index; });
  ann.blocks.forEach((block) => {
    if (block.type !== "title") delete block.level;
    if (block.type === "title" && !Number.isInteger(block.level)) block.level = 1;
    if (ANNOTATION_CAPTION_TYPES.has(block.type) && !block.parent_id) {
      const parent = ann.blocks.find((item) => item !== block && ANNOTATION_MEDIA_TYPES.has(item.type));
      if (parent) block.parent_id = parent.block_id;
    }
  });
}

function applyPatch(patch) {
  const keepId = selectedAnnotationBlock()?.block_id || ann.selectedId;
  resetAnnotationState();
  const chain = state.detail.blocks || [];
  const byId = new Map(chain.map((block) => [block.block_id, block]));
  const comparedFields = ["type", "level", "text", "html", "parent_id", "bbox_image_px"];
  if (patch.exists) {
    ann.blocks = (patch.blocks || []).map((block) => {
      const original = byId.get(block.block_id);
      const changed = !original || comparedFields.some((field) => JSON.stringify(original[field]) !== JSON.stringify(block[field]));
      return {...block, _state: original ? (changed ? "updated" : "chain") : "added"};
    });
    const present = new Set(ann.blocks.map((block) => block.block_id));
    for (const block of chain) {
      if (!present.has(block.block_id)) ann.deleted.set(block.block_id, {...block, _state: "deleted"});
    }
    ann.exists = true;
    ann.revision = patch.revision || 0;
    ann.updatedAt = patch.updated_at || null;
    ann.stale = Boolean(patch.stale);
    ann.annotator = patch.annotator || ann.annotator;
  } else {
    ann.blocks = chain.map((block) => ({...cloneValue(block), _state: "chain"}));
  }
  renumber();
  // keep the annotator's selection across saves/reloads (autosave must not drop it)
  if (keepId && ann.blocks.some((block) => block.block_id === keepId)) {
    ann.selectedId = keepId;
    state.selectedKey = `ann:${keepId}`;
  }
  els.annotatorName.value = ann.annotator || "";
  updateAnnotationChrome();
}

function snapshotState() {
  return {
    blocks: ann.blocks.map(cloneValue),
    deleted: [...ann.deleted.entries()].map(([key, value]) => [key, cloneValue(value)]),
    ops: ann.ops.map((operation) => ({...operation, fields: [...(operation.fields || [])]})),
    selectedId: ann.selectedId,
  };
}

function pushUndo() {
  ann.undo.push(snapshotState());
  if (ann.undo.length > 80) ann.undo.shift();
  ann.redo = [];
}

function restoreState(snapshot) {
  ann.blocks = snapshot.blocks.map(cloneValue);
  ann.deleted = new Map(snapshot.deleted.map(([key, value]) => [key, cloneValue(value)]));
  ann.ops = snapshot.ops.map((operation) => ({...operation, fields: [...(operation.fields || [])]}));
  ann.selectedId = snapshot.selectedId;
  state.selectedKey = ann.selectedId ? `ann:${ann.selectedId}` : null;
  markDirty();
  renderCurrentLayer();
}

function undo() {
  const snapshot = ann.undo.pop();
  if (!snapshot) return;
  ann.redo.push(snapshotState());
  restoreState(snapshot);
}

function redo() {
  const snapshot = ann.redo.pop();
  if (!snapshot) return;
  ann.undo.push(snapshotState());
  restoreState(snapshot);
}

function recordOp(kind, blockId, fields, note = "") {
  ann.ops.push({kind, block_id: blockId, fields: fields || [], note});
  markDirty();
}

function markDirty() {
  ann.dirty = true;
  ann.editVersion += 1;
  updateAnnotationChrome();
  // annotation saves by default: the annotator never has to remember a save button
  window.clearTimeout(ann.autosaveTimer);
  ann.autosaveTimer = window.setTimeout(() => { saveAnnotations(true); }, AUTOSAVE_MS);
}

function toggleButton(element, value) {
  if (!element) return;
  element.classList.toggle("active", Boolean(value));
  element.setAttribute("aria-pressed", String(Boolean(value)));
}

/** Only the save-state chip: the rest of the chrome does not change while typing. */
function updateDirtyChrome() {
  if (!els.dirtyChip) return;
  els.dirtyChip.textContent = ann.saving ? "保存中…" : ann.saveError ? "保存失败 · 改动仍保留" : ann.dirty ? `未保存 · ${ann.ops.length} 处` : `已保存${ann.savedAt ? ` · ${ann.savedAt}` : ""}`;
  els.dirtyChip.classList.toggle("dirty", ann.dirty);
  const feedback = $("saveFeedback");
  if (feedback) {
    feedback.hidden = !ann.saveError;
    $("saveErrorText").textContent = ann.saveError;
  }
}

function updateAnnotationChrome() {
  const selected = selectedAnnotationBlock();
  updateDirtyChrome();
  els.undoBtn.disabled = !ann.undo.length;
  els.redoBtn.disabled = !ann.redo.length;
  if (els.saveAnnotations) els.saveAnnotations.disabled = ann.saving || !ann.dirty;
  if (els.deleteBoxBtn) els.deleteBoxBtn.disabled = !selected;
  if (els.orderPickBtn) {
    els.orderPickBtn.disabled = !selected;
    els.orderPickBtn.classList.toggle("active", Boolean(ann.orderPick));
  }
  if (els.snapToggle) els.snapToggle.checked = ann.snap;
  if (els.flowToggle) els.flowToggle.checked = ann.showFlow;
  updateModeBanner();
  updatePageStepper();
  renderBatchProgress();
}

function updatePageStepper() {
  if (!els.pageStepperLabel) return;
  const document_ = state.catalog?.documents.find((entry) => entry.sample_id === state.detail?.sample_id);
  const index = document_?.pages.findIndex((entry) => entry.page === state.detail?.page);
  els.pageStepperLabel.textContent = document_ && index >= 0 ? `${state.detail.page} / ${document_.pages.length}` : "-";
  const docs = state.catalog?.documents.filter((doc) => doc.pages?.length) || [];
  const docIndex = docs.findIndex((doc) => doc.sample_id === state.detail?.sample_id);
  if (els.prevPageBtn) els.prevPageBtn.disabled = !state.detail || (docIndex === 0 && index === 0);
  if (els.nextPageBtn) els.nextPageBtn.disabled = !state.detail || (docIndex === docs.length - 1 && index === document_?.pages.length - 1);
  const jump = $("pageJump");
  if (jump) {
    jump.max = document_?.pages.length || 1;
    jump.disabled = !state.detail;
    if (document.activeElement !== jump) jump.value = index >= 0 ? index + 1 : "";
  }
  if ($("nextPending")) $("nextPending").textContent = state.mode === "annotate" ? "下一未标注页 →" : "下一待复核页 →";
}

function renderBatchProgress() {
  if (!els.batchProgress) return;
  const total = state.pages.length || 0;
  const done = state.annotated?.size || 0;
  const ratio = total ? done / total : 0;
  els.batchProgress.innerHTML = `<div class="progress-label"><span>本批已标注页</span><strong>${done} / ${total}</strong></div>
    <div class="progress-track"><div class="progress-fill ok" style="width:${Math.round(ratio * 100)}%"></div></div>
    <div class="editor-hint">补丁写入 <code>annotations/&lt;sample&gt;/page_XXXX.json</code>，不会改动 runs/</div>`;
}

/** Give the block exactly the fields its type needs, dropping the ones it must not carry.
 *
 *  On a retype nothing is inherited: values that belonged to the previous type are cleared and no
 *  field is pre-filled with a guess. An annotator who changes the type must say what the block is,
 *  otherwise the completion counter would report "filled" for fields nobody looked at. */
function applyTypeDefaults(block, type, {reset = false} = {}) {
  block.type = type;
  // Changing the type must not destroy the content: the text belongs to the block, not to the type.
  // Switching to a type without a text field parks it (_carryText) so switching back restores it.
  if (ANNOTATION_TEXT_TYPES.has(type)) {
    if (typeof block.text !== "string" && typeof block._carryText === "string") block.text = block._carryText;
    if (typeof block.text !== "string") block.text = "";
  } else {
    if (typeof block.text === "string" && block.text) block._carryText = block.text;
    delete block.text;
  }
  if (type === "table") {
    // A draft grid lowers the barrier, but it still has to be filled in before the block counts.
    // Keep any table the annotator already has; a draft only fills the gap.
    if (typeof block.html !== "string" || !block.html.includes("<table")) {
      block.html = (typeof block._carryHtml === "string" && block._carryHtml.includes("<table") ? block._carryHtml : "")
        || tableDraftFromEvidence(block.bbox_image_px) || "<table><tr><td></td></tr></table>";
    }
  } else {
    if (typeof block.html === "string" && block.html.includes("<table")) block._carryHtml = block.html;
    delete block.html;
  }
  if (type === "title") {
    if (reset || !Number.isInteger(block.level)) delete block.level;   // the level is the annotator's call
  } else {
    delete block.level;
  }
  if (ANNOTATION_CAPTION_TYPES.has(type)) {
    if (reset) delete block.parent_id;                                 // do not guess the parent block
  } else {
    delete block.parent_id;
  }
}

function newBlockId() {
  ann.serial += 1;
  // `ann.serial` counts the session's edits, so it restarts at 1 on every reload and a second new box
  // could reuse an id that is already on the page - the server then rejects the whole save
  // ("duplicate block_id"). Uniqueness wins over a tidy sequence.
  const page = state.detail.page;
  const used = new Set([...ann.blocks.map((block) => block.block_id), ...ann.deleted.keys()]);
  let serial = ann.serial;
  let candidate = `manual.block.p${page}.${String(serial).padStart(4, "0")}`;
  while (used.has(candidate)) {
    serial += 1;
    candidate = `manual.block.p${page}.${String(serial).padStart(4, "0")}`;
  }
  ann.serial = serial;
  return candidate;
}

function inheritFromOverlappedBlock(block) {
  // Drawing a box over an existing table/image should not force the annotator to retype content:
  // inherit the strongest overlapping chain block of the same family.
  const candidates = (state.detail.blocks || []).filter((item) => ioa(item.bbox_image_px, block.bbox_image_px) >= 0.5);
  if (!candidates.length) return;
  const same = candidates.filter((item) => item.type === block.type);
  const best = (same.length ? same : candidates).sort((a, b) => area(b.bbox_image_px) - area(a.bbox_image_px))[0];
  if (!best) return;
  if (block.type === "table" && typeof best.html === "string" && best.html.includes("<table")) block.html = best.html;
  if (ANNOTATION_TEXT_TYPES.has(block.type) && typeof best.text === "string" && best.text.trim() && !String(block.text || "").trim()) block.text = best.text;
  if (block.type === "title" && Number.isInteger(best.level)) block.level = best.level;
  if (!block.source_refs.length) block.source_refs = [...(best.source_refs || [])];
}

/** Everything a drawn box covers: the PDF text layer *and* the blocks already on the page.

 * The annotator is normally framing something the pipeline already found, so its text is copied
 * instead of retyped (typing is where typos come from). Whichever source saw more text wins: on an
 * image-only page the model's text is the real content and the PDF has none, and vice versa.
 */
function contentFromBox(box) {
  const lines = evidenceLinesInside(box);
  const pdfText = lines.map((item) => String(item.text || "").trim()).filter(Boolean).join("\n");
  const boxArea = Math.max(1, area(box));
  const overlapped = (state.detail?.blocks || [])
    .filter((item) => {
      if (!Array.isArray(item.bbox_image_px)) return false;
      const [x1, y1] = [Math.max(item.bbox_image_px[0], box[0]), Math.max(item.bbox_image_px[1], box[1])];
      const [x2, y2] = [Math.min(item.bbox_image_px[2], box[2]), Math.min(item.bbox_image_px[3], box[3])];
      const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
      if (inter <= 0) return false;
      const smaller = Math.min(area(item.bbox_image_px), boxArea);
      return inter / Math.max(1, smaller) >= 0.5;
    })
    .sort((a, b) => (a.reading_order ?? 0) - (b.reading_order ?? 0));
  const blockText = overlapped
    .filter((item) => typeof item.text === "string" && item.text.trim())
    .map((item) => String(item.text).trim())
    .join("\n");
  const html = overlapped.map((item) => item.html).find((value) => typeof value === "string" && value.includes("<table")) || "";
  const sourceRefs = [...new Set(overlapped.flatMap((item) => item.source_refs || []))];
  const level = overlapped.map((item) => item.level).find((value) => Number.isInteger(value));
  const text = blockText.length > pdfText.length ? blockText : (pdfText || blockText);
  return {
    text,
    html,
    source_refs: sourceRefs,
    level,
    line_count: lines.length,
    block_count: overlapped.filter((item) => String(item.text || "").trim()).length,
  };
}

function addBlock(box, refs = [], fields = null) {
  pushUndo();
  const block = {
    block_id: newBlockId(),
    bbox_image_px: box.map((value) => Math.round(value * 100) / 100),
    source_refs: refs,
    support_refs: [],
    reading_order: ann.blocks.length,
  };
  applyTypeDefaults(block, fields?.type || ann.quickType);
  // No dialog: the new block is prefilled from the PDF lines inside the drawn box, and inherits the
  // HTML/text of any chain block it covers, so the annotator edits instead of retyping.
  if (fields) {
    if (fields.type === "title") block.level = fields.level;
    if (ANNOTATION_TEXT_TYPES.has(fields.type)) block.text = fields.text;
    if (fields.type === "table") block.html = fields.html;
    if (ANNOTATION_CAPTION_TYPES.has(fields.type)) { block.parent_id = fields.parent_id; block.text = fields.text; }
  } else {
    const content = contentFromBox(block.bbox_image_px);
    if (ANNOTATION_TEXT_TYPES.has(block.type)) block.text = content.text;
    if (block.type === "table") block.html = content.html || tableDraftFromEvidence(block.bbox_image_px) || block.html;
    if (!block.source_refs.length) block.source_refs = [...content.source_refs];
    if (block.type === "title" && Number.isInteger(content.level)) block.level = content.level;
    inheritFromOverlappedBlock(block);
    // Copied from the page (PDF lines and/or the blocks underneath): convenient, but the annotator
    // still confirms it - which is exactly why the copy must be right instead of empty.
    if (String(block.text || "").trim() || block.html) block._prefilled = true;
    if (String(block.text || "").trim()) toast(`已从页面复制文本（${content.line_count ? `${content.line_count} 行 PDF 文本` : "原有块"}），可直接编辑`);
  }
  block._state = "added";
  // A new box belongs where it sits on the page: insert it at the geometric position instead of
  // appending it to the end, so the reading order stays usable without a manual re-sort.
  // On a multi-column page "where the box sits" is a column, not a y coordinate: sorting pure by
  // position interleaves the columns and the annotator is told to fix an order the app just broke.
  // Only the insertion slot is computed from the column model - the existing order is never rewritten
  // (re-sorting the whole page here could duplicate or drop blocks, which the server rejects).
  const placement = detectPageColumns([...ann.blocks, block]);
  let insertAt;
  if (placement.columns > 1) {
    const probe = [...ann.blocks, block];
    const ordered = orderBlocksByColumns(probe, placement);
    const before = new Set(ordered.slice(0, ordered.findIndex((item) => item.block_id === block.block_id)).map((item) => item.block_id));
    insertAt = ann.blocks.filter((item) => before.has(item.block_id)).length;
  } else {
    insertAt = ann.blocks.filter((item) => item.bbox_image_px[1] <= block.bbox_image_px[1]).length;
  }
  ann.blocks.splice(insertAt, 0, block);
  renumber();
  selectAnnotationBlock(block.block_id);
  const placedAt = ann.blocks.findIndex((item) => item.block_id === block.block_id) + 1;
  recordOp("add", block.block_id, ["type", "bbox_image_px", ...(refs.length ? ["source_refs"] : []), `reading_order=${placedAt}`]);
  toast(`已新建并插入到第 ${placedAt} 位（按位置）`);
  switchTab("annotation");
}

/** Delete every box in the current selection (one undo step, one operation per block). */
function deleteSelectedBlocks() {
  const ids = ann.multi.length >= 2 ? [...ann.multi] : (ann.selectedId ? [ann.selectedId] : []);
  const blocks = ids.map((id) => ann.blocks.find((block) => block.block_id === id)).filter(Boolean);
  if (!blocks.length) return;
  pushUndo();
  for (const block of blocks) {
    ann.deleted.set(block.block_id, {...cloneValue(block), _state: "deleted"});
    recordOp("delete", block.block_id, ["deleted"]);
  }
  const drop = new Set(blocks.map((block) => block.block_id));
  ann.blocks = ann.blocks.filter((block) => !drop.has(block.block_id));
  ann.multi = [];
  selectAnnotationBlock(null);
  renumber();
  renderCurrentLayer();
  renderBlockPopover(true);
  toast(`已删除 ${blocks.length} 个框（可在「标注」页签恢复，Ctrl+Z 撤销）`);
}

function deleteBlock(blockId) {
  const index = ann.blocks.findIndex((item) => item.block_id === blockId);
  if (index < 0) return;
  const block = ann.blocks[index];
  pushUndo();
  ann.deleted.set(blockId, {...cloneValue(block), _state: "deleted"});
  ann.blocks = ann.blocks.filter((item) => item.block_id !== blockId);
  renumber();
  recordOp("delete", blockId, ["deleted"]);
  if (ann.selectedId === blockId) {
    // Close the editor: jumping to the neighbouring block made deletion look like a no-op.
    selectAnnotationBlock(null);
    toast("已删除该框，可在「标注」页签恢复（Ctrl+Z 撤销）");
    return;
  }
  renderCurrentLayer();
}

function restoreDeleted(blockId) {
  const block = ann.deleted.get(blockId);
  if (!block) return;
  pushUndo();
  ann.deleted.delete(blockId);
  const onChain = (state.detail.blocks || []).some((item) => item.block_id === blockId);
  ann.blocks.push({...cloneValue(block), _state: onChain ? "chain" : "added"});
  ann.selectedId = blockId;
  state.selectedKey = `ann:${blockId}`;
  recordOp("update", blockId, ["restored"]);
  renderCurrentLayer();
}

function updateBlockFields(blockId, patch, kind = "update") {
  const block = ann.blocks.find((item) => item.block_id === blockId);
  if (!block) return;
  pushUndo();
  delete block._prefilled;
  Object.assign(block, patch);
  if (block._state === "chain") block._state = "updated";
  recordOp(kind, blockId, Object.keys(patch));
  renderCurrentLayer();
}

/** Retype every selected block in one step: one undo entry, one repaint, one op per block. */
function retypeSelection(type) {
  const ids = [...ann.multi].filter((id) => ann.blocks.some((block) => block.block_id === id));
  if (!ids.length || !ALL_BLOCK_TYPES.includes(type)) return;
  if (ids.every((id) => ann.blocks.find((block) => block.block_id === id)?.type === type)) return;
  pushUndo();
  for (const id of ids) {
    const block = ann.blocks.find((item) => item.block_id === id);
    if (!block || block.type === type) continue;
    const before = block.type;
    applyTypeDefaults(block, type, {reset: true});
    if (block._state === "chain") block._state = "updated";
    recordOp("retype", id, ["type", `${before}→${type}`]);
  }
  renderCurrentLayer();
  toast(`已把 ${ids.length} 个框统一改为 ${type}（字段已清空，请填写）`);
}

/** Merge the multi-selected boxes into the first one: union box, union evidence, joined text.
 *  Tables are merged row-wise, dropping a repeated header row from the following part - which is
 *  exactly what a cross-page table needs. */
function mergeSelectedBlocks() {
  const ids = ann.multi.filter((id) => ann.blocks.some((block) => block.block_id === id));
  if (ids.length < 2) return;
  pushUndo();
  const members = ids.map((id) => ann.blocks.find((block) => block.block_id === id)).filter(Boolean);
  const primary = members[0];
  const ordered = orderBlocksByPosition(members);
  const boxes = ordered.map((block) => block.bbox_image_px);
  primary.bbox_image_px = [
    Math.min(...boxes.map((box) => box[0])), Math.min(...boxes.map((box) => box[1])),
    Math.max(...boxes.map((box) => box[2])), Math.max(...boxes.map((box) => box[3])),
  ];
  primary.source_refs = [...new Set(ordered.flatMap((block) => block.source_refs || []))];
  primary.support_refs = [...new Set(ordered.flatMap((block) => block.support_refs || []))];
  const types = [...new Set(ordered.map((block) => block.type))];
  if (types.length > 1) toast(`类型不一致（${types.join(" / ")}），保留 ${primary.type}`, true);
  if (ANNOTATION_TEXT_TYPES.has(primary.type)) {
    primary.text = ordered.map((block) => String(block.text || "").trim()).filter(Boolean).join("\n");
  }
  if (primary.type === "table") {
    primary.html = mergeTableHtml(ordered.map((block) => block.html || ""));
  }
  primary._state = primary._state === "chain" ? "updated" : primary._state;
  const removed = ordered.filter((block) => block.block_id !== primary.block_id);
  for (const block of removed) {
    ann.deleted.set(block.block_id, {...cloneValue(block), _state: "deleted"});
  }
  ann.blocks = ann.blocks.filter((block) => !removed.some((item) => item.block_id === block.block_id));
  renumber();
  recordOp("merge", primary.block_id, [`merged ${ordered.length} blocks`, ...removed.map((block) => block.block_id)]);
  ann.multi = [primary.block_id];
  selectAnnotationBlock(primary.block_id);
  renderCurrentLayer();
  toast(`已合并 ${ordered.length} 个框（Ctrl+Z 可撤销）`);
}

/** Row-wise concatenation; a repeated header row in the next part is dropped once. */
function mergeTableHtml(htmls) {
  const tables = htmls.map(parseBlockTable).filter(Boolean);
  if (tables.length < 2) return htmls[0] || "";
  const merged = tables[0].cloneNode(true);
  const headerOf = (table) => {
    const row = table.querySelector("tr");
    return row ? row.textContent.replace(/\s+/g, " ").trim() : "";
  };
  const head = headerOf(tables[0]);
  for (const table of tables.slice(1)) {
    const rows = [...table.querySelectorAll("tr")];
    const start = rows.length && headerOf(table) === head && head ? 1 : 0;
    for (const row of rows.slice(start)) merged.appendChild(row.cloneNode(true));
  }
  return merged.outerHTML;
}

function retypeBlock(blockId, type) {
  const block = ann.blocks.find((item) => item.block_id === blockId);
  if (!block || block.type === type) return;
  pushUndo();
  const before = block.type;
  // Nothing is carried over: fields of the previous type are cleared, so the annotator fills (or
  // re-confirms) what this type needs instead of letting a stale value look like a finished field.
  applyTypeDefaults(block, type, {reset: true});
  if (block._state === "chain") block._state = "updated";
  recordOp("retype", blockId, ["type", `${before}→${type}`]);
  renderCurrentLayer();
  toast(`已切换为 ${type}：该类型的字段已清空，请填写`);
}

function moveBlockTo(blockId, targetIndex) {
  const index = ann.blocks.findIndex((item) => item.block_id === blockId);
  if (index < 0) return;
  const bounded = Math.max(0, Math.min(ann.blocks.length - 1, targetIndex));
  if (bounded === index) return;
  pushUndo();
  const [block] = ann.blocks.splice(index, 1);
  ann.blocks.splice(bounded, 0, block);
  renumber();
  recordOp("move", blockId, [`order ${index + 1}→${bounded + 1}`]);
  renderCurrentLayer();
}

/** One-click reading order by geometry: row bands (tolerant) then left-to-right.
 *  Column layouts still need manual dragging - the button is the fast path, not the truth. */
/** Reading order by geometry: row bands (tolerance relative to the median block height), then left to
 *  right. Used both by the explicit "按位置重排" action and to place a freshly drawn box. */
function orderBlocksByPosition(blocks) {
  if (blocks.length < 2) return [...blocks];
  const heights = blocks.map((block) => block.bbox_image_px[3] - block.bbox_image_px[1]).sort((a, b) => a - b);
  const medianHeight = heights[Math.floor(heights.length / 2)] || 20;
  const tolerance = Math.max(6, medianHeight * 0.6);
  const rows = [];
  for (const block of blocks) {
    const y = block.bbox_image_px[1];
    const row = rows.find((candidate) => Math.abs(candidate.y - y) <= tolerance);
    if (row) row.items.push(block);
    else rows.push({y, items: [block]});
  }
  return rows
    .sort((a, b) => a.y - b.y)
    .flatMap((row) => row.items.sort((a, b) => a.bbox_image_px[0] - b.bbox_image_px[0]));
}

/* ----------------------------------------------------------------------------------------------
 * Column-aware reading order (viewer side)
 *
 * Same model as the pipeline's `ocr_agent.reading_order`: a gutter is a vertical strip no block
 * crosses, the columns are read left to right, and a full-width block splits the page into segments.
 * It only has to be good enough to power the one-click "按栏重排" button and the interleaving hint -
 * the authoritative order still comes from the pipeline, and the annotator can always fix it by hand.
 * ---------------------------------------------------------------------------------------------- */
const ORDER_FURNITURE = new Set(["header", "footer", "page_number"]);

function detectPageColumns(blocks) {
  const width = Number(state.detail?.page_image?.width || 0);
  const height = Number(state.detail?.page_image?.height || 0);
  const items = (blocks || []).filter(
    (block) => !ORDER_FURNITURE.has(block.type) && Array.isArray(block.bbox_image_px) && block.bbox_image_px.length === 4,
  );
  if (!width || !height || items.length < 4) return {columns: 1, gutter: null, fullSpan: []};
  const area = (block) => Math.max(0, block.bbox_image_px[2] - block.bbox_image_px[0]) * Math.max(0, block.bbox_image_px[3] - block.bbox_image_px[1]);
  const total = items.reduce((sum, block) => sum + area(block), 0) || 1;
  const edges = [...new Set(items.flatMap((block) => [block.bbox_image_px[0], block.bbox_image_px[2]]))].sort((a, b) => a - b);
  let best = null;
  for (const split of edges) {
    const left = items.filter((block) => block.bbox_image_px[2] <= split + 1);
    const right = items.filter((block) => block.bbox_image_px[0] >= split - 1);
    if (left.length < 2 || right.length < 2) continue;
    const share = Math.min(
      left.reduce((sum, block) => sum + area(block), 0) / total,
      right.reduce((sum, block) => sum + area(block), 0) / total,
    );
    if (share < 0.22) continue;
    const span = Math.min(
      (Math.max(...left.map((block) => block.bbox_image_px[3])) - Math.min(...left.map((block) => block.bbox_image_px[1]))) / height,
      (Math.max(...right.map((block) => block.bbox_image_px[3])) - Math.min(...right.map((block) => block.bbox_image_px[1]))) / height,
    );
    if (span < 0.30) continue;
    if (!best || share > best.share) best = {split, share};
  }
  if (!best) return {columns: 1, gutter: null, fullSpan: []};
  const fullSpan = items
    .filter((block) => block.bbox_image_px[0] <= best.split && block.bbox_image_px[2] >= best.split && block.bbox_image_px[2] - block.bbox_image_px[0] >= width * 0.62)
    .map((block) => block.block_id);
  return {columns: 2, gutter: best.split, fullSpan};
}

function columnOf(block, info) {
  if (!info || info.columns < 2 || !Number.isFinite(info.gutter)) return 0;
  const center = (block.bbox_image_px[0] + block.bbox_image_px[2]) / 2;
  return center < info.gutter ? 0 : 1;
}

function countColumnInterleaving(blocks, info) {
  if (!info || info.columns < 2 || !Array.isArray(blocks)) return 0;
  let count = 0;
  for (let index = 1; index < blocks.length; index += 1) {
    const previous = blocks[index - 1];
    const next = blocks[index];
    if (ORDER_FURNITURE.has(previous.type) || ORDER_FURNITURE.has(next.type)) continue;
    if (columnOf(previous, info) <= columnOf(next, info)) continue;
    const nextTop = next.bbox_image_px[1];
    const previousHeight = previous.bbox_image_px[3] - previous.bbox_image_px[1];
    if (nextTop > previous.bbox_image_px[1] + Math.max(6, previousHeight * 0.5)) count += 1;
  }
  return count;
}

/** Reading order for the detected columns: full-width blocks split the page, then left column down,
 *  then right column down. Falls back to the plain position order on single-column pages. */
function orderBlocksByColumns(blocks, info) {
  if (!info || info.columns < 2 || !Array.isArray(blocks) || blocks.length < 2) return orderBlocksByPosition(blocks);
  const fullSpan = new Set(info.fullSpan || []);
  const regions = [];
  let current = [];
  for (const block of orderBlocksByPosition(blocks)) {
    if (fullSpan.has(block.block_id)) {
      if (current.length) regions.push(current);
      regions.push([block]);
      current = [];
      continue;
    }
    current.push(block);
  }
  if (current.length) regions.push(current);
  const ordered = [];
  for (const region of regions) {
    if (region.length === 1 && fullSpan.has(region[0].block_id)) {
      ordered.push(region[0]);
      continue;
    }
    const left = orderBlocksByPosition(region.filter((block) => columnOf(block, info) === 0));
    const right = orderBlocksByPosition(region.filter((block) => columnOf(block, info) === 1));
    ordered.push(...left, ...right);
  }
  // Running head first, running foot last - same policy as the pipeline.
  const head = ordered.filter((block) => block.type === "header" || block.type === "page_number");
  const foot = ordered.filter((block) => block.type === "footer" || (block.type === "page_number" && !head.includes(block)));
  const body = ordered.filter((block) => !head.includes(block) && !foot.includes(block));
  return [...head, ...body, ...foot];
}

function autoSortByColumns() {
  if (ann.blocks.length < 2) return;
  const info = detectPageColumns(ann.blocks);
  if (info.columns < 2) {
    autoSortByPosition();
    return;
  }
  pushUndo();
  ann.blocks = orderBlocksByColumns(ann.blocks, info);
  renumber();
  recordOp("move", "page", ["reading_order", "auto_by_columns"]);
  renderCurrentLayer();
  toast(`已按 ${info.columns} 栏重排（左栏到底 → 右栏）`);
}

function clearBlockText() {
  const target = document.getElementById("popText");
  if (!target) return;
  target.value = "";
  target.dispatchEvent(new Event("input", {bubbles: true}));
  target.focus();
}

function autoSortByPosition() {
  if (ann.blocks.length < 2) return;
  pushUndo();
  ann.blocks = orderBlocksByPosition(ann.blocks);
  renumber();
  recordOp("move", "page", ["reading_order", "auto_by_position"]);
  renderCurrentLayer();
  toast("已按位置重排阅读顺序（多栏版面请再手动微调）");
}

function enterCreateMode() {
  if (!state.detail) {
    toast("先选择一页", true);
    return;
  }
  ann.createMode = true;
  ann.draft = null;
  ann.orderPick = null;
  selectAnnotationBlock(null);
  // The card floats above the canvas and would swallow the first drag of a new box: hide it while
  // framing (it comes back with the next selection).
  if (els.blockPopover) els.blockPopover.hidden = true;
  renderCurrentLayer();
  updateModeBanner();
}

function exitCreateMode() {
  ann.createMode = false;
  ann.draft = null;
  ann.drawing = null;
  // A cancelled draft must leave no half-finished gesture behind: a stale drag/draft handle would
  // otherwise swallow the first press of the next box (draw -> cancel -> draw again did nothing).
  ann.drag = null;
  ann.band = null;
  ann.snapPreview = null;
  ann.drawing = null;
  els.overlay?.classList.remove("is-dragging");
  renderCurrentLayer();
  updateModeBanner();
}

function confirmDraft() {
  const draft = ann.draft;
  if (!draft) return;
  ann.draft = null;
  ann.createMode = false;
  // the block is created immediately; type + fields are then chosen in the side panel (no dialog, so
  // the annotator can keep flipping pages while filling it in)
  addBlock(draft.box, draft.refs);
  updateModeBanner();
  // Focus the panel, not the <select>: a focused form field swallows the keyboard shortcuts, which
  // looked like "framing stopped working" right after creating a box.
  const card = els.blockPopover;
  if (card) {
    card.setAttribute("tabindex", "-1");
    card.focus({preventScroll: true});
  }
}

function discardDraft() {
  // Cancelling leaves framing mode. Keeping it armed looked like the app "forgot" how to draw: the
  // next B press toggled framing off, so dragging produced nothing.
  const hadDraft = Boolean(ann.draft);
  exitCreateMode();
  toast(hadDraft ? "已取消框选，退出框选模式（按 B 重新开始）" : "已退出框选模式");
}

function startLinkPick(direction) {
  const block = selectedAnnotationBlock();
  if (!block) return;
  const movement = direction === "prev" ? -1 : 1;
  ann.linkPick = {origin: {sampleId: state.detail.sample_id, page: state.detail.page, blockId: block.block_id}, direction};
  switchTab("blocks");
  flushAnnotations().then((saved) => {
    if (!saved) { ann.linkPick = null; return; }
    renderCurrentLayer();
    updateModeBanner();
    navigatePage(movement);
  });
}

function cancelLinkPick() {
  if (!ann.linkPick) return;
  ann.linkPick = null;
  renderCurrentLayer();
  updateModeBanner();
  toast("已取消跨页链接");
}

/** Called when a link pick is armed and the annotator clicks a block on the neighbour page. */
function applyLinkPick(targetBlockId) {
  const pick = ann.linkPick;
  if (!pick) return;
  const movement = pick.direction === "prev" ? -1 : 1;
  ann.pendingLink = {...pick, targetBlockId};
  ann.linkPick = null;
  navigatePage(-movement);   // back to the origin page; selectPage applies pendingLink after loading
}

/** Undo the mirror link on the previous page when a text link is retracted. */
function clearReciprocalLink(block, previousBlockId) {
  const previousPage = state.detail.page - 1;
  if (previousPage < 1) return;
  const sampleId = state.detail.sample_id;
  requestJson(`/api/annotations/${encodeURIComponent(sampleId)}/${previousPage}?t=${Date.now()}`)
    .then((patch) => patch.exists && patch.blocks?.length
      ? patch.blocks
      : requestJson(`/api/page/${encodeURIComponent(sampleId)}/${previousPage}?t=${Date.now()}`).then((page) => page.blocks))
    .then((blocks) => {
      const target = blocks.find((entry) => entry.block_id === previousBlockId);
      if (!target || !target.cross_page_text) return null;
      target.cross_page_text = {...target.cross_page_text, continued_by_block_id: null, status: "PASS", method: "manual_annotation_link"};
      return requestJson(`/api/annotations/${encodeURIComponent(sampleId)}/${previousPage}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({annotator: ann.annotator, blocks, operations: [{kind: "update", block_id: previousBlockId, fields: ["cross_page_text.continued_by_block_id"], note: "retracted by the next page"}]}),
      });
    })
    .then((saved) => { if (saved) toast("上一页的反向链接已清除"); })
    .catch(() => {});
}

function writeReciprocalLink(pick, targetBlockId, originBlockId) {
  // The neighbour page gets the mirrored field so both pages agree. If that page has no annotation
  // yet, the patch is built on top of its chain revision (the link is the only change).
  const neighbourPage = pick.origin.page + (pick.direction === "prev" ? -1 : 1);
  const sampleId = pick.origin.sampleId;
  const reverseField = pick.direction === "prev" ? "continued_by_block_id" : "continues_block_id";
  requestJson(`/api/annotations/${encodeURIComponent(sampleId)}/${neighbourPage}?t=${Date.now()}`)
    .then((patch) => patch.exists && patch.blocks?.length
      ? patch.blocks
      : requestJson(`/api/page/${encodeURIComponent(sampleId)}/${neighbourPage}?t=${Date.now()}`).then((page) => page.blocks))
    .then((blocks) => {
      const target = blocks.find((block) => block.block_id === targetBlockId);
      if (!target) return null;
      target.cross_page_text = {...(target.cross_page_text || {}), [reverseField]: originBlockId, status: "CHECK", method: "manual_annotation_link"};
      return requestJson(`/api/annotations/${encodeURIComponent(sampleId)}/${neighbourPage}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          annotator: ann.annotator,
          blocks,
          operations: [{kind: "update", block_id: targetBlockId, fields: [`cross_page_text.${reverseField}`], note: `reciprocal of ${originBlockId}`}],
        }),
      });
    })
    .then((saved) => { if (saved) toast("相邻页反向链接已同步"); })
    .catch((error) => toast(`反向链接同步失败：${error.message}`, true));
}

function startOrderPick() {
  const block = selectedAnnotationBlock();
  if (!block) {
    toast("先选中要移动的 block，再点「插到某块之后」", true);
    return;
  }
  ann.orderPick = {blockId: block.block_id, hoverId: null};
  switchTab("blocks");
  renderCurrentLayer();
  updateModeBanner();
}

function cancelOrderPick() {
  if (!ann.orderPick) return;
  ann.orderPick = null;
  renderCurrentLayer();
  updateModeBanner();
}

/** Move the picked block to sit immediately after the clicked block (the annotator points at the
 *  predecessor, which is how people actually think about order). */
function applyOrderPick(targetBlockId) {
  const pick = ann.orderPick;
  if (!pick) return;
  const sourceIndex = ann.blocks.findIndex((block) => block.block_id === pick.blockId);
  const targetIndex = ann.blocks.findIndex((block) => block.block_id === targetBlockId);
  ann.orderPick = null;
  updateModeBanner();
  if (sourceIndex < 0 || targetIndex < 0 || targetBlockId === pick.blockId) {
    renderCurrentLayer();
    return;
  }
  const insertAfter = targetIndex + 1;
  const finalIndex = insertAfter > sourceIndex ? insertAfter - 1 : insertAfter;
  moveBlockTo(pick.blockId, finalIndex);
  toast(`已插到第 ${finalIndex + 1} 位`);
}

function updateModeBanner() {
  if (els.blockPopover && els.blockPopover.dataset.blockId) renderBlockPopover();
  if (!els.pickBanner) return;
  if (ann.orderPick) {
    const block = ann.blocks.find((entry) => entry.block_id === ann.orderPick.blockId);
    const target = ann.orderPick.hoverId ? ann.blocks.find((entry) => entry.block_id === ann.orderPick.hoverId) : null;
    const targetIndex = target ? ann.blocks.findIndex((entry) => entry.block_id === target.block_id) + 1 : null;
    els.pickBanner.hidden = false;
    els.pickBannerTitle.textContent = "顺序拾取";
    els.pickBannerText.textContent = target
      ? `把「${block ? block.type : "?"}」插到「${target.type}（第 ${targetIndex} 位）」之后`
      : `正在搬动「${block ? block.type : "?"}」：点击要排在它前面的 block，或点空白处取消`;
    updateDraftActions();
    return;
  }
  if (ann.linkPick) {
    const step = ann.linkPick.direction === "prev" ? "上一页" : "下一页";
    els.pickBanner.hidden = false;
    els.pickBannerTitle.textContent = "跨页链接";
    els.pickBannerText.textContent = ann.linkPick.hoverId
      ? "点击该 block 建立跨页链接"
      : `在${step}点击要承接/被承接的那个 block（Esc 取消）`;
    updateDraftActions();
    return;
  }
  if (ann.createMode) {
    els.pickBanner.hidden = false;
    els.pickBannerTitle.textContent = "框选新建";
    els.pickBannerText.textContent = ann.draft
      ? "拖动框可移动、拖角可缩放，确认后到右侧选类型与字段"
      : "在页面上拖拽出目标区域（Esc 退出）";
    updateDraftActions();
    return;
  }
  els.pickBanner.hidden = true;
  updateDraftActions();
}

function updateDraftActions() {
  if (!els.draftActions) return;
  const draft = ann.draft;
  els.draftActions.hidden = !(ann.createMode && draft);
  if (!draft) return;
  const rect = els.overlay.getBoundingClientRect();
  const viewBox = els.overlay.viewBox.baseVal;
  if (!viewBox.width) return;
  const scaleX = rect.width / viewBox.width;
  const scaleY = rect.height / viewBox.height;
  const left = rect.left + draft.box[2] * scaleX + 8;
  const top = rect.top + draft.box[1] * scaleY;
  els.draftActions.style.left = `${Math.max(8, Math.min(left, window.innerWidth - 210))}px`;
  els.draftActions.style.top = `${Math.max(8, top)}px`;
  els.draftSize.textContent = `${Math.round(draft.box[2] - draft.box[0])} × ${Math.round(draft.box[3] - draft.box[1])} px`;
}

function moveBlockInOrder(blockId, delta) {
  const index = ann.blocks.findIndex((item) => item.block_id === blockId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= ann.blocks.length) return;
  pushUndo();
  const [block] = ann.blocks.splice(index, 1);
  ann.blocks.splice(target, 0, block);
  renumber();
  recordOp("move", blockId, ["reading_order"]);
  renderCurrentLayer();
}

function duplicateBlock(blockId) {
  const block = ann.blocks.find((item) => item.block_id === blockId);
  if (!block) return;
  pushUndo();
  const copy = {...cloneValue(block), block_id: newBlockId(), _state: "added"};
  copy.bbox_image_px = [block.bbox_image_px[0] + 8, block.bbox_image_px[1] + 8, block.bbox_image_px[2] + 8, block.bbox_image_px[3] + 8];
  ann.blocks.push(copy);
  ann.selectedId = copy.block_id;
  state.selectedKey = `ann:${copy.block_id}`;
  recordOp("add", copy.block_id, ["duplicate"]);
  renderCurrentLayer();
}

function revertToChain(blockId) {
  const original = (state.detail.blocks || []).find((item) => item.block_id === blockId);
  if (!original) return;
  pushUndo();
  const index = ann.blocks.findIndex((item) => item.block_id === blockId);
  ann.blocks[index] = {...cloneValue(original), _state: "chain"};
  ann.selectedId = blockId;
  state.selectedKey = `ann:${blockId}`;
  recordOp("update", blockId, ["revert"]);
  renderCurrentLayer();
}

/* ---- snapping: a hand-drawn box should anchor on the PDF evidence it covers ---- */
function evidenceItems() {
  return (state.detail?.evidence?.pdf || []).filter((item) => Array.isArray(item.bbox_image_px) && (item.kind === "pdf_line" || item.kind === "pdf_image"));
}

function snapBox(box) {
  if (!ann.snap) return {box, refs: []};
  const covered = evidenceItems().filter((item) => ioa(item.bbox_image_px, box) >= 0.6);
  if (!covered.length) return {box, refs: []};
  const union = covered.reduce((acc, item) => [
    Math.min(acc[0], item.bbox_image_px[0]), Math.min(acc[1], item.bbox_image_px[1]),
    Math.max(acc[2], item.bbox_image_px[2]), Math.max(acc[3], item.bbox_image_px[3]),
  ], box);
  return {box: union, refs: covered.map((item) => item.evidence_id)};
}

function evidenceLinesInside(box) {
  return evidenceInside(box)
    .filter((item) => item.kind === "pdf_line" && String(item.text || "").trim())
    .sort((a, b) => a.bbox_image_px[1] - b.bbox_image_px[1] || a.bbox_image_px[0] - b.bbox_image_px[0]);
}

/** Plain text of the drawn box: this is what the annotator would otherwise retype by hand. */
function evidenceTextInside(box) {
  return evidenceLinesInside(box).map((item) => String(item.text || "").trim()).join("\n");
}

/** Page text in reading order (PDF lines first, model text as fallback), for copy/paste work. */
function pagePlainText() {
  const lines = (state.detail?.evidence?.pdf || [])
    .filter((item) => item.kind === "pdf_line" && String(item.text || "").trim())
    .sort((a, b) => a.bbox_image_px[1] - b.bbox_image_px[1] || a.bbox_image_px[0] - b.bbox_image_px[0]);
  if (lines.length) return lines.map((item) => String(item.text || "").trim()).join("\n");
  return (state.detail?.blocks || [])
    .filter((block) => typeof block.text === "string" && block.text.trim())
    .map((block) => String(block.text).trim())
    .join("\n");
}

const escapeHtmlText = (value) => String(value ?? "").replace(/[&<>"]/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;"}[char]));

/** Rough table draft from the PDF lines inside the box: rows by y, cells by x gaps. */
function tableDraftFromEvidence(box) {
  const lines = evidenceLinesInside(box);
  if (lines.length < 2) return "";
  const rows = [];
  for (const line of lines) {
    const centre = (line.bbox_image_px[1] + line.bbox_image_px[3]) / 2;
    const height = Math.max(4, line.bbox_image_px[3] - line.bbox_image_px[1]);
    const row = rows.find((candidate) => Math.abs(candidate.centre - centre) <= height * 0.5);
    if (row) row.items.push(line);
    else rows.push({centre, items: [line]});
  }
  if (rows.length < 2) return "";
  const cells = rows.map((row) => {
    const items = row.items.sort((a, b) => a.bbox_image_px[0] - b.bbox_image_px[0]);
    const grouped = [];
    let current = null;
    for (const item of items) {
      const width = Math.max(6, item.bbox_image_px[2] - item.bbox_image_px[0]);
      if (current && item.bbox_image_px[0] - current.right > width * 0.8) current = null;
      if (!current) {
        current = {text: String(item.text || "").trim(), right: item.bbox_image_px[2]};
        grouped.push(current);
      } else {
        current.text = `${current.text} ${String(item.text || "").trim()}`.trim();
        current.right = Math.max(current.right, item.bbox_image_px[2]);
      }
    }
    return grouped.map((cell) => cell.text);
  });
  return `<table>${cells.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtmlText(cell)}</td>`).join("")}</tr>`).join("")}</table>`;
}

function copyToClipboard(text, label = "已复制") {
  const value = String(text ?? "");
  if (!value) { toast("没有可复制的内容", true); return; }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(value).then(() => toast(`${label}（${value.length} 字）`)).catch(() => fallbackCopy(value, label));
  } else {
    fallbackCopy(value, label);
  }
}

function fallbackCopy(value, label) {
  const helper = document.createElement("textarea");
  helper.value = value;
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.append(helper);
  helper.select();
  try {
    document.execCommand("copy");
    toast(`${label}（${value.length} 字）`);
  } catch (error) {
    toast("浏览器拒绝复制，请手动全选复制", true);
  }
  helper.remove();
}

function evidenceInside(box) {
  return evidenceItems().filter((item) => ioa(item.bbox_image_px, box) >= 0.6);
}

/* ---- overlay rendering ---- */
function renderAnnotationOverlay(width, height) {
  const parts = [`<rect class="overlay-surface" x="0" y="0" width="${width}" height="${height}" fill="transparent"/>`];
  const picking = ann.orderPick;
  if (picking || ann.createMode || ann.linkPick) parts.push(`<rect class="overlay-dim" x="0" y="0" width="${width}" height="${height}"/>`);
  const ordered = annotationItems();
  const draftBox = ann.createMode ? ann.draft : null;
  const handleSize = Math.max(6, Math.min(width, height) * 0.012);
  if (ann.showFlow && ordered.length > 1) {
    parts.push(flowMarkup(ordered.map((item) => [(item.bbox_image_px[0] + item.bbox_image_px[2]) / 2, (item.bbox_image_px[1] + item.bbox_image_px[3]) / 2]), width, height));
  }
  for (const item of ordered) {
    const [x1, y1, x2, y2] = item.bbox_image_px;
    const color = colorOf(item._type);
    const selected = item.block_id === ann.selectedId;
    // Every block in the current multi-selection is highlighted, not only the active one: the group is
    // what the annotator is acting on (merge / delete / reorder), so it has to be visible as a group.
    const inMulti = ann.multi.includes(item.block_id);
    const labelText = `${item._type}${item.level ? ` L${item.level}` : ""}`;
    const label = els.labelToggle.checked
      ? overlayChip({x1, x2, y1, text: labelText, color, unit: overlayUnit(width), state: item._state, selected: selected || inMulti, pageWidth: width, pageHeight: height})
      : "";
    let pickClass = "";
    let pickTag = "";
    if (ann.linkPick) {
      const isTarget = ann.linkPick.hoverId === item.block_id;
      pickClass = isTarget ? "pick-target" : "pick-idle";
      if (isTarget) pickTag = `<text class="overlay-pick-tag" x="${x1 + 3}" y="${Math.max(12, y1 - 5)}">点击选择它为跨页链接</text>`;
    } else if (picking) {
      const isMoving = picking.blockId === item.block_id;
      const isTarget = picking.hoverId === item.block_id;
      pickClass = isMoving ? "pick-moving" : isTarget ? "pick-target" : "pick-idle";
      if (isTarget) pickTag = `<text class="overlay-pick-tag" x="${x1 + 3}" y="${Math.max(12, y1 - 5)}">插到它后面（第 ${item._index + 2} 位）</text>`;
      if (isMoving) pickTag = `<text class="overlay-pick-tag moving" x="${x1 + 3}" y="${Math.max(12, y1 - 5)}">搬动中</text>`;
    }
    const handles = selected && !draftBox
      ? [[x1, y1, "nwse"], [x2, y1, "nesw"], [x1, y2, "nesw"], [x2, y2, "nwse"]]
          .map(([hx, hy, cursor]) => `<rect class="overlay-handle" data-handle="${cursor}" x="${hx - handleSize / 2}" y="${hy - handleSize / 2}" width="${handleSize}" height="${handleSize}"/>`)
          .join("")
      : "";
    const crossMark = crossPageMarkup(item, item.bbox_image_px, overlayUnit(width), width, height);
    parts.push(crossMark);
    parts.push(`<g class="overlay-node" data-block-id="${esc(item.block_id)}"><rect class="overlay-block edit-${esc(item._state)} ${selected ? "edit-selected" : ""} ${inMulti && !selected ? "multi-selected" : ""} ${pickClass}" style="--c:${color}" x="${x1}" y="${y1}" width="${Math.max(0, x2 - x1)}" height="${Math.max(0, y2 - y1)}" stroke="${color}" fill="${colorAlpha(item._type, selected ? 0.22 : inMulti ? 0.20 : 0.10)}"/>${label}${pickTag}${handles}</g>`);
  }
  // Deleted blocks are intentionally not drawn: the canvas shows the annotated state only.
  // They stay reachable in the 「标注」 panel, where they can be restored.
  if (ann.multi.length >= 2) {
    for (const block of ann.blocks) {
      if (!ann.multi.includes(block.block_id)) continue;
      const [x1, y1, x2, y2] = block.bbox_image_px;
      parts.push(`<rect class="overlay-multi" x="${x1}" y="${y1}" width="${Math.max(0, x2 - x1)}" height="${Math.max(0, y2 - y1)}"/>`);
    }
  }
  if (ann.band) {
    const box = normaliseBox([ann.band.start.x, ann.band.start.y, ann.band.current.x, ann.band.current.y]);
    parts.push(`<rect class="overlay-band" x="${box[0]}" y="${box[1]}" width="${Math.max(0, box[2] - box[0])}" height="${Math.max(0, box[3] - box[1])}"/>`);
  }
  if (ann.snapPreview) {
    const [x1, y1, x2, y2] = ann.snapPreview;
    parts.push(`<rect class="overlay-snap" x="${x1}" y="${y1}" width="${Math.max(0, x2 - x1)}" height="${Math.max(0, y2 - y1)}"/>`);
  }
  if (ann.drawing) {
    const [x1, y1, x2, y2] = ann.drawing;
    parts.push(`<rect class="overlay-draw" x="${x1}" y="${y1}" width="${Math.max(0, x2 - x1)}" height="${Math.max(0, y2 - y1)}"/>`);
  }
  const draft = draftBox;
  if (draft) {
    const [x1, y1, x2, y2] = draft.box;
    const size = Math.max(7, Math.min(width, height) * 0.014);
    const handles = [[x1, y1], [x2, y1], [x1, y2], [x2, y2]]
      .map(([hx, hy]) => `<rect class="overlay-handle" x="${hx - size / 2}" y="${hy - size / 2}" width="${size}" height="${size}"/>`)
      .join("");
    parts.push(`<g class="overlay-draft-node"><rect class="overlay-draft" x="${x1}" y="${y1}" width="${Math.max(0, x2 - x1)}" height="${Math.max(0, y2 - y1)}"/>${handles}</g>`);
  }
  return parts.join("");
}

function cssEscapeId(value) {
  return window.CSS && window.CSS.escape ? window.CSS.escape(String(value)) : String(value).replace(/["\\]/g, "\\$&");
}

/** Move one block's SVG node in place. Returns false when the node is missing (caller re-renders). */
function syncBlockNode(blockId) {
  positionBlockPopover(false);
  const block = ann.blocks.find((entry) => entry.block_id === blockId);
  const group = els.overlay.querySelector(`g.overlay-node[data-block-id="${cssEscapeId(blockId)}"]`);
  if (!block || !group) return false;
  const [x1, y1, x2, y2] = block.bbox_image_px;
  const rect = group.querySelector("rect.overlay-block");
  if (!rect) return false;
  rect.setAttribute("x", x1);
  rect.setAttribute("y", y1);
  rect.setAttribute("width", Math.max(0, x2 - x1));
  rect.setAttribute("height", Math.max(0, y2 - y1));
  const label = group.querySelector("text.overlay-label");
  if (label) {
    label.setAttribute("x", x1 + 3);
    label.setAttribute("y", Math.max(12, y1 - 4));
  }
  const chip = group.querySelector("rect.overlay-order-chip");
  if (chip) {
    chip.setAttribute("x", x1);
    chip.setAttribute("y", y1);
    const chipText = group.querySelector("text.overlay-order-text");
    if (chipText) {
      chipText.setAttribute("x", x1 + 4);
      chipText.setAttribute("y", y1 + 11);
    }
  }
  const tag = group.querySelector("text.overlay-pick-tag");
  if (tag) {
    tag.setAttribute("x", x1 + 3);
    tag.setAttribute("y", Math.max(12, y1 - 5));
  }
  const handles = group.querySelectorAll("rect.overlay-handle");
  if (handles.length === 4) {
    const size = Number(handles[0].getAttribute("width")) || 8;
    [[x1, y1], [x2, y1], [x1, y2], [x2, y2]].forEach(([hx, hy], index) => {
      handles[index].setAttribute("x", hx - size / 2);
      handles[index].setAttribute("y", hy - size / 2);
    });
  }
  return true;
}

/** Same idea for the draft box that is being framed or adjusted. */
function syncDraftNode() {
  const group = els.overlay.querySelector("g.overlay-draft-node");
  if (!group || !ann.draft) return false;
  const [x1, y1, x2, y2] = ann.draft.box;
  const rect = group.querySelector("rect.overlay-draft");
  rect.setAttribute("x", x1);
  rect.setAttribute("y", y1);
  rect.setAttribute("width", Math.max(0, x2 - x1));
  rect.setAttribute("height", Math.max(0, y2 - y1));
  const handles = group.querySelectorAll("rect.overlay-handle");
  if (handles.length === 4) {
    const size = Number(handles[0].getAttribute("width")) || 8;
    [[x1, y1], [x2, y1], [x1, y2], [x2, y2]].forEach(([hx, hy], index) => {
      handles[index].setAttribute("x", hx - size / 2);
      handles[index].setAttribute("y", hy - size / 2);
    });
  }
  return true;
}

/** The live drawing rectangle is a single rect: no need to rebuild the layer per pointermove. */
function syncDrawingNode(x1, y1, x2, y2) {
  const rect = els.overlay.querySelector("rect.overlay-draw");
  if (!rect) return false;
  rect.setAttribute("x", x1);
  rect.setAttribute("y", y1);
  rect.setAttribute("width", Math.max(0, x2 - x1));
  rect.setAttribute("height", Math.max(0, y2 - y1));
  return true;
}

/** Keep the bbox inputs in step with the canvas without re-rendering the editor while dragging. */
function updateEditorBox() {
  const block = selectedAnnotationBlock();
  if (!block || els.blockPopover.hidden) return;
  const [x1, y1, x2, y2] = block.bbox_image_px;
  for (const [id, value] of [["bboxX1", x1], ["bboxY1", y1], ["bboxX2", x2], ["bboxY2", y2]]) {
    const input = document.getElementById(id);
    if (input && document.activeElement !== input) input.value = Math.round(value * 100) / 100;
  }
}

/** The snap preview is one rect: keep it in place instead of rebuilding the overlay. */
function syncSnapNode() {
  let rect = els.overlay.querySelector("rect.overlay-snap");
  const box = ann.snapPreview;
  if (!box) {
    if (rect) rect.remove();
    return true;
  }
  if (!rect) {
    rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("class", "overlay-snap");
    els.overlay.append(rect);
  }
  rect.setAttribute("x", box[0]);
  rect.setAttribute("y", box[1]);
  rect.setAttribute("width", Math.max(0, box[2] - box[0]));
  rect.setAttribute("height", Math.max(0, box[3] - box[1]));
  return true;
}

/* ---- inline editor anchored to the selected bbox ---------------------------------------------

/** Flat, label-driven rows: the fields relevant to this block type are always visible. */
function cardRow(label, body, extra = "") {
  return `<div class="card-row${extra}"><span class="card-label">${esc(label)}</span><span class="card-content">${body}</span></div>`;
}


/** Parse the block HTML into a live <table> element (null when the markup has no table). */
function parseBlockTable(html) {
  const source = String(html || "");
  if (!source.includes("<table")) return null;
  const doc = new DOMParser().parseFromString(source, "text/html");
  return doc.querySelector("table");
}

/** Serialise the edited table back to clean HTML (drop the editing affordances). */
function serialiseBlockTable(tableEl) {
  const clone = tableEl.cloneNode(true);
  clone.querySelectorAll("[contenteditable]").forEach((cell) => cell.removeAttribute("contenteditable"));
  clone.querySelectorAll(".cell-edit, .cell-active").forEach((cell) => cell.classList.remove("cell-edit", "cell-active"));
  // the visible image is a preview of the page crop: store the reference again, not the preview
  clone.querySelectorAll(".cell-image[data-image-ref]").forEach((node) => {
    const image = document.createElement("img");
    image.setAttribute("data-image-ref", node.getAttribute("data-image-ref") || "");
    image.setAttribute("alt", "");
    node.replaceWith(image);
  });
  return clone.outerHTML;
}

/** An in-table picture: crop it out of the page image so the annotator can actually see it.
 *  Kept non-editable inside the grid and converted back to <img data-image-ref> on save. */
function cellImageMarkup(ref) {
  const item = (state.detail?.evidence?.pdf || []).find((entry) => entry.evidence_id === ref);
  const box = item?.bbox_image_px;
  const page = state.detail?.page_image;
  if (!box || !page) {
    return `<span class="cell-image token" contenteditable="false" data-image-ref="${esc(ref)}" title="${esc(ref)}">▧ 图</span>`;
  }
  const naturalWidth = Math.max(1, box[2] - box[0]);
  const naturalHeight = Math.max(1, box[3] - box[1]);
  const scale = Math.min(1, 170 / naturalWidth, 90 / naturalHeight);
  const width = Math.max(18, Math.round(naturalWidth * scale));
  const height = Math.max(14, Math.round(naturalHeight * scale));
  const url = `/api/image/${encodeURIComponent(state.detail.sample_id)}/${state.detail.page}/source`;
  return `<span class="cell-image" contenteditable="false" data-image-ref="${esc(ref)}" title="${esc(ref)} · ${Math.round(naturalWidth)}×${Math.round(naturalHeight)}px" style="width:${width}px;height:${height}px;background-image:url('${url}');background-size:${page.width}px ${page.height}px;background-position:-${Math.round(box[0] * scale * (page.width / Math.max(1, naturalWidth)))}px -${Math.round(box[1] * scale * (page.height / Math.max(1, naturalHeight)))}px;background-size:${Math.round(page.width * scale * (page.width / Math.max(1, naturalWidth)))}px auto"></span>`;
}

function tableEditorHtml(block) {
  const table = parseBlockTable(block.html);
  if (!table) {
    return `<div class="editor-warn">当前 HTML 里没有 &lt;table&gt;，可直接编辑下面的源码</div>`;
  }
  table.classList.add("table-editor");
  table.querySelectorAll("img[data-image-ref]").forEach((image) => {
    const ref = image.getAttribute("data-image-ref") || "";
    const holder = document.createElement("span");
    holder.innerHTML = cellImageMarkup(ref);
    image.replaceWith(holder.firstElementChild);
  });
  table.querySelectorAll("td, th").forEach((cell) => {
    cell.setAttribute("contenteditable", "true");
    cell.classList.add("cell-edit");
    cell.setAttribute("spellcheck", "false");
  });
  return `<div class="table-editor-wrap">${table.outerHTML}</div>`;
}

function renderBlockCardBody(block) {
  const textLike = ANNOTATION_TEXT_TYPES.has(block.type);
  const insideText = evidenceTextInside(block.bbox_image_px);
  const insideLines = insideText.split("\n").map((line) => line.trim()).filter(Boolean);
  const warnings = blockFieldIssues(block);
  const parentOptions = ann.blocks
    .filter((entry) => entry.block_id !== block.block_id && (ANNOTATION_MEDIA_TYPES.has(entry.type) || entry.type === "table"))
    .map((entry) => `<option value="${esc(entry.block_id)}" ${entry.block_id === block.parent_id ? "selected" : ""}>${esc(entry.type)} ${esc(shortBlockId(entry.block_id))}</option>`).join("");
  const orderIndex = ann.blocks.findIndex((entry) => entry.block_id === block.block_id);
  const groups = [];

  // --- type -------------------------------------------------------------------------------------
  const typeBody = [
    cardRow("类型", `<div class="type-grid inline">${QUICK_TYPES.map((type) => `<button type="button" data-quick-type="${esc(type)}" class="${block.type === type ? "active" : ""}" style="--c:${colorOf(type)}" title="切换为 ${esc(type)}（也可按 ${QUICK_TYPES.indexOf(type) + 1 <= 9 ? QUICK_TYPES.indexOf(type) + 1 : "…"}）">${esc(type)}</button>`).join("")}</div>`),
  ];
  if (block.type === "title") {
    typeBody.push(cardRow("层级", `<span class="level-buttons">${[1, 2, 3, 4, 5, 6].map((level) => `<button type="button" data-level="${level}" class="${block.level === level ? "active" : ""}">L${level}</button>`).join("")}</span>`));
  }
  if (ANNOTATION_CAPTION_TYPES.has(block.type)) {
    typeBody.push(cardRow("所属", `<select id="popParent"><option value="">（未选择）</option>${parentOptions}</select>`));
  }
  if (warnings.length) typeBody.push(`<div class="editor-warn">${warnings.map((warning) => esc(warning)).join(" · ")}</div>`);
  groups.push(`<div class="card-group">${typeBody.join("")}</div>`);

  // --- content ----------------------------------------------------------------------------------
  const contentBody = [];
  if (textLike) {
    // Two boxes, deliberately different colours and different wording:
    //   * reference (top) - what the PDF text layer says, read-only, cold tint;
    //   * actual content (below) - what the annotator writes into the GT, accented, with its own hint
    //     and the copy button right underneath it.
    contentBody.push(cardRow("参考文本", `<span class="card-hint">PDF 原文 · 只读 · ${insideLines.length} 行</span>`));
    contentBody.push(`<div class="reference-box ${insideLines.length ? "" : "empty"}">
      <div class="reference-text" role="textbox" aria-readonly="true">${esc(insideText || "（框内没有 PDF 文字行，请直接按页面抄录）")}</div>
    </div>`);
    contentBody.push(cardRow("实际内容", `<span class="card-hint">写入 GT 的文字 · ${String(block.text || "").length} 字</span><span class="card-spacer"></span>
      <span class="content-flag ${String(block.text || "").trim() ? "ok" : "todo"}">${String(block.text || "").trim() ? "已填" : "待填"}</span>`));
    contentBody.push(`<textarea id="popText" class="content-text" rows="3" placeholder="在此填写框内的实际文字（以页面为准，不要照抄参考文本里的乱码）">${esc(block.text || "")}</textarea>`);
    contentBody.push(`<div class="content-actions">
      <button type="button" class="mini primary" data-action="fill-inside" title="把上面的「参考文本」写入实际内容，再逐字核对">复制参考文本到该框</button>
      <button type="button" class="mini" data-action="clear-text" title="清空实际内容">清空</button>
      <span class="card-hint">提示：实际内容才是导出的 GT，参考文本只用于核对。</span>
    </div>`);
  }
  if (block.type === "table") {
    const stats = tableStats(block.html || "");
    contentBody.push(cardRow("表格", `<span class="card-hint">${esc(stats)}</span><span class="card-spacer"></span>
      <button type="button" class="mini" data-action="copy-inside" title="复制本框 HTML 到剪贴板">复制 HTML</button>`));
    contentBody.push(tableEditorHtml(block));
    contentBody.push(`<details class="raw-html-fold"><summary>原始 HTML</summary>
      <textarea id="popHtml" rows="4" placeholder="<table>…</table>">${esc(block.html || "")}</textarea>
      <div class="card-hint">改这里会覆盖上面的表格内容</div></details>`);
  }
  if (contentBody.length) groups.push(`<div class="card-group">${contentBody.join("")}</div>`);

  // --- cross page (above the order block: it is the field annotators check most) ------------------
  const crossBlock = block.type === "text" || block.type === "table"
    ? `<div class="card-group cross-group" style="--c:${colorOf(block.type)}">${renderCrossPageSection(block)}</div>`
    : "";
  groups.push(crossBlock);
  // --- order ------------------------------------------------------------------------------------
  const columnInfo = detectPageColumns(ann.blocks);
  // The pipeline measured this page from geometry *and* cross-checked it against two parsers, so its
  // verdict beats the viewer's own heuristic (the heuristic sees only the current blocks).
  const measuredColumns = Number(state.detail?.reading_order?.columns || 0);
  const pageColumns = measuredColumns > 1 ? measuredColumns : columnInfo.columns;
  const effectiveColumns = {...columnInfo, columns: pageColumns};
  const interleaveCount = countColumnInterleaving(ann.blocks, effectiveColumns);
  groups.push(`<div class="card-group order-group">
    ${cardRow("顺序", `<span class="order-badge">第 ${orderIndex + 1} / ${ann.blocks.length} 个</span>
      <span class="order-buttons">
        <button type="button" data-action="order-first" title="置顶">⤒</button>
        <button type="button" data-action="order-up" title="前移一位 ([)">↑</button>
        <button type="button" data-action="order-down" title="后移一位 (])">↓</button>
        <button type="button" data-action="order-last" title="置底">⤓</button>
      </span>`)}
    <div class="order-actions">
      <button type="button" class="mini" data-action="order-pick">插到某块之后…</button>
      <button type="button" class="mini ${columnInfo.columns > 1 ? "primary" : ""}" data-action="auto-sort-columns" title="${columnInfo.columns > 1 ? `按检测到的 ${columnInfo.columns} 栏顺序重排（左栏到底 → 右栏）` : "本页未检测到分栏，按位置重排即可"}">${columnInfo.columns > 1 ? "按栏重排" : "按位置重排"}</button>
      ${columnInfo.columns > 1 ? '<button type="button" class="mini" data-action="auto-sort" title="忽略分栏，单纯按位置（上→下、左→右）">按位置重排</button>' : ""}
    </div>
    ${columnInfo.columns > 1 ? `<div class="order-note ${interleaveCount ? "warn" : ""}">${interleaveCount ? `⚠ 顺序疑似跨栏交错 ${interleaveCount} 处，建议点「按栏重排」` : `本页检测到 ${columnInfo.columns} 栏（阅读顺序：左栏到底 → 右栏）`}</div>` : (measuredColumns === 1 ? '<div class="order-note">本页经流水线判定为单栏（上→下、同行左→右）</div>' : "")}
    ${columnInfo.columns <= 1 ? '<div class="order-note">本页按单栏顺序：上→下、同行左→右</div>' : ""}
  </div>`);

  return groups.join("");
}

/** Push model values back into the open card without rebuilding it (keeps the caret). */
function syncCardFields(block = selectedAnnotationBlock()) {
  if (!block || els.blockPopover.hidden) return;
  const text = document.getElementById("popText");
  if (text && text.value !== (block.text || "")) text.value = block.text || "";
  const html = document.getElementById("popHtml");
  if (html && html.value !== (block.html || "")) html.value = block.html || "";
  const table = els.blockPopover.querySelector("table.table-editor");
  if (table && block.type === "table" && serialiseBlockTable(table) !== String(block.html || "").replace(/\s+/g, " ").trim()) {
    renderBlockPopover(true);
  }
  const bbox = document.querySelectorAll("#blockPopover .bbox-grid input");
  if (bbox.length === 4) {
    block.bbox_image_px.forEach((value, index) => {
      if (document.activeElement !== bbox[index]) bbox[index].value = Math.round(value * 100) / 100;
    });
  }
}

/** Multi-selection card: what is selected, and the one action that matters (merge). */
/** Reorder the selected blocks among the slots they already occupy.
 *
 *  Only the relative order of the selection changes; every other block keeps its position, so the
 *  annotator can fix a local reading-order problem without touching the rest of the page. */
function reorderSelection(orderedBlocks, method) {
  const ids = ann.multi.filter((id) => orderedBlocks.some((block) => block.block_id === id));
  if (ids.length < 2) return;
  const slots = ann.blocks
    .map((block, index) => (ann.multi.includes(block.block_id) ? index : -1))
    .filter((index) => index >= 0);
  const ordered = orderedBlocks.filter((block) => ann.multi.includes(block.block_id));
  if (ordered.length !== slots.length) return;
  pushUndo();
  const next = [...ann.blocks];
  slots.forEach((slot, position) => { next[slot] = ordered[position]; });
  ann.blocks = next;
  renumber();
  recordOp("move", "selection", ["reading_order", method]);
  renderBlockPopover(true);
  renderCurrentLayer();
  toast(`已按${method === "auto_by_columns_selection" ? "多栏" : "单栏"}重排选中的 ${ordered.length} 个框`);
}

function renderMultiCard() {
  const pop = els.blockPopover;
  const blocks = ann.multi.map((id) => ann.blocks.find((block) => block.block_id === id)).filter(Boolean);
  const counts = {};
  for (const block of blocks) counts[block.type] = (counts[block.type] || 0) + 1;
  const boxes = blocks.map((block) => block.bbox_image_px);
  const width = Math.round(Math.max(...boxes.map((box) => box[2])) - Math.min(...boxes.map((box) => box[0])));
  const height = Math.round(Math.max(...boxes.map((box) => box[3])) - Math.min(...boxes.map((box) => box[1])));
  const pageColumns = detectPageColumns(ann.blocks);
  const groupOrder = blocks
    .map((block) => ({block, index: ann.blocks.findIndex((entry) => entry.block_id === block.block_id)}))
    .sort((a, b) => a.index - b.index);
  const preview = groupOrder.map((entry) => `#${entry.index + 1}`).join(" → ");
  const columnPreview = orderBlocksByColumns(blocks, pageColumns)
    .map((block) => `#${ann.blocks.findIndex((entry) => entry.block_id === block.block_id) + 1}`).join(" → ");
  const singlePreview = orderBlocksByPosition(blocks)
    .map((block) => `#${ann.blocks.findIndex((entry) => entry.block_id === block.block_id) + 1}`).join(" → ");
  pop.innerHTML = `
    <div class="pop-head" style="--c:${colorOf(blocks[0]?.type || "default")}">
      <span class="pop-type" style="--c:${colorOf(blocks[0]?.type || "default")}">已选 ${blocks.length} 个框</span>
      <span class="pop-id">合并后 ${width} × ${height} px</span>
      <button type="button" class="pop-icon" data-pop="close" title="取消选择（Esc）">✕</button>
    </div>
    <div class="pop-scroll"><div class="card-group">
      ${cardRow("类型", `<span class="card-hint">${esc(Object.entries(counts).map(([type, n]) => `${type}${n > 1 ? ` ×${n}` : ""}`).join(" · "))}</span>`)}
      ${cardRow("统一类型", `<div class="type-grid inline">${ALL_BLOCK_TYPES.map((type) => `<button type="button" data-multi-type="${esc(type)}" style="--c:${colorOf(type)}" title="把选中的 ${blocks.length} 个框都改成 ${esc(type)}">${esc(type)}</button>`).join("")}</div>`)}
      <div class="card-hint">改类型会清空该类型的字段（需要重新填写）；合并：外框取并集，证据与文本合并，表格按行拼接并去掉重复表头。</div>
    </div>
    <div class="card-group order-group">
      ${cardRow("选中顺序", `<span class="order-badge">${esc(preview)}</span>`)}
      <div class="order-actions">
        <button type="button" class="mini ${pageColumns.columns <= 1 ? "primary" : ""}" data-action="multi-sort-single" title="把选中的框按单栏顺序重排（上→下、同行左→右），只动这几个框的顺序">按单栏重排</button>
        <button type="button" class="mini ${pageColumns.columns > 1 ? "primary" : ""}" data-action="multi-sort-columns" title="把选中的框按分栏顺序重排（左栏到底 → 右栏），只动这几个框的顺序">按多栏重排</button>
      </div>
      <div class="order-note">
        只在这 ${blocks.length} 个框占用的顺序位置里重排，其它框不受影响。<br>
        单栏：${esc(singlePreview)} · 多栏（本页${pageColumns.columns > 1 ? `检测到 ${pageColumns.columns} 栏` : "未检测到分栏，等同单栏"}）：${esc(columnPreview)}
      </div>
    </div></div>
    <div class="pop-footer">
      <button type="button" data-action="merge">合并为一个框</button>
      <button type="button" class="danger" data-action="multi-delete" title="删除选中的这 ${blocks.length} 个框（Delete）">删除选中的 ${blocks.length} 个框</button>
      <button type="button" data-pop="close">取消选择</button>
    </div>`;
  pop.hidden = false;
  pop.dataset.blockId = "multi";
  positionBlockPopover(true);
}

function renderBlockPopover(force = false) {
  const pop = els.blockPopover;
  if (!pop) return;
  if (state.mode === "annotate" && ann.multi.length >= 2 && !ann.drawing && !ann.band && !ann.createMode && !ann.linkPick && !ann.orderPick) {
    renderMultiCard();
    return;
  }
  const block = popoverTarget();
  if (!block) {
    pop.hidden = true;
    pop.innerHTML = "";
    delete pop.dataset.blockId;
    return;
  }
  // Never rebuild the card while a *field* inside it has the caret: that would drop the cursor.
  // Explicit actions (retype, level, order…) pass force so the fields follow the new type.
  const focused = document.activeElement;
  const typingInCard = pop.contains(focused) && ["INPUT", "TEXTAREA", "SELECT"].includes(focused?.tagName);
  if (!force && pop.dataset.blockId === block.block_id && typingInCard) {
    positionBlockPopover(true);
    return;
  }
  pop.innerHTML = `
    <div class="pop-head" style="--c:${colorOf(block.type)}">
      <span class="pop-type" style="--c:${colorOf(block.type)}">${esc(block.type)}</span>
      <span class="pop-id" title="${esc(block.block_id)} · 拖动标题栏可移动卡片（双击复位）">${esc(shortBlockId(block.block_id))}</span>
      ${block._state && block._state !== "chain" ? `<span class="edit-tag ${esc(block._state)}">${esc(EDIT_LABEL[block._state] || block._state)}</span>` : ""}
      ${ann.cardPinned ? '<button type="button" class="pop-icon" data-pop="unpin" title="卡片回到自动位置">⇱</button>' : ""}
      <button type="button" class="pop-icon" data-pop="close" title="关闭（Esc）">✕</button>
    </div>
    <div class="pop-scroll">${renderBlockCardBody(block)}</div>
    <div class="pop-footer">
      <button type="button" class="danger" data-action="delete" title="删除该框（Delete）">删除该框</button>
      <button type="button" data-action="revert" title="还原为模型给出的原始块">还原</button>
      <button type="button" data-pop="close">完成</button>
    </div>
    ${pageCompletionHtml()}`;
  pop.dataset.blockId = block.block_id;
  pop.hidden = false;
  positionBlockPopover(true);
}

/** Place the card next to the box it belongs to.
 *
 *  Preference order: right of the box, left of it, below, above, and finally docked against the canvas
 *  edge. A candidate is rejected when it would fall outside the canvas column or cover the selected
 *  box itself (covering it made the box impossible to drag).
 */
function positionBlockPopover(remeasure = false) {
  const pop = els.blockPopover;
  const block = popoverTarget();
  if (!pop || pop.hidden || !block) return;
  const rect = els.overlay.getBoundingClientRect();
  const viewBox = els.overlay.viewBox.baseVal;
  if (!viewBox.width || !viewBox.height) return;
  if (remeasure) {
    pop._width = pop.offsetWidth;
    pop._height = pop.offsetHeight;
  }
  const width = pop._width || 380;
  const height = pop._height || 320;
  const scaleX = rect.width / viewBox.width;
  const scaleY = rect.height / viewBox.height;
  const [x1, y1, x2, y2] = block.bbox_image_px;
  const box = {
    left: rect.left + x1 * scaleX,
    top: rect.top + y1 * scaleY,
    right: rect.left + x2 * scaleX,
    bottom: rect.top + y2 * scaleY,
  };
  const canvas = els.canvasScroll ? els.canvasScroll.getBoundingClientRect() : {left: 0, right: window.innerWidth};
  const minLeft = Math.max(8, canvas.left + 8);
  const maxLeft = Math.max(minLeft, Math.min(window.innerWidth, canvas.right) - width - 8);
  const minTop = 8;
  const maxTop = Math.max(minTop, window.innerHeight - height - 10);
  const clampX = (value) => Math.min(Math.max(value, minLeft), maxLeft);
  const clampY = (value) => Math.min(Math.max(value, minTop), maxTop);
  const overlapsBox = (left, top) => !(left + width < box.left - 6 || left > box.right + 6 || top + height < box.top - 6 || top > box.bottom + 6);
  const candidates = [
    {left: box.right + 10, top: clampY(box.top)},                 // right of the box
    {left: box.left - width - 10, top: clampY(box.top)},          // left of the box
    {left: clampX(box.left), top: box.bottom + 10},               // below the box
    {left: clampX(box.left), top: box.top - height - 10},         // above the box
    {left: maxLeft, top: clampY(box.top)},                        // docked at the canvas edge
  ];
  const fits = (candidate) => candidate.left >= minLeft - 0.5 && candidate.left <= maxLeft + 0.5
    && candidate.top >= minTop - 0.5 && candidate.top <= maxTop + 0.5 && !overlapsBox(candidate.left, candidate.top);
  // A card the annotator dragged stays where they put it (double-click the header to reset).
  if (ann.cardPinned) {
    pop.style.left = `${Math.round(clampX(ann.cardPinned.left))}px`;
    pop.style.top = `${Math.round(clampY(ann.cardPinned.top))}px`;
    return;
  }
  const chosen = candidates.find(fits) || candidates[candidates.length - 1];
  pop.style.left = `${Math.round(clampX(chosen.left))}px`;
  pop.style.top = `${Math.round(clampY(chosen.top))}px`;
}

/** The popover shows only while a single box is being edited on the canvas. */
function popoverTarget() {
  if (state.mode !== "annotate") return null;
  if (ann.drawing || ann.draft || ann.createMode || ann.linkPick || ann.orderPick) return null;
  if (!els.overlay || els.overlayImageToggle.checked) return null;
  return selectedAnnotationBlock();
}

/* ---- live field edits: typing counts immediately (the old panel only committed on blur) ---- */
const liveEdit = {blockId: null, fields: new Set(), timer: 0};

function beginLiveEdit(blockId) {
  if (liveEdit.blockId === blockId) return;
  if (liveEdit.blockId) commitLiveEdit();      // close the previous field session first
  liveEdit.blockId = blockId;
  liveEdit.fields = new Set();
  pushUndo();
}

function commitLiveEdit() {
  window.clearTimeout(liveEdit.timer);
  const {blockId, fields} = liveEdit;
  liveEdit.blockId = null;
  liveEdit.fields = new Set();
  if (!blockId || !fields.size) return;
  recordOp("update", blockId, [...fields]);
  markDirty();
  // Full repaint once per edit session; typing itself only touches the nodes that changed.
  renderBlockList();
  renderAnnotationPanel();
  renderDetail();
  renderBlockPopover();
  updateAnnotationChrome();
}

/** One list row: rebuilding the whole list on every keystroke was the expensive part. */
function updateBlockListEntry(blockId) {
  const item = els.blockList.querySelector(`.block-item[data-key="ann:${cssEscapeId(blockId)}"]`);
  const block = ann.blocks.find((entry) => entry.block_id === blockId);
  if (!item || !block) return;
  const text = item.querySelector(".block-text");
  if (text) text.textContent = shortText(block.text || block.html || "") || block.block_id;
}

/** Cheap per-keystroke repaint: geometry, the list row text and the dirty chip only. */
function refreshBlockViews(blockId) {
  // No renderOverlay()/renderDetail()/renderBlockList() here: those replace the DOM the annotator is
  // typing into (and rebuilding ~20 rows per keystroke cost ~5 ms).
  syncBlockNode(blockId);
  positionBlockPopover(false);
  updateBlockListEntry(blockId);
  updateEditorBox();
  updateDirtyChrome();
}

function applyLiveEdit(blockId, patch, field) {
  const block = ann.blocks.find((entry) => entry.block_id === blockId);
  if (!block) return;
  beginLiveEdit(blockId);
  delete block._prefilled;          // the annotator touched this block: the content is now theirs
  Object.assign(block, patch);
  if (block._state === "chain") block._state = "updated";
  ann.editVersion += 1;
  ann.dirty = true;
  liveEdit.fields.add(field);
  window.clearTimeout(liveEdit.timer);
  liveEdit.timer = window.setTimeout(commitLiveEdit, 500);
  refreshBlockViews(blockId);
}

/* ---- editor ---- */
/** True when a table carries at least one non-empty cell - an empty skeleton is not "filled in". */
function tableHasContent(html) {
  const table = parseBlockTable(html);
  if (!table) return false;
  return [...table.querySelectorAll("td, th")].some((cell) => String(cell.textContent || "").trim().length > 0);
}

/** The fields a block must have before it counts as complete: one predicate for the card hints and
 *  for the page progress bar, so the counter always matches what the annotator sees. A field only
 *  counts once the annotator actually put content in it - a placeholder never shows up as complete. */
function blockFieldIssues(block) {
  const issues = [];
  if (ANNOTATION_TEXT_TYPES.has(block.type) && !String(block.text || "").trim()) issues.push("缺少文本");
  if (block.type === "title" && !(block.level >= 1 && block.level <= MAX_TITLE_LEVEL_UI)) issues.push("标题需要 L1–L4");
  if (block.type === "table" && !String(block.html || "").includes("<table")) issues.push("缺少 <table> 的 HTML");
  if (block.type === "table" && String(block.html || "").includes("<table") && !tableHasContent(block.html)) issues.push("表格内容为空");
  if (ANNOTATION_CAPTION_TYPES.has(block.type) && !block.parent_id) issues.push("需要所属块");
  if (block._prefilled) issues.push("待确认预填内容");
  const box = block.bbox_image_px || [];
  if (!(box.length === 4 && box[2] > box[0] && box[3] > box[1])) issues.push("bbox 无效");
  return issues;
}

function pageCompletion() {
  const blocks = ann.blocks;
  const valid = blocks.filter((block) => blockFieldIssues(block).length === 0).length;
  return {valid, total: blocks.length, ratio: blocks.length ? valid / blocks.length : 1};
}

/** Which blocks still have missing fields - shown under the progress bar. */
function pageCompletionDetail() {
  const pending = ann.blocks.filter((block) => blockFieldIssues(block).length);
  if (!pending.length) return "";
  const rows = pending.slice(0, 4).map((block) => `<span class="pending-chip" title="${esc(block.block_id)}">${esc(shortBlockId(block.block_id))} · ${esc(blockFieldIssues(block).join("/"))}</span>`).join("");
  return `<div class="pending-row">${rows}${pending.length > 4 ? `<span class="card-hint">+${pending.length - 4}</span>` : ""}</div>`;
}

function pageCompletionHtml() {
  const {valid, total, ratio} = pageCompletion();
  const ready = valid === total && total > 0;
  return `<div class="progress-block compact">
    <div class="progress-label"><span>本页字段完整度</span><strong class="${ready ? "ok" : "warn"}">${valid}/${total}</strong></div>
    <div class="progress-track"><div class="progress-fill ${ready ? "ok" : "warn"}" style="width:${Math.round(ratio * 100)}%"></div></div>
    ${pageCompletionDetail()}
  </div>`;
}

/** Cross page fields follow docs/CROSS_PAGE_CONTRACT.md: booleans are true/false, null means
 *  "not determined" (which is not the same as false), and every judgement is recorded with the method
 *  that produced it. The card exposes the same shape the pipeline writes. */
/** Cross page judgement, one control for both kinds of block.
 *
 *  The contract has no boolean for text (continuity *is* the link) and the table's geometry flags are
 *  rule evidence, not annotator input, so the human choice is only: continues from the previous page,
 *  or not. Anything untouched keeps the pipeline's value.
 *    continue → continues_block_id=<id>, status=PASS, method=manual_annotation_link
 *    separate → continues_block_id=null,  status=PASS, method=manual_annotation_link
 */
function renderCrossPageSection(block) {
  const isTable = block.type === "table";
  const scope = isTable ? "cross_page_table" : "cross_page_text";
  const field = (isTable ? block.cross_page_table : block.cross_page_text) || {};
  const method = field.method ? `<code class="card-code">${esc(field.method)}</code>` : "";
  const human = String(field.method || "").startsWith("manual");
  const origin = !field.method
    ? '<span class="origin-chip none">未判定</span>'
    : human
      ? '<span class="origin-chip manual">人工判定</span>'
      : '<span class="origin-chip pipeline">模型判定</span>';
  const mode = field.status === "CHECK" ? "uncertain" : field.continues_block_id ? "continue" : field.status === "PASS" ? "separate" : "unset";
  const button = (value, label) => `<button type="button" class="mini cross-btn ${mode === value ? (human ? "active" : "proposed") : ""}" data-cross-mode="${value}" ${ann.linkPick ? "disabled" : ""}>${label}</button>`;
  const linkNote = field.continues_block_id
    ? `承接 <code>${esc(shortBlockId(field.continues_block_id))}</code>`
    : mode === "separate" ? "判定不接"
    : mode === "uncertain" ? "未判定"
    : "未判定过";
  const evidence = isTable
    ? `<div class="cross-evidence">模型判据：页底截断 <b>${field.truncated_at_page_end === true ? "是" : field.truncated_at_page_end === false ? "否" : "未定"}</b> · 页顶续接 <b>${field.truncated_at_page_start === true ? "是" : field.truncated_at_page_start === false ? "否" : "未定"}</b> · 重复表头 <b>${field.repeated_header === true ? "是" : field.repeated_header === false ? "否" : "未定"}</b> · 闭合线 <b>${field.has_closing_rule === true ? "是" : field.has_closing_rule === false ? "否" : "未定"}</b> · 续表 <b>${field.part_index ?? "—"}/${field.part_count ?? "—"}</b></div>`
    : "";
  return `<div class="card-group-title cross-title">跨页 · ${isTable ? "跨页表" : "跨页文本"}${origin}</div>
    <div class="cross-row">
      <span class="seg">${button("continue", "承接上页")}${button("separate", "不接")}</span>
      <span class="card-hint">${linkNote}</span>
      ${field.continued_by_block_id ? `<span class="card-hint">· 下页 <code>${esc(shortBlockId(field.continued_by_block_id))}</code> 承接</span>` : ""}
      ${method}
    </div>
    ${evidence}`;
}



function renderTextPanel() {
  if (!els.textPanel) return;
  if (!state.detail) {
    els.textPanel.innerHTML = "";
    return;
  }
  const lines = (state.detail.evidence.pdf || [])
    .filter((item) => item.kind === "pdf_line" && String(item.text || "").trim())
    .sort((a, b) => a.bbox_image_px[1] - b.bbox_image_px[1] || a.bbox_image_px[0] - b.bbox_image_px[0]);
  const page = pagePlainText();
  els.textPanel.innerHTML = `<div class="text-panel">
    <div class="row">
      <button type="button" data-copy="page">复制整页文本（${page.length} 字）</button>
      <button type="button" data-copy="lines">复制为多行（每行一条）</button>
    </div>
    <textarea class="reference-text" readonly rows="14" id="pageTextArea">${esc(page)}</textarea>
    <div class="section-title">PDF 文字行 · ${lines.length}（点击即可复制）</div>
    <div class="line-list">
      ${lines.map((item, index) => `<button type="button" class="line-item" data-copy-line="${esc(item.text || "")}" title="${esc(item.evidence_id)}">${index + 1}. ${esc(item.text)}</button>`).join("") || '<div class="muted">本页没有 PDF 文字行</div>'}
    </div>
  </div>`;
}

function renderAnnotationPanel() {
  const log = document.getElementById("annotationLog");
  if (!log) return;
  if (!state.detail || state.mode !== "annotate") {
    log.innerHTML = '<div class="muted">切到“标注”模式后可在这里查看改动日志</div>';
    return;
  }
  const state_ = ann.exists
    ? `<span class="ann-chip done">已标注</span> 第 ${ann.revision} 版 · ${esc(ann.updatedAt || "")}`
    : '<span class="ann-chip">未标注</span>';
  const status = `${state_}${ann.stale ? ' <span class="editor-warn">链上结果已变化，请复核</span>' : ""}`;
  const rows = ann.ops.map((operation) => `<div class="log-row"><span class="log-kind ${esc(operation.kind)}">${esc(operation.kind)}</span><span>${esc(shortBlockId(operation.block_id))}</span><span class="muted">${esc((operation.fields || []).join(", "))}</span></div>`).join("");
  const rv = ann.revisionValidation;
  const validationRow = rv && rv.checked
    ? (rv.error_count
      ? `<div class="log-row"><span class="log-kind delete">校验</span><span>保存后的 revision 有 ${rv.error_count} 条不一致</span><button type="button" data-validation="1">查看</button></div>`
      : '<div class="log-row"><span class="log-kind add">校验</span><span>与 ocr-agent revision 校验一致（0 error）</span></div>')
    : "";
  const deleted = [...ann.deleted.values()].map((block) => `<div class="log-row"><span class="log-kind delete">已删除</span><span>${esc(shortBlockId(block.block_id))}</span><button type="button" data-restore="${esc(block.block_id)}">恢复</button></div>`).join("");
  log.innerHTML = `<div class="annotation-log">
    <div class="log-status">${status}</div>
    <div class="log-metrics">
      <span><strong>${state.detail.blocks.length}</strong> → <strong>${ann.blocks.length}</strong> blocks</span>
      <span><strong>${ann.ops.length}</strong> 处未提交改动</span>
      ${ann.deleted.size ? `<span><strong>${ann.deleted.size}</strong> 个已删除</span>` : ""}
    </div>
    ${rows || '<div class="muted log-empty">本次会话还没有改动</div>'}
    ${deleted}
    ${validationRow}
  </div>`;
}

/* ---- interaction ---- */
function overlayPoint(event) {
  const rect = els.overlay.getBoundingClientRect();
  const viewBox = els.overlay.viewBox.baseVal;
  return {
    x: (event.clientX - rect.left) / rect.width * viewBox.width,
    y: (event.clientY - rect.top) / rect.height * viewBox.height,
  };
}

function hitBlock(point) {
  const hits = ann.blocks.filter((block) => insideBox(block.bbox_image_px, point));
  return hits.sort((a, b) => area(a.bbox_image_px) - area(b.bbox_image_px))[0] || null;
}

function hitHandle(event, point, boxOverride = null) {
  const selected = ann.blocks.find((block) => block.block_id === ann.selectedId);
  const target = boxOverride || selected?.bbox_image_px;
  if (!target) return null;
  const [x1, y1, x2, y2] = target;
  const viewBox = els.overlay.viewBox.baseVal;
  const tolerance = Math.max(6, Math.min(viewBox.width, viewBox.height) * 0.012);
  for (const [handle, hx, hy] of [["nw", x1, y1], ["ne", x2, y1], ["sw", x1, y2], ["se", x2, y2]]) {
    if (Math.abs(point.x - hx) <= tolerance && Math.abs(point.y - hy) <= tolerance) return {handle, origin: selected.bbox_image_px};
  }
  return null;
}

function normaliseBox(box) {
  return [Math.min(box[0], box[2]), Math.min(box[1], box[3]), Math.max(box[0], box[2]), Math.max(box[1], box[3])];
}

function onOverlayPointerDown(event) {
  if (state.mode !== "annotate" || event.button !== 0) return;
  // A focused input/select would swallow the keyboard shortcuts; working on the canvas releases it.
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable) document.activeElement.blur();
  const point = overlayPoint(event);
  if (ann.orderPick) {
    const picked = hitBlock(point);
    if (picked) applyOrderPick(picked.block_id);
    else cancelOrderPick();
    return;
  }
  if (ann.linkPick) {
    const picked = hitBlock(point);
    if (picked) applyLinkPick(picked.block_id);
    else cancelLinkPick();
    return;
  }
  // Create mode: the first drag frames a new box, later drags adjust the draft (move / resize),
  // and clicking empty space re-frames. Picking existing blocks stays disabled while armed.
  if (ann.createMode) {
    // Framing is modal too: existing boxes never take the click, a drag anywhere starts a new draft.
    if (ann.draft) {
      const draftHandle = hitHandle(event, point, ann.draft.box);
      if (draftHandle) {
        ann.drag = {mode: "draft-resize", handle: draftHandle.handle, origin: [...ann.draft.box], start: point};
        els.overlay.setPointerCapture(event.pointerId);
        return;
      }
      if (insideBox(ann.draft.box, point)) {
        ann.drag = {mode: "draft-move", origin: [...ann.draft.box], start: point};
        els.overlay.setPointerCapture(event.pointerId);
        return;
      }
    }
    ann.draft = null;
    selectAnnotationBlock(null);
    ann.drawing = [point.x, point.y, point.x, point.y];
    els.overlay.setPointerCapture(event.pointerId);
    return;
  }
  const handle = hitHandle(event, point);
  if (handle) {
    ann.drag = {mode: "resize", handle: handle.handle, origin: [...handle.origin], start: point, blockId: ann.selectedId};
    els.overlay.setPointerCapture(event.pointerId);
    els.overlay.classList.add("is-dragging");
    pushUndo();
    return;
  }
  // Empty canvas: dragging pulls a rubber band over the blocks (no modifier needed) and a plain click
  // still clears the selection - the gesture is decided on release, so both live on the same press.
  if (!hitBlock(point) && !event.shiftKey && !ann.createMode) {
    ann.band = {start: point, current: point, additive: false};
    els.overlay.setPointerCapture(event.pointerId);
    return;
  }
  if (event.shiftKey && !hitBlock(point)) {
    ann.band = {start: point, current: point, additive: true};
    els.overlay.setPointerCapture(event.pointerId);
    return;
  }
  const block = hitBlock(point);
  if (block && (event.ctrlKey || event.metaKey || event.shiftKey)) {
    selectAnnotationBlock(block.block_id, {additive: true});
    renderBlockPopover(true);
    return;
  }
  if (block) {
    // Clicking any box switches the card to it - selecting is cheap and reversible.
    // Moving/resizing is the gesture that needs intent, so a drag only starts on the box that is
    // already selected: pressing on a different box selects it and does nothing else, and the drag
    // itself is a second gesture.
    const alreadySelected = ann.selectedId === block.block_id || ann.multi.includes(block.block_id);
    selectAnnotationBlock(block.block_id);
    if (!alreadySelected) {
      renderBlockPopover(true);
      return;
    }
    ann.drag = {mode: "move", origin: [...block.bbox_image_px], start: point, blockId: block.block_id};
    els.overlay.setPointerCapture(event.pointerId);
    els.overlay.classList.add("is-dragging");
    pushUndo();
    return;
  }
  selectAnnotationBlock(null);
  ann.drawing = [point.x, point.y, point.x, point.y];
  els.overlay.setPointerCapture(event.pointerId);
}

function onOverlayPointerMove(event) {
  if (state.mode !== "annotate") return;
  const point = overlayPoint(event);
  if (ann.orderPick) {
    const hovered = hitBlock(point);
    const hoverId = hovered && hovered.block_id !== ann.orderPick.blockId ? hovered.block_id : null;
    if (hoverId !== ann.orderPick.hoverId) {
      ann.orderPick.hoverId = hoverId;
      renderOverlay();
      updateModeBanner();
    }
    return;
  }
  if (ann.linkPick) {
    const hovered = hitBlock(point);
    const hoverId = hovered ? hovered.block_id : null;
    if (hoverId !== ann.linkPick.hoverId) {
      ann.linkPick.hoverId = hoverId;
      renderOverlay();
      updateModeBanner();
    }
    return;
  }
  if (ann.band) {
    ann.band.current = point;
    renderOverlay(true);
    return;
  }
  if (ann.drawing) {
    ann.drawing = normaliseBox([ann.drawing[0], ann.drawing[1], point.x, point.y]);
    const snap = event.altKey ? {box: ann.drawing, refs: []} : snapBox(ann.drawing);
    ann.snapPreview = snap.refs.length ? snap.box : null;
    syncSnapNode();
    const [dx1, dy1, dx2, dy2] = ann.drawing;
    if (!syncDrawingNode(dx1, dy1, dx2, dy2)) renderOverlay();
    return;
  }
  if (ann.drag) {
    const dx = point.x - ann.drag.start.x;
    const dy = point.y - ann.drag.start.y;
    if (ann.drag.mode === "draft-move" || ann.drag.mode === "draft-resize") {
      const [ox1, oy1, ox2, oy2] = ann.drag.origin;
      ann.draft.box = ann.drag.mode === "draft-move"
        ? [ox1 + dx, oy1 + dy, ox2 + dx, oy2 + dy]
        : normaliseBox([
            ann.drag.handle.includes("w") ? ox1 + dx : ox1,
            ann.drag.handle.includes("n") ? oy1 + dy : oy1,
            ann.drag.handle.includes("e") ? ox2 + dx : ox2,
            ann.drag.handle.includes("s") ? oy2 + dy : oy2,
          ]);
      if (!syncDraftNode()) renderOverlay();
      updateDraftActions();
      return;
    }
    const block = ann.blocks.find((entry) => entry.block_id === ann.drag.blockId);
    if (!block) return;
    const [x1, y1, x2, y2] = ann.drag.origin;
    if (ann.drag.mode === "move") {
      block.bbox_image_px = [x1 + dx, y1 + dy, x2 + dx, y2 + dy];
    } else {
      block.bbox_image_px = normaliseBox([
        ann.drag.handle.includes("w") ? x1 + dx : x1,
        ann.drag.handle.includes("n") ? y1 + dy : y1,
        ann.drag.handle.includes("e") ? x2 + dx : x2,
        ann.drag.handle.includes("s") ? y2 + dy : y2,
      ]);
    }
    // Snapping helps line a box up with the PDF evidence; Alt suspends it for a free move.
    const snap = event.altKey ? {box: block.bbox_image_px, refs: []} : snapBox(block.bbox_image_px);
    if (snap.refs.length) block.bbox_image_px = snap.box;
    ann.snapPreview = snap.refs.length ? snap.box : null;
    // In-place update keeps dragging at frame rate: no overlay rebuild unless something is missing.
    syncSnapNode();
    if (!syncBlockNode(block.block_id)) renderOverlay();
    updateEditorBox({refresh: false});
  }
}

function onOverlayPointerUp(event) {
  if (state.mode !== "annotate") return;
  els.overlay.classList.remove("is-dragging");
  const point = overlayPoint(event);
  if (ann.band) {
    const band = ann.band;
    const box = normaliseBox([band.start.x, band.start.y, point.x, point.y]);
    const dragged = Math.abs(point.x - band.start.x) > 4 || Math.abs(point.y - band.start.y) > 4;
    ann.band = null;
    if (!dragged) {
      // A click on empty canvas keeps its old meaning: clear the selection.
      selectAnnotationBlock(null);
      renderBlockPopover(true);
      renderCurrentLayer();
      return;
    }
    const inside = ann.blocks.filter((block) => {
      const [x1, y1, x2, y2] = block.bbox_image_px;
      const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
      return cx >= box[0] && cx <= box[2] && cy >= box[1] && cy <= box[3];
    }).map((block) => block.block_id);
    if (inside.length >= 1) {
      ann.multi = inside;
      selectAnnotationBlock(inside[inside.length - 1], {additive: true});
      ann.multi = inside;
      ann.selectedId = inside[inside.length - 1];
      state.selectedKey = `ann:${ann.selectedId}`;
      toast(`已选中 ${inside.length} 个框${inside.length >= 2 ? "，可以合并" : ""}`);
    } else {
      selectAnnotationBlock(null);
    }
    renderCurrentLayer();
    return;
  }
  if (ann.drawing) {
    const drawn = normaliseBox([ann.drawing[0], ann.drawing[1], point.x, point.y]);
    const snap = event.altKey ? {box: drawn, refs: []} : snapBox(drawn);
    ann.drawing = null;
    ann.snapPreview = null;
    if (area(snap.box) >= 16) {
      ann.draft = {box: snap.box, refs: snap.refs};
      renderCurrentLayer();
      updateModeBanner();
    } else {
      renderOverlay();
    }
    return;
  }
  if (ann.drag) {
    if (ann.drag.mode === "draft-move" || ann.drag.mode === "draft-resize") {
      ann.drag = null;
      renderOverlay();
      updateDraftActions();
      return;
    }
    const block = ann.blocks.find((entry) => entry.block_id === ann.drag.blockId);
    const origin = ann.drag.origin;
    const mode = ann.drag.mode;
    ann.drag = null;
    ann.snapPreview = null;
    if (block && JSON.stringify(block.bbox_image_px) !== JSON.stringify(origin)) {
      if (block._state === "chain") block._state = "updated";
      // Changing a box must never lose its content: the text stays as it is, and a box that has no
      // text yet picks up whatever the new geometry now covers.
      if (ANNOTATION_TEXT_TYPES.has(block.type) && !String(block.text || "").trim()) {
        const grown = contentFromBox(block.bbox_image_px);
        if (grown.text) {
          block.text = grown.text;
          block._prefilled = true;
          toast("已按新范围补齐文本（可编辑）");
        }
      }
      recordOp("move", block.block_id, [mode === "move" ? "bbox_image_px" : "bbox_image_px (resize)"]);
    } else {
      ann.undo.pop();
      ann.dirty = ann.ops.length > 0;
    }
    renderOverlay();
    renderCurrentLayer();
  }
}

/* ---- persistence ---- */
function materialiseBlocks() {
  renumber();
  return ann.blocks.map((block) => {
    const copy = cloneValue(block);
    delete copy._state;
    for (const key of Object.keys(copy)) {
      if (key.startsWith("_")) delete copy[key];
    }
    return copy;
  });
}

async function flushAnnotations() {
  if (state.mode !== "annotate") return true;
  commitLiveEdit();
  if (ann.savePromise && !(await ann.savePromise)) return false;
  // Edits made during a pending save belong to a second snapshot.
  while (ann.dirty) {
    if (!(await saveAnnotations(true))) return false;
    commitLiveEdit();
  }
  return true;
}

async function saveAnnotations(silent = false) {
  if (!state.detail || state.mode !== "annotate") return true;
  commitLiveEdit();
  if (ann.savePromise) return ann.savePromise;
  if (!ann.dirty) {
    if (!silent) toast("没有需要保存的改动");
    return true;
  }
  const pending = persistAnnotations();
  ann.savePromise = pending;
  try { return await pending; }
  finally { ann.savePromise = null; }
}

async function persistAnnotations() {
  ann.saving = true;
  ann.saveError = "";
  window.clearTimeout(ann.autosaveTimer);
  const version = ann.editVersion;
  const opsBefore = ann.ops.length;
  updateAnnotationChrome();
  try {
    const saved = await requestJson(`/api/annotations/${encodeURIComponent(state.detail.sample_id)}/${state.detail.page}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({annotator: ann.annotator, blocks: materialiseBlocks(), operations: ann.ops}),
    });
    ann.annotator = saved.annotator || ann.annotator;
    try { window.localStorage.setItem("ocrAgentAnnotator", ann.annotator); } catch (_) {}
    ann.savedAt = new Date().toTimeString().slice(0, 8);
    ann.revisionValidation = saved.revision_validation || null;
    ann.fieldReviews = saved.field_reviews || null;
    // Saving is a checkpoint, not a reset: keep selection and undo/redo history.
    ann.exists = true;
    ann.revision = saved.revision || ann.revision;
    ann.updatedAt = saved.updated_at || ann.updatedAt;
    ann.stale = Boolean(saved.stale);
    ann.ops.splice(0, opsBefore);
    for (const snapshot of [...ann.undo, ...ann.redo]) snapshot.ops = [];
    ann.dirty = ann.editVersion !== version;
    if (ann.dirty) {
      ann.autosaveTimer = window.setTimeout(() => { saveAnnotations(true); }, AUTOSAVE_MS);
    }
    state.annotated?.add(`${state.detail.sample_id}:${state.detail.page}`);
    markCatalogAnnotated(state.detail.sample_id, state.detail.page);
    state.renderedExpanded = null;
    renderCatalog();
    renderPageSummary();
    renderBatchProgress();
    return true;
  } catch (error) {
    ann.saveError = `保存失败：${error.message}。请重试，或先导出本页改动。`;
    ann.dirty = true;
    toast(ann.saveError, true);
    return false;
  } finally {
    ann.saving = false;
    updateAnnotationChrome();
  }
}

function exportLocalAnnotation() {
  if (!state.detail) return;
  commitLiveEdit();
  const payload = {sample_id: state.detail.sample_id, page: state.detail.page, batch: state.batch,
    annotator: ann.annotator, blocks: materialiseBlocks(), operations: ann.ops};
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.detail.sample_id}-page-${state.detail.page}-draft.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Discard this page's patch and start again from the pipeline result. */
async function clearAnnotations() {
  if (!state.detail || ann.saving || state.navigating) return;
  commitLiveEdit();
  window.clearTimeout(ann.autosaveTimer);
  try {
    const cleared = await requestJson(`/api/annotations/${encodeURIComponent(state.detail.sample_id)}/${state.detail.page}`, {method: "DELETE"});
    state.annotated.delete(`${state.detail.sample_id}:${state.detail.page}`);
    applyPatch(cleared);
    renderCurrentLayer();
    renderCatalog();
    toast("已重置本页标注，从模型结果重新开始");
  } catch (error) {
    toast(`重置失败：${error.message}`, true);
  }
}

function openResetDialog() {
  if (!state.detail || !els.resetDialog) return;
  const sampleId = state.detail.sample_id;
  const page = state.detail.page;
  const ops = state.annotator_ops ?? ann.ops.length;
  els.resetDialogWhere.textContent = `${sampleId} · 第 ${page} 页`;
  const blocks = (state.detail.blocks || []).length;
  els.resetDialogHint.textContent = ann.exists
    ? `本页已有第 ${ann.revision} 版标注：链上 ${blocks} blocks → 当前 ${ann.blocks.length} blocks${ops ? ` · 未保存改动 ${ops} 处` : ""}`
    : "本页还没有保存过标注";
  els.resetDialog.hidden = false;
  els.resetDialogConfirm.focus();
}

function closeResetDialog() {
  if (els.resetDialog) els.resetDialog.hidden = true;
}

async function confirmResetDialog() {
  closeResetDialog();
  await clearAnnotations();
}

/* ---- mode switching ---- */
function openDocumentDialog(nextAction) {
  const sampleId = state.detail?.sample_id;
  if (!sampleId) return;
  const document_ = state.catalog?.documents.find((entry) => entry.sample_id === sampleId);
  const total = document_?.pages.length || 0;
  const annotated = (document_?.pages || []).filter((item) => state.annotated?.has(`${sampleId}:${item.page}`)).length;
  ann.pendingDocAction = nextAction || null;
  els.docDialogSample.textContent = sampleId;
  els.docDialogProgress.innerHTML = `<div class="progress-label"><span>本文档已标注</span><strong>${annotated} / ${total} 页</strong></div>
    <div class="progress-track"><div class="progress-fill ok" style="width:${total ? Math.round(annotated / total * 100) : 0}%"></div></div>`;
  els.docDialogHint.textContent = state.annotated?.has(`${sampleId}:${state.detail.page}`)
    ? "当前页已保存标注。确认整篇文档是否标注成功，结论会写入 annotations/<sample>/document.json。"
    : "当前页还没有保存标注（停止编辑 2.5 秒会自动保存）。";
  els.docDialogNote.value = "";
  els.docDialog.hidden = false;
  els.docDialogPass.focus();
}

function closeDocumentDialog() {
  ann.pendingDocAction = null;
  els.docDialog.hidden = true;
}

async function submitDocumentDialog(status) {
  const sampleId = state.detail?.sample_id;
  const action = ann.pendingDocAction;
  els.docDialog.hidden = true;
  ann.pendingDocAction = null;
  if (!sampleId) return;
  try {
    const saved = await requestJson(`/api/document-annotations/${encodeURIComponent(sampleId)}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status, note: els.docDialogNote.value, annotator: ann.annotator}),
    });
    ann.docStatus[sampleId] = saved.status;
    state.annotated = state.annotated || new Set();
    renderCatalog();
    renderBatchProgress();
    toast(`文档 ${sampleId} 标注结论：${saved.status === "PASS" ? "成功" : "失败"}`);
  } catch (error) {
    toast(`文档结论保存失败：${error.message}`, true);
  }
  if (action) action();
}

async function setMode(mode) {
  if (state.mode === mode) return;
  if (state.navigating || !(await flushAnnotations())) return;
  state.mode = mode;
  els.modeSwitch.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  const annotating = mode === "annotate";
  els.annotateBar.hidden = !annotating;
  els.annotateHint.hidden = !annotating;
  renderCatalogFilters();
  renderCatalog();
  if (state.detail) renderPageSummary();
  els.reviewActions.hidden = annotating || !state.detail;
  // the layer switch selects which evidence layer to inspect; annotation always edits the final blocks
  els.layerControl.style.opacity = annotating ? "0.45" : "1";
  els.layerControl.style.pointerEvents = annotating ? "none" : "auto";
  els.layerControl.title = annotating ? "标注模式固定编辑 Final 结果；证据范围在编辑器里显示" : "";
  if (!annotating) {
    els.overlay.innerHTML = "";
    state.selectedKey = null;
    renderCurrentLayer();
    return;
  }
  if (state.detail) {
    const patch = await requestJson(`/api/annotations/${encodeURIComponent(state.detail.sample_id)}/${state.detail.page}?t=${Date.now()}`);
    applyPatch(patch);
    state.layer = "final";
    els.layerControl.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.layer === "final"));
    updateAnnotationChrome();
    renderCurrentLayer();
    switchTab("annotation");
  }
}

els.modeSwitch?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-mode]");
  if (button) setMode(button.dataset.mode).catch((error) => toast(error.message, true));
});
function toggleAnnotateMenu(force = null) {
  if (!els.annotateMenu) return;
  const open = force === null ? els.annotateMenu.hidden : force;
  els.annotateMenu.hidden = !open;
  els.annotateMenuBtn?.setAttribute("aria-expanded", String(open));
}

els.annotateMenuBtn?.addEventListener("click", (event) => { event.stopPropagation(); toggleAnnotateMenu(); });
els.annotateMenu?.addEventListener("click", (event) => event.stopPropagation());
document.addEventListener("click", () => toggleAnnotateMenu(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && els.annotateMenu && !els.annotateMenu.hidden) toggleAnnotateMenu(false);
});

/* drag the card by its header: it often covers the box being adjusted */
els.blockPopover?.addEventListener("pointerdown", (event) => {
  const head = event.target.closest(".pop-head");
  if (!head || event.target.closest("button")) return;
  const pop = els.blockPopover;
  const rect = pop.getBoundingClientRect();
  const start = {x: event.clientX, y: event.clientY, left: rect.left, top: rect.top};
  pop.setPointerCapture(event.pointerId);
  pop.classList.add("dragging");
  const move = (moveEvent) => {
    ann.cardPinned = {
      left: Math.max(4, Math.min(start.left + moveEvent.clientX - start.x, window.innerWidth - 60)),
      top: Math.max(4, Math.min(start.top + moveEvent.clientY - start.y, window.innerHeight - 40)),
    };
    positionBlockPopover(false);
  };
  const finish = () => {
    pop.classList.remove("dragging");
    if (pop.hasPointerCapture?.(event.pointerId)) pop.releasePointerCapture(event.pointerId);
    pop.removeEventListener("pointermove", move);
    pop.removeEventListener("pointerup", finish);
    pop.removeEventListener("pointercancel", finish);
  };
  pop.addEventListener("pointermove", move);
  pop.addEventListener("pointerup", finish);
  pop.addEventListener("pointercancel", finish);
});
els.blockPopover?.addEventListener("dblclick", (event) => {
  if (event.target.closest(".pop-head")) {
    ann.cardPinned = null;
    positionBlockPopover(true);
    toast("卡片回到自动位置");
  }
});
els.hardFlagBtn?.addEventListener("click", () => toggleHardFlag());
els.flowToggleBrowse?.addEventListener("click", () => {
  ann.showFlow = !ann.showFlow;
  toggleButton(els.flowToggleBrowse, ann.showFlow);
  if (els.flowToggle) els.flowToggle.checked = ann.showFlow;
  renderOverlay();
});
els.resetDialogCancel?.addEventListener("click", closeResetDialog);
els.resetDialogConfirm?.addEventListener("click", () => confirmResetDialog());
els.resetDialog?.addEventListener("click", (event) => { if (event.target === els.resetDialog) closeResetDialog(); });
els.newBoxBtn?.addEventListener("click", () => { toggleAnnotateMenu(false); enterCreateMode(); });
els.draftConfirm?.addEventListener("click", () => confirmDraft());
els.draftCancel?.addEventListener("click", () => discardDraft());
els.snapToggle?.addEventListener("change", () => { ann.snap = els.snapToggle.checked; toast(ann.snap ? "拖动/新建会吸附到框内 PDF 证据" : "已关闭吸附"); });
els.flowToggle?.addEventListener("change", () => { ann.showFlow = els.flowToggle.checked; renderOverlay(); });
els.orderPickBtn?.addEventListener("click", () => startOrderPick());
els.prevPageBtn?.addEventListener("click", () => navigatePage(-1));
els.nextPageBtn?.addEventListener("click", () => navigatePage(1));
els.pickCancel?.addEventListener("click", () => cancelOrderPick());
els.docDialogPass?.addEventListener("click", () => submitDocumentDialog("PASS"));
els.docDialogFail?.addEventListener("click", () => submitDocumentDialog("FAIL"));
els.docDialogLater?.addEventListener("click", () => closeDocumentDialog());
els.undoBtn?.addEventListener("click", undo);
els.redoBtn?.addEventListener("click", redo);
els.saveAnnotations?.addEventListener("click", () => saveAnnotations(false));
els.clearAnnotations?.addEventListener("click", () => openResetDialog());
els.annotatorName?.addEventListener("change", () => {
  ann.annotator = els.annotatorName.value.trim();
  window.localStorage.setItem("ocrAgentAnnotator", ann.annotator);
});
els.overlay.addEventListener("pointerdown", onOverlayPointerDown);
els.overlay.addEventListener("pointermove", onOverlayPointerMove);
els.overlay.addEventListener("pointerup", onOverlayPointerUp);
els.overlay.addEventListener("pointercancel", () => { ann.drawing = null; ann.drag = null; ann.snapPreview = null; renderOverlay(); });
els.blockPopover?.addEventListener("click", (event) => {
  if (state.mode !== "annotate") return;
  const current = selectedAnnotationBlock();
  if (!current) return;
  ann.selectedId = current.block_id;
  const section = event.target.closest("button[data-section]")?.dataset.section;
  if (section) {
    if (!ann.cardOpen) ann.cardOpen = new Set(["content"]);
    if (ann.cardOpen.has(section)) ann.cardOpen.delete(section);
    else ann.cardOpen.add(section);
    renderBlockPopover();
    return;
  }
  const quickType = event.target.closest("button[data-quick-type]")?.dataset.quickType;
  if (quickType) {
    commitLiveEdit();
    retypeBlock(ann.selectedId, quickType);
    return;
  }
  const levelButton = event.target.closest("button[data-level]");
  if (levelButton) {
    commitLiveEdit();
    updateBlockFields(ann.selectedId, {level: Number(levelButton.dataset.level)}, "update");
    return;
  }
  const popAction = event.target.closest("button[data-pop]")?.dataset.pop;
  if (popAction === "close") {
    commitLiveEdit();
    selectAnnotationBlock(null);
    return;
  }
  if (popAction === "unpin") {
    ann.cardPinned = null;
    renderBlockPopover(true);
    return;
  }
  const multiType = event.target.closest("button[data-multi-type]")?.dataset.multiType;
  if (multiType) {
    retypeSelection(multiType);
    return;
  }
  const crossMode = event.target.closest("button[data-cross-mode]")?.dataset.crossMode;
  if (crossMode) {
    const block = selectedAnnotationBlock();
    if (!block) return;
    commitLiveEdit();
    if (crossMode === "continue") {
      startLinkPick("prev");                       // pick the tail block/table on the previous page
      return;
    }
    const field = block.type === "table" ? "cross_page_table" : "cross_page_text";
    const previous = block[field] || {};
    const next = {...previous, status: crossMode === "separate" ? "PASS" : "CHECK", method: "manual_annotation_link"};
    if (crossMode === "separate") {
      next.continues_block_id = null;              // null means "decided: does not continue"
      next.continued_by_block_id = null;
    } else {
      delete next.continues_block_id;
      delete next.continued_by_block_id;
    }
    updateBlockFields(block.block_id, {[field]: next}, "update");
    if (crossMode === "separate" && previous.continues_block_id) clearReciprocalLink(block, previous.continues_block_id);
    toast(crossMode === "separate" ? "已记为「不接」" : "已记为「未定」，请在右侧复核");
    return;
  }
  const action = event.target.closest("button[data-action]")?.dataset.action;
  if (!action) return;
  const block = selectedAnnotationBlock();
  if (!block) return;
  commitLiveEdit();
  if (action === "merge") { mergeSelectedBlocks(); return; }
  if (action === "multi-sort-single") { reorderSelection(orderBlocksByPosition(ann.multi.map((id) => ann.blocks.find((b) => b.block_id === id)).filter(Boolean)), "auto_by_position_selection"); return; }
  if (action === "multi-sort-columns") { const info = detectPageColumns(ann.blocks); reorderSelection(orderBlocksByColumns(ann.blocks.filter((b) => ann.multi.includes(b.block_id)), info), "auto_by_columns_selection"); return; }
  if (action === "multi-delete") { deleteSelectedBlocks(); return; }
  if (action === "delete") deleteBlock(ann.selectedId);
  if (action === "duplicate") duplicateBlock(ann.selectedId);
  if (action === "revert") revertToChain(ann.selectedId);
  if (action === "snap-selected") {
    const snap = snapBox(block.bbox_image_px);
    updateBlockFields(ann.selectedId, {bbox_image_px: snap.box, source_refs: snap.refs.length ? snap.refs : block.source_refs}, "move");
    syncCardFields(block);
  }
  if (action === "fill-inside" || action === "copy-inside" || action === "copy-page") {
    const insideText = evidenceTextInside(block.bbox_image_px);
    if (action === "fill-inside") {
      if (!insideText) {
        toast("框内没有 PDF 文字行", true);
      } else {
        // write it into the block *and* into the open textarea: the card is the place the annotator
        // is looking at, so a model-only update looked like the button did nothing.
        block.text = insideText;
        if (block._state === "chain") block._state = "updated";
        const field = document.getElementById("popText");
        if (field) field.value = insideText;
        recordOp("update", block.block_id, ["text"]);
        markDirty();
        syncCardFields(block);
        renderBlockList();
        renderAnnotationPanel();
        toast("已用框内 PDF 文字填充");
      }
    } else {
      copyToClipboard(action === "copy-page" ? pagePlainText() : insideText, action === "copy-page" ? "整页文本已复制" : "框内文本已复制");
    }
  }
  if (action === "order-up") moveBlockInOrder(ann.selectedId, -1);
  if (action === "order-down") moveBlockInOrder(ann.selectedId, 1);
  if (action === "order-first") moveBlockTo(ann.selectedId, 0);
  if (action === "order-last") moveBlockTo(ann.selectedId, ann.blocks.length - 1);
  if (action === "order-pick") startOrderPick();
  if (action === "auto-sort") autoSortByPosition();
  if (action === "auto-sort-columns") autoSortByColumns();
  if (action === "clear-text") clearBlockText();
  if (action === "link-prev") startLinkPick("prev");
  if (action === "link-next") startLinkPick("next");
  if (action === "link-clear") {
    const field = block.type === "table" ? "cross_page_table" : "cross_page_text";
    const next = {...(block[field] || {})};
    delete next.continues_block_id;
    delete next.continued_by_block_id;
    updateBlockFields(ann.selectedId, {[field]: next}, "update");
  }
});
els.blockPopover?.addEventListener("input", (event) => {
  if (state.mode !== "annotate") return;
  const block = selectedAnnotationBlock();
  if (!block) return;
  // Editing a table cell rewrites the block HTML (the grid *is* the HTML, cells included).
  const cell = event.target.closest?.("td[contenteditable], th[contenteditable]");
  if (cell) {
    const table = cell.closest("table.table-editor");
    if (table) applyLiveEdit(block.block_id, {html: serialiseBlockTable(table)}, "html");
    return;
  }
  const id = event.target.id;
  if (id === "popText") applyLiveEdit(block.block_id, {text: event.target.value}, "text");
  if (id === "popHtml") {
    applyLiveEdit(block.block_id, {html: event.target.value}, "html");
    renderBlockPopover(true);
  }
  if (["bboxX1", "bboxY1", "bboxX2", "bboxY2"].includes(id)) {
    if (!event.target.value) return;
    const box = [...block.bbox_image_px];
    box[{"bboxX1": 0, "bboxY1": 1, "bboxX2": 2, "bboxY2": 3}[id]] = Number(event.target.value);
    applyLiveEdit(block.block_id, {bbox_image_px: normaliseBox(box)}, "bbox_image_px");
  }
});
els.blockPopover?.addEventListener("change", (event) => {
  if (state.mode !== "annotate") return;
  const block = selectedAnnotationBlock();
  if (!block) return;
  ann.selectedId = block.block_id;
  commitLiveEdit();
  if (event.target.id === "popParent") updateBlockFields(ann.selectedId, {parent_id: event.target.value}, "update");
  const crossSelect = event.target.closest("select[data-cross-field]");
  if (crossSelect) {
    const scope = crossSelect.dataset.crossScope;          // cross_page_text | cross_page_table
    const name = crossSelect.dataset.crossField;
    const next = {...(block[scope] || {})};
    if (name === "status") {
      if (crossSelect.value === "") delete next.status;
      else next.status = crossSelect.value === "true" ? "PASS" : "CHECK";
    } else if (crossSelect.value === "") {
      delete next[name];                                    // "" means undetermined, which is not false
    } else {
      next[name] = crossSelect.value === "true";
    }
    next.method = "manual_annotation_link";
    updateBlockFields(block.block_id, {[scope]: next}, "update");
    return;
  }
  const crossInt = event.target.closest("input[data-cross-int]");
  if (crossInt) {
    const scope = block.type === "table" ? "cross_page_table" : "cross_page_text";
    const next = {...(block[scope] || {})};
    const value = Number.parseInt(event.target.value, 10);
    if (Number.isInteger(value) && value > 0) next[crossInt.dataset.crossInt] = value;
    else delete next[crossInt.dataset.crossInt];
    next.method = "manual_annotation_link";
    updateBlockFields(block.block_id, {[scope]: next}, "update");
    return;
  }
});
els.blockPopover?.addEventListener("focusin", (event) => {
  const cell = event.target.closest?.("td.cell-edit, th.cell-edit");
  if (cell) cell.classList.add("cell-active");
});
els.blockPopover?.addEventListener("focusout", (event) => {
  const cell = event.target.closest?.("td.cell-edit, th.cell-edit");
  if (cell) cell.classList.remove("cell-active");
  window.setTimeout(() => {
    if (!els.blockPopover.contains(document.activeElement)) commitLiveEdit();
  }, 0);
});
els.blockPopover?.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.stopPropagation();
    const cell = event.target.closest?.("td.cell-edit, th.cell-edit");
    if (cell) {
      // Esc inside a cell only leaves the cell, so the card is not closed by accident.
      commitLiveEdit();
      cell.blur();
      return;
    }
    commitLiveEdit();
    selectAnnotationBlock(null);
    return;
  }
  // Tab moves through the cells of a table instead of leaving the card.
  if (event.key === "Tab" && event.target.closest?.("td.cell-edit, th.cell-edit")) {
    const cells = [...els.blockPopover.querySelectorAll("td.cell-edit, th.cell-edit")];
    const index = cells.indexOf(event.target);
    const next = cells[(index + (event.shiftKey ? -1 : 1) + cells.length) % cells.length];
    if (next) {
      event.preventDefault();
      next.focus();
      const range = document.createRange();
      range.selectNodeContents(next);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }
});
els.canvasScroll?.addEventListener("scroll", () => positionBlockPopover(false), {passive: true});
// Clicking anywhere outside the page (the grey area around it) ends the current edit, the same way
// clicking empty canvas inside the page does.
els.canvasScroll?.addEventListener("pointerdown", (event) => {
  if (state.mode !== "annotate") return;
  if (event.target.closest("#blockPopover") || event.target.closest("#draftActions") || event.target.closest("#pickBanner")) return;
  // Decide by geometry, not by event target: pointer capture retargets events, and a click on the
  // page must keep the selection (only the grey area around the page ends the edit).
  const frame = els.pageFrame.getBoundingClientRect();
  const onPage = event.clientX >= frame.left && event.clientX <= frame.right && event.clientY >= frame.top && event.clientY <= frame.bottom;
  if (onPage) return;
  commitLiveEdit();
  selectAnnotationBlock(null);
});
window.addEventListener("resize", () => positionBlockPopover(true));

els.annotationPanel?.addEventListener("click", (event) => {
  const blockId = event.target.closest("button[data-restore]")?.dataset.restore;
  if (blockId) restoreDeleted(blockId);
  if (event.target.closest("button[data-validation]")) {
    const errors = (ann.revisionValidation?.errors || []) ;
    const target = document.getElementById("validationDetail");
    if (target) {
      target.innerHTML = errors.length
        ? `<ul class="validation-list">${errors.map((item) => `<li><code>${esc(item.code)}</code> ${esc(item.message)}</li>`).join("")}</ul>`
        : '<div class="muted">没有细节</div>';
      target.hidden = false;
    }
  }
});
els.textPanel?.addEventListener("click", (event) => {
  const copy = event.target.closest("button[data-copy]")?.dataset.copy;
  if (copy === "page") copyToClipboard(pagePlainText(), "已复制整页文本");
  if (copy === "lines") {
    const lines = (state.detail?.evidence?.pdf || []).filter((item) => item.kind === "pdf_line" && String(item.text || "").trim());
    copyToClipboard(lines.map((item) => String(item.text).trim()).join("\n"), "已复制多行文本");
  }
  const line = event.target.closest("button[data-copy-line]")?.dataset.copyLine;
  if (line) copyToClipboard(line, "已复制该行");
});
window.addEventListener("beforeunload", (event) => {
  if (state.mode === "annotate" && (ann.dirty || ann.saving || liveEdit.fields.size)) {
    event.preventDefault();
    event.returnValue = "标注未保存";
  }
});

/* ----------------------------------------------------------------------------------------------
 * Quiet background sync
 *
 * Several people annotate the same batch at once, so progress (and a re-balanced workload) should
 * show up without a page reload. The refresh is skipped whenever the annotator is mid-edit, and it
 * never touches the selection or the scroll position: only the counters and list chips are redrawn.
 * -------------------------------------------------------------------------------------------- */
const QUEUE_SYNC_INTERVAL = 20000;

function syncIsSafe() {
  if (state.mode !== "annotate" && state.mode !== "browse") return false;
  if (ann.dirty || ann.saving || ann.drawing || ann.band || ann.linkPick || ann.orderPick || ann.createMode) return false;
  if (ann.drag) return false;
  if (els.blockPopover && !els.blockPopover.hidden) return false;
  if (document.querySelector(".lightbox:not([hidden]), #lightbox:not([hidden])")) return false;
  return true;
}

async function quietSync() {
  if (!state.batch || !syncIsSafe()) return;
  try {
    state.catalog = await requestJson(`/api/catalog?batch=${encodeURIComponent(state.batch)}&t=${Date.now()}`);
    state.pages = state.catalog.documents.flatMap((doc) => doc.pages);
    await loadQueue(state.batch);
    await loadAnnotationIndex();
    renderCatalogFilters();
    renderCatalog();
  } catch (_) {
    /* a failed background refresh must never disturb the annotator */
  }
}

setInterval(quietSync, QUEUE_SYNC_INTERVAL);
