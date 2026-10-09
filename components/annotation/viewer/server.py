#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

try:  # Support direct source execution as well as the installed viewer command.
    import ocr_agent
except ModuleNotFoundError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocr_agent.contract_gt import node_id_for_block
from ocr_agent.constants import ALLOWED_TYPES
from ocr_agent.schemas import SchemaError, add_coordinate_views, validate_block
from ocr_agent.jsonio import write_json_atomic
from ocr_agent.validators import validate_revision

ROOT = Path(__file__).resolve().parent
DEFAULT_PILOT_ROOT = Path(os.environ.get("OCR_AGENT_DOCUMENT_ROOT", "data/annotation"))
STATUS_SCHEMA = "ocr-agent-status.v1"
ANNOTATION_SCHEMA = "ocr-agent.page-annotations.v1"
# Humans may mark deeper heading levels than the pipeline contract (which flattens >4): the annotation
# layer keeps the human value, the export clamps it back to the contract.
ANNOTATION_MAX_TITLE_LEVEL = 6
PAGE_FLAG_SCHEMA = "ocr-agent.page-flag.v1"
PAGE_FLAGS = {"too_hard"}
DOCUMENT_ANNOTATION_SCHEMA = "ocr-agent.document-annotation.v1"
DOCUMENT_ANNOTATION_STATUSES = {"PASS", "FAIL"}
REVIEW_STATUSES = {"PASS", "CHECK", "FAIL"}
SAFE_SAMPLE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Batch names are shown in the UI and come from the annotation split, which uses readable names such
# as 标注_batch1: allow any printable character except path separators and control codes.
FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="6" fill="#1f6feb"/>'
    b'<rect x="7" y="7" width="18" height="4" rx="1" fill="#fff"/>'
    b'<rect x="7" y="14" width="18" height="4" rx="1" fill="#fff" opacity=".8"/>'
    b'<rect x="7" y="21" width="11" height="4" rx="1" fill="#fff" opacity=".6"/>'
    b'</svg>'
)

