"""Portable document registries for annotation batches."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_registry(path: Path, count: int | None = None) -> list[dict[str, Any]]:
    """Resolve asset paths relative to the registry, and validate before any writes."""
    path = path.resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("documents") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"registry must contain a non-empty documents list: {path}")
    if count is not None and count < 0:
        raise ValueError("count must be non-negative")
    prepared = []
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"document {index} must be an object")
        entry = dict(row)
        for key in ("sample_id", "full_id", "short_id"):
            value = entry.get(key)
            if not isinstance(value, str) or value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise ValueError(f"document {index}: invalid {key}")
        if entry["sample_id"] in seen:
            raise ValueError(f"duplicate sample_id: {entry['sample_id']}")
        seen.add(entry["sample_id"])
        pages = entry.get("pages")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 1:
            raise ValueError(f"document {index}: pages must be a positive integer")
        for key in ("pdf", "mineru_root", "paddle_root", "ovis_root", "source_docx"):
            value = entry.get(key)
            if key == "source_docx" and value is None:
                entry[key] = None
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"document {index}: {key} must be an explicit path")
            asset = Path(value).expanduser()
            entry[key] = str((path.parent / asset).resolve())
        prepared.append(entry)
    return prepared[:count] if count else prepared