SAFE_BATCH = re.compile(r'^[^\x00-\x1f\x7f/\\:*?"<>|]{1,80}$')
SAFE_ANNOTATOR = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_ANNOTATION_BLOCKS = 2000
MAX_ANNOTATION_TEXT = 20000
MAX_ANNOTATION_HTML = 400000
ANNOTATION_EDIT_STATES = {"added", "updated", "deleted"}
# fields the annotation editor owns; everything else of a chain block is carried through untouched
ANNOTATION_EDITABLE_FIELDS = ("type", "level", "text", "html", "parent_id")
HASHED_ANNOTATION_FIELDS = ("block_id", "type", "level", "text", "html", "parent_id", "bbox_image_px", "reading_order")


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_signature(blocks: list[dict]) -> str:
    """Stable hash of the annotated geometry/semantics - used to detect later edits of the same page."""
    import hashlib

    payload = [[block.get(field) for field in HASHED_ANNOTATION_FIELDS] for block in blocks]
    payload.sort(key=lambda entry: (str(entry[0]), str(entry[6])))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class OCRAgentCatalog:
    def __init__(self, pilot_root: Path) -> None:
        self.root = pilot_root.resolve()
        self.batch_root = self.root.parent
        self.batch_name = self.root.name
        self._doc_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._doc_cache_cap = 12
        self._lock = threading.Lock()
        self._keys: set[str] | None = None
        self._image_locks: dict[str, threading.Lock] = {}
        self._catalog_cache: tuple[tuple, dict] | None = None
        self._catalog_at = 0.0
        self._catalog_ttl = 10.0            # seconds; new pages of a running batch appear within this window
        self._detail_cache: "OrderedDict[tuple[str, int], tuple[tuple, dict]]" = OrderedDict()
        self._detail_cap = 24
        self._page_meta: dict[tuple[str, int], tuple[tuple, dict]] = {}
        self._status_cache: "OrderedDict[str, tuple[tuple, dict]]" = OrderedDict()
        self._status_cap = 8192
        # Assignment plan + queue keys: reloaded whenever the file changes, so a re-balance shows up
        # in every open viewer without restarting the service.
        self._workload_cache: tuple[tuple, dict] | None = None

    def batches(self) -> list[dict]:
        if not self.batch_root.is_dir():
            return [{"name": self.batch_name, "root": str(self.root)}]
        return [{"name": path.name, "root": str(path)} for path in sorted(self.batch_root.iterdir()) if path.is_dir() and (path / "runs").is_dir()]

    def _page_dir(self, sample_id: str, page: int) -> Path:
        if not SAFE_SAMPLE.fullmatch(sample_id) or page < 1:
            raise ValueError("invalid sample or page")
        path = self.root / "runs" / sample_id / f"page_{page:04d}"
        if not path.is_dir():
            raise FileNotFoundError(path)
        return path

    def _evidence_path(self, sample_id: str, page: int) -> Path:
        return self.root / "evidence" / sample_id / f"page_{page:04d}.json"

    def _status_path(self, sample_id: str, page: int) -> Path:
        return self._page_dir(sample_id, page) / "status.json"

    def _stat_signature(self, path: Path) -> tuple | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _cached_status(self, path: Path) -> dict | None:
        signature = self._stat_signature(path)
        if signature is None:
            return None
        key = str(path)
        cached = self._status_cache.get(key)
        if cached is not None and cached[0] == signature:
            self._status_cache.move_to_end(key)
            return cached[1]
        raw = read_json(path)
        self._status_cache[key] = (signature, raw if isinstance(raw, dict) else {})
        self._status_cache.move_to_end(key)
        while len(self._status_cache) > self._status_cap:
            self._status_cache.popitem(last=False)
        return self._status_cache[key][1]

    def effective_status(self, sample_id: str, page: int, automatic_status: str) -> dict:
        raw = self._cached_status(self._status_path(sample_id, page))
        if not isinstance(raw, dict) or raw.get("status") not in REVIEW_STATUSES:
            return {"status": automatic_status, "note": "", "updated_at": None}
        return raw

    def _runs_signature(self) -> tuple:
        """Cheap change detector: no result.json is opened, only directory metadata."""
        runs = self.root / "runs"
        count = 0
        newest = 0.0
        try:
            with os.scandir(runs) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        count += 1
                        newest = max(newest, entry.stat().st_mtime)
                    except OSError:
                        continue
        except OSError:
            return (0, 0.0)
        try:
            newest = max(newest, runs.stat().st_mtime)
        except OSError:
            pass
        return (count, round(newest, 2))

    def list_pages(self) -> dict:
        signature = self._runs_signature()
        now = time.monotonic()
        cached = self._catalog_cache
        if cached is not None and cached[0] == signature and now - self._catalog_at < self._catalog_ttl:
            return cached[1]
        payload = self._build_catalog()
        self._catalog_cache = (signature, payload)
        self._catalog_at = now
        return payload

    def _build_catalog(self) -> dict:
        samples: dict[str, list[dict]] = {}
        runs = self.root / "runs"
        if runs.is_dir():
            for result_path in sorted(runs.glob("*/page_*/result.json")):
                signature = self._stat_signature(result_path)
                if signature is None:
                    continue
                key = (str(result_path.parent.parent.name), result_path.parent.name)
                cached = self._page_meta.get(key)
                # A page is re-read only when its result.json changed: the catalog is polled while a
                # batch is writing, and parsing every result.json on each poll dominated the request.
                if cached is not None and cached[0] == signature:
                    item = cached[1]
                else:
                    try:
                        result = read_json(result_path)
                    except OSError:
                        # a page whose files are being written/moved: skip it for this poll instead of
                        # failing the whole batch (an annotation service must never 404 a live batch
                        # because one page is mid-write)
                        continue
                    if not isinstance(result, dict):
                        continue
                    validation = result.get("validation") or {}
                    item = {
                        "sample_id": str(result.get("sample_id") or result_path.parent.parent.name),
                        "page": int(result.get("page") or result_path.parent.name.split("_")[-1]),
                        "automatic_status": str(result.get("status", "CHECK")),
                        "block_count": len((result.get("revision") or {}).get("blocks") or []),
                        "task_count": result.get("task_count", 0),
                        "error_count": validation.get("error_count", 0),
                        "review_count": validation.get("review_count", 0),
                        "unresolved_field_count": validation.get("unresolved_field_count", 0),
                    }
                    self._page_meta[key] = (signature, item)
                try:
                    flag = self.load_page_flag(item["sample_id"], item["page"])["flag"]
                    status = self.effective_status(item["sample_id"], item["page"], item["automatic_status"])["status"]
                except OSError:
                    # page-level metadata unreadable (moved mid-write): keep the page with its
                    # automatic status rather than failing the catalog
                    flag, status = None, item["automatic_status"]
                samples.setdefault(item["sample_id"], []).append({
                    "sample_id": item["sample_id"],
                    "page": item["page"],
                    "flag": flag,
                    "status": status,
                    "block_count": item["block_count"],
                    "task_count": item["task_count"],
                    "error_count": item["error_count"],
                    "review_count": item["review_count"],
                    "unresolved_field_count": item["unresolved_field_count"],
                })
        documents = []
        for sample_id, pages in sorted(samples.items()):
            pages.sort(key=lambda item: item["page"])
            documents.append({
                "sample_id": sample_id,
                "pages": pages,
                "page_count": len(pages),
                "status_counts": _counts(pages, "status"),
            })
        return {"pilot_root": str(self.root), "batch": self.batch_name, "batches": self.batches(), "documents": documents}

    def page_detail(self, sample_id: str, page: int) -> dict:
        page_dir = self._page_dir(sample_id, page)
        signature = tuple(
            (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
            for path in (page_dir / "result.json", self._evidence_path(sample_id, page), page_dir / "status.json", page_dir / "decisions", page_dir / "tasks")
        )
        key = (sample_id, page)
        cached = self._detail_cache.get(key)
        if cached is not None and cached[0] == signature:
            self._detail_cache.move_to_end(key)
            return cached[1]
        payload = self._build_page_detail(sample_id, page, page_dir)
        self._detail_cache[key] = (signature, payload)
        self._detail_cache.move_to_end(key)
        while len(self._detail_cache) > self._detail_cap:
            self._detail_cache.popitem(last=False)
        return payload

    def _build_page_detail(self, sample_id: str, page: int, page_dir: Path) -> dict:
        result = read_json(page_dir / "result.json")
        evidence = read_json(self._evidence_path(sample_id, page))
        if not isinstance(result, dict) or not isinstance(evidence, dict):
            raise FileNotFoundError("result or evidence missing")
        tasks = _load_named_json(page_dir / "tasks")
        decisions = _load_named_json(page_dir / "decisions")
        contract = self._contract_gt(sample_id)
        blocks = (result.get("revision") or {}).get("blocks") or []
        if contract:
            for block in blocks:
                node_id = node_id_for_block(page, str(block.get("block_id") or ""))
                entry = {
                    "node_id": node_id,
                    "entity_id": (contract["entities"].get(node_id) or {}).get("entity_id"),
                    "role": contract["roles"].get(node_id),
                    "tree": contract["tree"].get(node_id),
                    "heading": contract["headings"].get(node_id),
                }
                block["contract_gt"] = {key: value for key, value in entry.items() if value}
        automatic_status = str(result.get("status", "CHECK"))
        review = self.effective_status(sample_id, page, automatic_status)
        return {
            "sample_id": sample_id,
            "page": page,
            "status": review["status"],
            "review": review,
            "page_image": evidence.get("page_image"),
            "pdf_page": evidence.get("pdf_page"),
            "reading_order": evidence.get("reading_order"),
            "blocks": blocks,
            "field_reviews": (result.get("revision") or {}).get("field_reviews") or {},
            "review_items": (result.get("revision") or {}).get("review_items") or [],
            "document_relations": (result.get("revision") or {}).get("document_relations") or [],
            "validation": result.get("validation") or {},
            "evidence": evidence.get("evidence") or {},
            "document_context": evidence.get("document_context") or {},
            "document_flow": self._document_flow(sample_id),
            "contract_gt": contract,
            "tasks": tasks,
            "decisions": decisions,
        }

    def _contract_gt(self, sample_id: str) -> dict:
        """Contract GT projection of this sample (written by ocr_agent.contract_gt)."""
        directory = self.root / "contract_gt" / sample_id
        bundle = read_json(directory / "full_document_gt_bundle.json")
        if not isinstance(bundle, dict):
            return {}
        return {
            "bundle_id": bundle.get("bundle_id"),
            "schema_version": bundle.get("schema_version"),
            "coordinate_space": bundle.get("coordinate_space"),
            "coverage": bundle.get("coverage") or {},
            "entities": {str(item.get("node_id")): item for item in bundle.get("a1_entities") or []},
            "roles": {str(item.get("node_id")): item.get("role") for item in bundle.get("a2_nodes") or []},
            "tree": {str(item.get("node_id")): item for item in bundle.get("a3_nodes") or []},
            "headings": {
                str(block_id): heading
                for heading in bundle.get("a2_heading_entities") or []
                for block_id in heading.get("block_ids") or []
            },
            "relations": bundle.get("a2_relations") or [],
            "table_continuations": bundle.get("table_continuations") or [],
        }

    def _document_flow(self, sample_id: str) -> dict:
        payload = read_json(self.root / "documents" / f"{sample_id}.json")
        if isinstance(payload, dict) and isinstance(payload.get("document_flow"), dict):
            return payload["document_flow"]
        return {}

    def set_status(self, sample_id: str, page: int, payload: dict) -> dict:
        page_dir = self._page_dir(sample_id, page)
        result = read_json(page_dir / "result.json")
        if not isinstance(result, dict):
            raise FileNotFoundError("result missing")
        status = payload.get("status")
        if status not in {"PASS", "FAIL"}:
            raise ValueError("manual status must be PASS or FAIL")
        automatic_status = str(result.get("status", "CHECK"))
        if automatic_status != "CHECK":
            raise ValueError("only an automatic CHECK result may be reviewed")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note must be a string of at most 2000 characters")
        review = {
            "schema_version": STATUS_SCHEMA,
            "sample_id": sample_id,
            "page": page,
            "status": status,
            "note": note.strip(),
            "updated_at": utc_now(),
        }
        write_json_atomic(page_dir / "status.json", review)
        # the review is part of both the catalog row and the page detail: drop the cached copies
        self._status_cache.pop(str(page_dir / "status.json"), None)
        self._detail_cache.pop((sample_id, page), None)
        self._catalog_cache = None
        return review

    # ---------------------------------------------------------------- annotation mode (write side)
    def page_flag_path(self, sample_id: str, page: int, annotator: str | None = None) -> Path:
        """Per-page flags live beside the patches (never inside them) so a flag alone - "this page is
        too hard to annotate" - cannot disturb the block-level annotation or its validation."""
        if not SAFE_SAMPLE.fullmatch(sample_id) or page < 1:
            raise ValueError("invalid sample or page")
        base = self.root / "annotations" / sample_id
        if annotator:
            if not SAFE_ANNOTATOR.fullmatch(annotator):
                raise ValueError("invalid annotator")
            return base / annotator / "flags" / f"page_{page:04d}.json"
        return base / "flags" / f"page_{page:04d}.json"

    def load_page_flag(self, sample_id: str, page: int, annotator: str | None = None) -> dict:
        """A page is flagged when anyone flagged it; the flagger stays anonymous."""
        raw = read_json(self.page_flag_path(sample_id, page, annotator))
        if not isinstance(raw, dict):
            raw = read_json(self.page_flag_path(sample_id, page))
        if not isinstance(raw, dict):
            plan = self.workload().get("assignments") or {}
            for item in plan.get("annotators") or []:
                candidate = read_json(self.page_flag_path(sample_id, page, str(item.get("id"))))
                if isinstance(candidate, dict) and candidate.get("flag") in PAGE_FLAGS:
                    raw = candidate
                    break
        if not isinstance(raw, dict) or raw.get("flag") not in PAGE_FLAGS:
            return {"sample_id": sample_id, "page": page, "flag": None, "note": "", "updated_at": None}
        return {"sample_id": sample_id, "page": page, "flag": raw.get("flag"),
                "note": raw.get("note", ""), "updated_at": raw.get("updated_at")}

    def set_page_flag(self, sample_id: str, page: int, payload: dict, annotator: str | None = None) -> dict:
        flag = payload.get("flag")
        note = payload.get("note", "")
        if flag is not None and flag not in PAGE_FLAGS:
            raise ValueError(f"flag must be one of {sorted(PAGE_FLAGS)} or null")
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError("note must be a string of at most 500 characters")
        # ``annotator`` (from a queue key) scopes the file; the payload's own field is only a label.
        label = annotator or str(payload.get("annotator") or "").strip()[:64] or "anonymous"
        path = self.page_flag_path(sample_id, page)
        if flag is None:
            if path.exists():
                path.unlink()
            self._catalog_cache = None
            return {"sample_id": sample_id, "page": page, "flag": None, "note": "", "updated_at": utc_now()}
        record = {
            "schema_version": PAGE_FLAG_SCHEMA,
            "sample_id": sample_id,
            "page": page,
            "flag": flag,
            "note": note.strip(),
            "annotator": label,
            "updated_at": utc_now(),
        }
        write_json_atomic(path, record)
        self._catalog_cache = None
        return {"sample_id": sample_id, "page": page, "flag": flag, "note": note.strip(), "updated_at": record["updated_at"]}

    # ------------------------------------------------------------------ annotation workload
    def workload(self) -> dict:
        """The admin assignment plan, re-read whenever ``workload/assignments.json`` changes.

        The file holds identities and the blind-QC overlap set, so it is never returned to a client:
        only :meth:`queue_for` - a bare list of sample ids - leaves this process.
        """
        path = self.root / "workload" / "assignments.json"
        signature = self._stat_signature(path) if path.is_file() else None
        if signature is None:
            return {}
        cached = self._workload_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        payload = read_json(path) or {}
        queue_of = {
            str(token): str(annotator)
            for annotator, token in (payload.get("queue_keys") or {}).items()
            if isinstance(token, str) and token
        }
        plan = {"assignments": payload, "queue_of": queue_of}
        self._workload_cache = (signature, plan)
        return plan

    def annotator_for_key(self, key: str | None) -> str | None:
        """Map an opaque queue key to an internal annotator id (None when absent or unknown)."""
        if not key or not isinstance(key, str):
            return None
        return (self.workload().get("queue_of") or {}).get(key)

    def queue_for(self, key: str | None) -> dict:
        """What an annotator may know about their own workload: document ids and counts only.

        Deliberately no identity and no overlap flags: the overlap set is a blind quality control, so
        a document that two people annotate must look exactly like any other document in the queue.
        """
        annotator = self.annotator_for_key(key)
        empty = {"exists": False, "sample_ids": [], "documents": 0, "pages": 0, "batch": self.batch_name}
        if not annotator:
            return empty
        plan = self.workload().get("assignments") or {}
        entry = next((item for item in plan.get("annotators") or [] if item.get("id") == annotator), None)
        if not isinstance(entry, dict):
            return empty
        samples = [str(value) for value in entry.get("sample_ids") or []]
        return {"exists": True, "sample_ids": samples, "documents": len(samples),
                "pages": int(entry.get("pages") or 0), "batch": self.batch_name}

    def annotation_path(self, sample_id: str, page: int, annotator: str | None = None) -> Path:
        """Annotations are stored per annotator: two people may annotate one document (blind QC).

        Without an annotator (admin use, smoke tests) the legacy flat path is used.
        """
        if not SAFE_SAMPLE.fullmatch(sample_id) or page < 1:
            raise ValueError("invalid sample or page")
        base = self.root / "annotations" / sample_id
        if annotator:
            if not SAFE_ANNOTATOR.fullmatch(annotator):
                raise ValueError("invalid annotator")
            return base / annotator / f"page_{page:04d}.json"
        return base / f"page_{page:04d}.json"

    def annotation_files(self, annotator: str | None = None) -> list[Path]:
        """Stored page patches: one annotator's own, or everyone's for the admin view.

        Scoping matters for blind quality control: with two people on the same document, an aggregate
        progress list would tell the second annotator that the page is already done.
        """
        directory = self.root / "annotations"
        if not directory.is_dir():
            return []
        if annotator:
            if not SAFE_ANNOTATOR.fullmatch(annotator):
                raise ValueError("invalid annotator")
            return sorted(path for path in directory.glob(f"*/{annotator}/page_*.json") if "/flags/" not in str(path))
        paths = set(directory.glob("*/page_*.json"))
        paths.update(directory.glob("*/*/page_*.json"))
        return sorted(path for path in paths if "/flags/" not in str(path))

    def _page_geometry(self, sample_id: str, page: int) -> tuple[dict, list[dict]]:
        """The page geometry the schema validators need, plus the chain blocks the patch is based on."""
        evidence = read_json(self._evidence_path(sample_id, page))
        result = read_json(self._page_dir(sample_id, page) / "result.json")
        if not isinstance(evidence, dict) or not isinstance(result, dict):
            raise FileNotFoundError("evidence or result missing")
        geometry = {"page_image": evidence.get("page_image") or {}, "pdf_page": evidence.get("pdf_page") or {}}
        if not geometry["page_image"].get("width") or not geometry["pdf_page"].get("width_pt"):
            raise ValueError("page geometry incomplete")
        blocks = (result.get("revision") or {}).get("blocks") or []
        return geometry, blocks

    def load_annotations(self, sample_id: str, page: int, annotator: str | None = None) -> dict:
        """Annotation patch of one page plus what the client needs to diff it against the chain result.

        ``annotator`` scopes the patch to one person (blind overlap); the stored id is never echoed.
        """
        _, chain_blocks = self._page_geometry(sample_id, page)
        stored = read_json(self.annotation_path(sample_id, page, annotator))
        if not isinstance(stored, dict) and not annotator:
            # Shared mode (no per-annotator token): show whatever was saved, whoever saved it.
            candidates = sorted(
                (self.root / "annotations" / sample_id).glob(f"*/page_{page:04d}.json"),
                key=lambda path: path.stat().st_mtime_ns,
            )
            if candidates:
                stored = read_json(candidates[-1])
        if not isinstance(stored, dict) and annotator:
            stored = read_json(self.annotation_path(sample_id, page))
        base_signature = content_signature(chain_blocks)
        if not isinstance(stored, dict):
            return {
                "schema_version": ANNOTATION_SCHEMA, "sample_id": sample_id, "page": page, "exists": False,
                "blocks": [], "operations": [], "updated_at": None, "revision": 0,
                "base_signature": base_signature, "stale": False,
                "chain_blocks": len(chain_blocks), "editable_fields": list(ANNOTATION_EDITABLE_FIELDS),
                "types": sorted(ALLOWED_TYPES), "max_title_level": ANNOTATION_MAX_TITLE_LEVEL,
            }
        stored = dict(stored)
        stored.pop("annotator", None)
        stored.pop("queue_key", None)
        stored["exists"] = True
        stored["base_signature"] = stored.get("base_signature") or base_signature
        stored["stale"] = bool(stored.get("base_signature_at_write")) and stored["base_signature_at_write"] != base_signature
        stored["chain_blocks"] = len(chain_blocks)
        stored["types"] = sorted(ALLOWED_TYPES)
        stored["max_title_level"] = ANNOTATION_MAX_TITLE_LEVEL
        stored["editable_fields"] = list(ANNOTATION_EDITABLE_FIELDS)
        return stored

    def _validate_annotated_blocks(self, blocks: Any, geometry: dict) -> list[dict]:
        if not isinstance(blocks, list):
            raise ValueError("blocks must be a list")
        if len(blocks) > MAX_ANNOTATION_BLOCKS:
            raise ValueError(f"too many blocks (max {MAX_ANNOTATION_BLOCKS})")
        seen: set[str] = set()
        cleaned: list[dict] = []
        problems: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                problems.append("block must be an object")
                continue
            block_id = str(block.get("block_id") or "")
            if not block_id:
                problems.append("block without block_id")
                continue
            if block_id in seen:
                problems.append(f"{block_id}: duplicate block_id")
                continue
            text = block.get("text")
            html = block.get("html")
            if isinstance(text, str) and len(text) > MAX_ANNOTATION_TEXT:
                problems.append(f"{block_id}: text longer than {MAX_ANNOTATION_TEXT}")
                continue
            if isinstance(html, str) and len(html) > MAX_ANNOTATION_HTML:
                problems.append(f"{block_id}: html longer than {MAX_ANNOTATION_HTML}")
                continue
            item = {key: value for key, value in block.items() if key not in {"origin", "note", "edit_state", "bbox_pdf_pt", "bbox_normalized"}}
            # the server owns the derived coordinate views so a client can never save inconsistent boxes
            add_coordinate_views(item, geometry)
            try:
                validate_block(item, page=geometry, max_title_level=ANNOTATION_MAX_TITLE_LEVEL)
            except SchemaError as exc:
                problems.append(str(exc))
                continue
            seen.add(block_id)
            # store the normalized copy: the saved annotation is directly usable as a revision block
            cleaned.append(item)
        if problems:
            preview = "; ".join(problems[:6])
            raise ValueError(f"{len(problems)} block(s) invalid: {preview}")
        return cleaned

    def save_annotations(self, sample_id: str, page: int, payload: dict, annotator: str | None = None) -> dict:
        geometry, chain_blocks = self._page_geometry(sample_id, page)
        blocks = self._validate_annotated_blocks(payload.get("blocks"), geometry)
        # ``annotator`` (from a queue key) scopes the stored file; the payload's own field is a label.
        label = annotator or str(payload.get("annotator") or "").strip()[:64] or "anonymous"
        operations = payload.get("operations") or []
        if not isinstance(operations, list) or len(operations) > 5000:
            raise ValueError("operations must be a list of at most 5000 entries")
        log = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise ValueError("operation must be an object")
            kind = str(operation.get("kind") or "")
            if kind not in {"add", "delete", "update", "retype", "move", "merge"}:
                raise ValueError(f"operation {index}: unsupported kind {kind!r}")
            log.append({
                "op_id": f"op.{index + 1:05d}",
                "kind": kind,
                "block_id": str(operation.get("block_id") or "")[:120],
                "at": utc_now(),
                "fields": [str(value)[:40] for value in (operation.get("fields") or []) if isinstance(value, (str, int))],
                "note": str(operation.get("note") or "")[:200],
                "annotator": label,
            })
        previous = read_json(self.annotation_path(sample_id, page, annotator)) or {}
        signature = content_signature(blocks)
        # An annotated page is meant to be exportable as a legal ocr-agent revision: keep the review
        # bookkeeping the pipeline would have written, so the patch alone is enough to rebuild one.
        field_reviews = self._annotation_field_reviews(sample_id, page, chain_blocks, blocks)
        revision_report = self._annotation_revision_report(sample_id, page, blocks, field_reviews)
        record = {
            "schema_version": ANNOTATION_SCHEMA,
            "batch": self.batch_name,
            "sample_id": sample_id,
            "page": page,
            "annotator": label,
            "revision": int(previous.get("revision") or 0) + 1,
            "updated_at": utc_now(),
            "base_signature_at_write": content_signature(chain_blocks),
            "signature": signature,
            "chain_blocks": len(chain_blocks),
            "annotated_blocks": len(blocks),
            "operations": (previous.get("operations") or []) + log,
            "blocks": blocks,
            "field_reviews": field_reviews,
            "revision_validation": revision_report,
        }
        write_json_atomic(self.annotation_path(sample_id, page, annotator), record)
        return self.load_annotations(sample_id, page, annotator)

    REVIEWED_FIELDS = ("type", "level", "text", "html", "parent_id", "bbox_image_px")

    def _annotation_field_reviews(self, sample_id: str, page: int, chain_blocks: list[dict], blocks: list[dict]) -> dict:
        """Per-field review status for the annotated revision.

        Unchanged blocks keep the pipeline's own reviews; blocks the annotator touched (or created) get
        an explicit human review for exactly the fields that are filled in. Without this the exported
        revision would claim fields nobody reviewed.
        """
        result = read_json(self._page_dir(sample_id, page) / "result.json") or {}
        chain_reviews = ((result.get("revision") or {}).get("field_reviews") or {}).get("blocks") or {}
        chain = {block["block_id"]: block for block in chain_blocks}
        reviews: dict[str, dict] = {}
        for block in blocks:
            block_id = block.get("block_id", "")
            original = chain.get(block_id)
            unchanged = original is not None and not any(
                json.dumps(original.get(field), sort_keys=True) != json.dumps(block.get(field), sort_keys=True)
                for field in self.REVIEWED_FIELDS
            )
            if unchanged:
                inherited = chain_reviews.get(block_id)
                if inherited:
                    reviews[block_id] = inherited
                    continue
            fields: dict[str, dict] = {}
            for field in ("type", "bbox"):
                fields[field] = {"status": "PASS", "method": "manual_annotation", "evidence_refs": list(block.get("source_refs") or [])[:12]}
            if isinstance(block.get("level"), int):
                fields["level"] = {"status": "PASS", "method": "manual_annotation", "evidence_refs": []}
            if isinstance(block.get("text"), str):
                fields["text"] = {"status": "PASS", "method": "manual_annotation", "evidence_refs": []}
            if isinstance(block.get("html"), str):
                fields["html"] = {"status": "PASS", "method": "manual_annotation", "evidence_refs": []}
            if block.get("parent_id"):
                fields["parent_id"] = {"status": "PASS", "method": "manual_annotation", "evidence_refs": []}
            reviews[block_id] = fields
        return {"blocks": reviews}

    def _annotation_revision_report(self, sample_id: str, page: int, blocks: list[dict], field_reviews: dict | None = None) -> dict:
        """Run the pipeline's own revision validator over the annotated blocks.

        The annotator is allowed to disagree with the pipeline (dropping a block leaves its evidence
        unclaimed, which is a legitimate human decision), so this never blocks a save - it is recorded
        in the patch and surfaced in the viewer so the export knows what to expect.
        """
        evidence = read_json(self._evidence_path(sample_id, page))
        if not isinstance(evidence, dict):
            return {"errors": [], "error_count": 0, "review_count": 0, "checked": False}
        page_view = dict(evidence)
        page_view.setdefault("sample_id", sample_id)
        page_view.setdefault("page", page)
        try:
            report = validate_revision(page_view, {"blocks": blocks, "field_reviews": field_reviews or {}}, max_title_level=ANNOTATION_MAX_TITLE_LEVEL)
        except Exception as exc:  # pragma: no cover - a validator crash must not lose the annotation
            return {"errors": [], "error_count": 0, "review_count": 0, "checked": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        errors = [{"code": item.get("code"), "message": str(item.get("message"))[:300], "block_ids": item.get("block_ids", [])} for item in report.get("errors", [])][:50]
        return {
            "checked": True,
            "error_count": len(report.get("errors", [])),
            "review_count": report.get("review_count", 0),
            "unresolved_field_count": report.get("unresolved_field_count", 0),
            "errors": errors,
        }

    def clear_annotations(self, sample_id: str, page: int, annotator: str | None = None) -> dict:
        """Drop one annotator's patch of a page (their own reset), leaving other annotators' work alone."""
        path = self.annotation_path(sample_id, page, annotator)
        if path.is_file():
            path.unlink()
        return self.load_annotations(sample_id, page, annotator)

    def document_annotation_path(self, sample_id: str, annotator: str | None = None) -> Path:
        if not SAFE_SAMPLE.fullmatch(sample_id):
            raise ValueError("invalid sample")
        base = self.root / "annotations" / sample_id
        if annotator:
            if not SAFE_ANNOTATOR.fullmatch(annotator):
                raise ValueError("invalid annotator")
            return base / annotator / "document.json"
        return base / "document.json"

    def _annotated_page_count(self, sample_id: str, annotator: str | None = None) -> int:
        base = self.root / "annotations" / sample_id
        if annotator:
            return len(list(base.glob(f"{annotator}/page_*.json")))
        pages = set(base.glob("page_*.json"))
        pages.update(base.glob("*/page_*.json"))
        return len(pages)

    def load_document_annotation(self, sample_id: str, annotator: str | None = None) -> dict:
        stored = read_json(self.document_annotation_path(sample_id, annotator))
        annotated = self._annotated_page_count(sample_id, annotator)
        if not isinstance(stored, dict):
            return {"schema_version": DOCUMENT_ANNOTATION_SCHEMA, "sample_id": sample_id, "exists": False,
                    "status": "", "note": "", "annotated_pages": annotated, "updated_at": None}
        stored = dict(stored)
        stored.pop("annotator", None)
        stored["exists"] = True
        stored["annotated_pages"] = annotated
        return stored

    def set_document_annotation(self, sample_id: str, payload: dict, annotator: str | None = None) -> dict:
        """Document level verdict of the annotation effort: did this document get annotated successfully?"""
        status = str(payload.get("status") or "")
        if status not in DOCUMENT_ANNOTATION_STATUSES:
            raise ValueError(f"document status must be one of {sorted(DOCUMENT_ANNOTATION_STATUSES)}")
        note = str(payload.get("note") or "")[:2000]
        label = annotator or str(payload.get("annotator") or "").strip()[:64] or "anonymous"
        pages = self._annotated_page_count(sample_id, annotator)
        record = {
            "schema_version": DOCUMENT_ANNOTATION_SCHEMA,
            "sample_id": sample_id,
            "status": status,
            "note": note.strip(),
            "annotator": label,
            "annotated_pages": pages,
            "updated_at": utc_now(),
        }
        write_json_atomic(self.document_annotation_path(sample_id, annotator), record)
        return self.load_document_annotation(sample_id, annotator)

    def annotation_summary(self, annotator: str | None = None) -> dict:
        """Progress overview: which pages carry manual annotations and how they differ from the chain.

        With ``annotator`` set this is *their* progress only (the client sends its queue key), which is
        what keeps an overlap assignment blind.
        """
        rows = []
        newest: dict[tuple[str, int], dict] = {}
        for path in self.annotation_files(annotator):
            record = read_json(path)
            if not isinstance(record, dict):
                continue
            sample_id = str(record.get("sample_id") or path.parent.parent.name or path.parent.name)
            page = int(record.get("page") or path.stem.split("_")[-1])
            operations = record.get("operations") or []
            kinds: dict[str, int] = {}
            for operation in operations:
                key = str((operation or {}).get("kind"))
                kinds[key] = kinds.get(key, 0) + 1
            candidate = {
                "sample_id": sample_id, "page": page,
                "updated_at": record.get("updated_at"), "revision": record.get("revision"),
                "annotated_blocks": record.get("annotated_blocks"), "chain_blocks": record.get("chain_blocks"),
                "operations": len(operations), "operation_kinds": kinds,
            }
            # Two annotators may hold the same page (blind overlap): report one aggregate row and
            # never reveal who wrote what.
            current = newest.get((sample_id, page))
            if current is None or str(candidate.get("updated_at") or "") > str(current.get("updated_at") or ""):
                newest[(sample_id, page)] = candidate
        rows = sorted(newest.values(), key=lambda row: (row["sample_id"], row["page"]))
        documents = {}
        for row in rows:
            documents.setdefault(row["sample_id"], 0)
            documents[row["sample_id"]] += 1
        document_status = {}
        # Every concluded document counts, not only those that already have page patches: an annotator
        # who reads a document and records a verdict before annotating pages must keep that badge.
        concluded = set(documents)
        for path in (self.root / "annotations").glob("*/document.json"):
            concluded.add(path.parent.name)
        for path in (self.root / "annotations").glob("*/*/document.json"):
            concluded.add(path.parent.parent.name)
        for sample_id in sorted(concluded):
            record = read_json(self.document_annotation_path(sample_id, annotator)) or read_json(self.document_annotation_path(sample_id))
            if isinstance(record, dict) and record.get("status") in DOCUMENT_ANNOTATION_STATUSES:
                document_status[sample_id] = {"status": record["status"], "note": record.get("note", ""),
                                              "updated_at": record.get("updated_at")}
        return {"batch": self.batch_name, "annotated_pages": len(rows), "pages": rows,
                "documents": [{"sample_id": key, "annotated_pages": value} for key, value in sorted(documents.items())],
                "document_status": document_status}

    def image_path(self, sample_id: str, page: int, kind: str) -> Path:
        page_dir = self._page_dir(sample_id, page)
        if kind == "overlay":
            path = page_dir / "final_overlay.png"
        elif kind == "source":
            evidence = read_json(self._evidence_path(sample_id, page))
            image = (evidence or {}).get("page_image") or {}
            path = Path(str(image.get("path", "")))
        else:
            raise ValueError("image kind must be source or overlay")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path


def _counts(items: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        value = str(item.get(field, "unknown"))
        out[value] = out.get(value, 0) + 1
    return out


def _load_named_json(directory: Path) -> list[dict]:
    out = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        value = read_json(path)
        if value is not None:
            out.append({"name": path.name, "content": value})
    return out


class Handler(BaseHTTPRequestHandler):
    catalog: OCRAgentCatalog
    _batch_catalogs: dict[str, OCRAgentCatalog] = {}

    def _catalog(self, query: dict | None = None, payload: dict | None = None) -> OCRAgentCatalog:
        """The catalog **of the batch this request is about**.

        Several annotators (and several open tabs) share one server, so the current batch must never be
        global state: a save has to land in the batch the client is looking at. ``batch`` travels with
        the request; without it the server's default batch is used for backwards compatibility.
        """
        name = None
        if query:
            value = (query.get("batch") or [None])[0]
            if value:
                name = str(value)
        if not name and payload:
            value = payload.get("batch")
            if isinstance(value, str) and value:
                name = value
        if not name or name == Handler.catalog.batch_name:
            return Handler.catalog
        if name not in Handler._batch_catalogs:
            root = (Handler.catalog.batch_root / name).resolve()
            if root.parent != Handler.catalog.batch_root.resolve() or not (root / "runs").is_dir():
                available = ", ".join(item["name"] for item in Handler.catalog.batches())
                raise FileNotFoundError(f"unknown batch {name!r}; available: {available}")
            Handler._batch_catalogs[name] = OCRAgentCatalog(root)
        return Handler._batch_catalogs[name]

    def _queue_key(self, query: dict | None = None, payload: dict | None = None) -> str | None:
        """The opaque queue key travels in the query string or the request body; it is never echoed."""
        if query:
            value = (query.get("t") or [None])[0]
            if value:
                return str(value)
        if payload:
            value = payload.get("queue_key")
            if isinstance(value, str) and value:
                return value
        return None

    def _annotator(self, query: dict | None = None, payload: dict | None = None) -> str | None:
        return self._catalog(query, payload).annotator_for_key(self._queue_key(query, payload))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        try:
            if route == "/api/queue":
                self._json(200, self._catalog(query).queue_for(self._queue_key(query)))
                return
            if route == "/api/catalog":
                # never mutate the shared default catalog: its root/batch_name are read by concurrent
                # requests, and switching it in place made one annotator's page land in another
                # batch's root (wrong batch label, and page lookups failing across batches)
                self._json(200, self._catalog(query).list_pages())
                return
            match = re.fullmatch(r"/api/page/([^/]+)/(\d+)", route)
            if match:
                self._json(200, self._catalog(query).page_detail(unquote(match.group(1)), int(match.group(2))))
                return
            match = re.fullmatch(r"/api/annotations/([^/]+)/(\d+)", route)
            if match:
                self._json(200, self._catalog(query).load_annotations(unquote(match.group(1)), int(match.group(2)), self._annotator(query)))
                return
            match = re.fullmatch(r"/api/document-annotations/([^/]+)", route)
            if match:
                self._json(200, self._catalog(query).load_document_annotation(unquote(match.group(1)), self._annotator(query)))
                return
            match = re.fullmatch(r"/api/page-flags/([^/]+)/(\d+)", route)
            if match:
                self._json(200, self._catalog(query).load_page_flag(unquote(match.group(1)), int(match.group(2)), self._annotator(query)))
                return
            if route == "/api/annotations":
                self._json(200, self._catalog(query).annotation_summary(self._annotator(query)))
                return
            match = re.fullmatch(r"/api/image/([^/]+)/(\d+)/(source|overlay)", route)
            if match:
                self._file(self._catalog(query).image_path(unquote(match.group(1)), int(match.group(2)), match.group(3)))
                return
            if route == "/favicon.ico":
                # browsers always ask for it; answering keeps the console clean for annotators
                self._bytes(200, FAVICON_SVG, "image/svg+xml")
                return
            if route == "/":
                route = "/static/index.html"
            if route.startswith("/static/"):
                relative = route.removeprefix("/static/")
                path = (ROOT / "static" / relative).resolve()
                static_root = (ROOT / "static").resolve()
                if path != static_root and static_root not in path.parents:
                    raise ValueError("invalid static path")
                self._file(path)
                return
            self._json(404, {"error": "not found"})
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        match = re.fullmatch(r"/api/status/([^/]+)/(\d+)", route)
        if match:
            self._post_status(match)
            return
        match = re.fullmatch(r"/api/annotations/([^/]+)/(\d+)", route)
        if match:
            self._post_annotations(match)
            return
        match = re.fullmatch(r"/api/document-annotations/([^/]+)", route)
        if match:
            self._post_document_annotations(match)
            return
        match = re.fullmatch(r"/api/page-flags/([^/]+)/(\d+)", route)
        if match:
            try:
                self._post_page_flag(match)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def _post_status(self, match: "re.Match[str]") -> None:
        try:
            payload = self._read_body(10000)
            review = self._catalog(parse_qs(urlparse(self.path).query), payload).set_status(unquote(match.group(1)), int(match.group(2)), payload)
            self._json(200, review)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})

    def _post_page_flag(self, match: "re.Match[str]") -> None:
        payload = self._read_body(8 * 1024)
        query = parse_qs(urlparse(self.path).query)
        self._json(200, self._catalog(query, payload).set_page_flag(unquote(match.group(1)), int(match.group(2)), payload, self._annotator(query, payload)))

    def _delete_page_flag(self, match: "re.Match[str]") -> None:
        query = parse_qs(urlparse(self.path).query)
        self._json(200, self._catalog(query).set_page_flag(unquote(match.group(1)), int(match.group(2)), {"flag": None}, self._annotator(query)))

    def _post_annotations(self, match: "re.Match[str]") -> None:
        try:
            payload = self._read_body(8_000_000)
            query = parse_qs(urlparse(self.path).query)
            saved = self._catalog(query, payload).save_annotations(unquote(match.group(1)), int(match.group(2)), payload, self._annotator(query, payload))
            self._json(200, saved)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})

    def _post_document_annotations(self, match: "re.Match[str]") -> None:
        try:
            payload = self._read_body(20_000)
            query = parse_qs(urlparse(self.path).query)
            saved = self._catalog(query, payload).set_document_annotation(unquote(match.group(1)), payload, self._annotator(query, payload))
            self._json(200, saved)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        flag_match = re.fullmatch(r"/api/page-flags/([^/]+)/(\d+)", route)
        if flag_match:
            try:
                self._delete_page_flag(flag_match)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            return
        match = re.fullmatch(r"/api/annotations/([^/]+)/(\d+)", route)
        if not match:
            self._json(404, {"error": "not found"})
            return
        try:
            query = parse_qs(urlparse(self.path).query)
            cleared = self._catalog(query).clear_annotations(unquote(match.group(1)), int(match.group(2)), self._annotator(query))
            self._json(200, cleared)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})

    def _read_body(self, maximum: int) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > maximum:
            raise ValueError(f"invalid request size (max {maximum} bytes)")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    @staticmethod
    def _maybe_gzip(body: bytes, accept: bool, minimum: int = 1024) -> tuple[bytes, bool]:
        """Compress JSON/static text: the catalog alone is ~450 KB of highly redundant JSON."""
        if not accept or len(body) < minimum:
            return body, False
        packed = gzip.compress(body, compresslevel=5)
        return (packed, True) if len(packed) < len(body) else (body, False)

    def _json(self, status: int, value: object) -> None:
        body = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        body, packed = self._maybe_gzip(body, self._accepts_gzip())
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if packed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, mime: str) -> None:
        """Answer with an in-memory asset (no file on disk)."""
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        body = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        compressible = mime.startswith("text/") or mime in {"application/javascript", "application/json", "image/svg+xml"}
        body, packed = self._maybe_gzip(body, compressible and self._accepts_gzip(), minimum=4096)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        if packed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def resolve_pilot_root(explicit: Path | None) -> Path:
    """Pick a usable batch: the requested one, the default, or any batch directory that has runs/."""
    for candidate in (explicit, DEFAULT_PILOT_ROOT):
        if candidate and (Path(candidate) / "runs").is_dir():
            return Path(candidate)
    parent = Path(explicit or DEFAULT_PILOT_ROOT).parent
    if parent.is_dir():
        for entry in sorted(parent.iterdir()):
            if (entry / "runs").is_dir():
                return entry
    return Path(explicit or DEFAULT_PILOT_ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description="FolioScribe annotation and review viewer")
    parser.add_argument("--data-root", "--pilot-root", dest="pilot_root", type=Path, default=DEFAULT_PILOT_ROOT)
    parser.add_argument("--batches-root", type=Path, help="parent directory containing batch result roots")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8745)
    args = parser.parse_args()
    if not (args.pilot_root / "runs").is_dir():
        raise SystemExit(f"runs directory not found: {args.pilot_root / 'runs'}")
    Handler.catalog = OCRAgentCatalog(args.pilot_root)
    if args.batches_root:
        Handler.catalog.batch_root = args.batches_root.resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"FolioScribe Viewer: http://{args.host}:{args.port}")
    print(f"Pilot root: {args.pilot_root.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
