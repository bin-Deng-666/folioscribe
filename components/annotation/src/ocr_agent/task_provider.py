from __future__ import annotations

import json
import time
from typing import Any

from .decision_applier import validate_semantic_response
from .errors import ProviderPermanentError, ProviderTransientError
from .provider import OpenAICompatibleProvider

RELATION_SYSTEM_PROMPT = """You adjudicate cross-page document structure from grounded OCR/PDF context.
Return JSON only. Decide whether the current page starts by continuing the previous page's table semantic content.
Use the supplied previous table candidate HTML/text and current top blocks. A continuation can lose visible grid lines after a page break. Distinguish a continuation from an independent new section by topic continuity, unfinished table cells or section labels, repeated headers, numbering and geometry.
Never rewrite text, HTML, types or bboxes. Use only supplied block IDs. If evidence is insufficient, return uncertain.
When the relation is continuation, also copy the boundary text verbatim so the claim can be checked:
boundary_before = the last few characters of the previous table fragment's content, boundary_after = the first few
characters of the current fragment's content (10-40 characters each, no rewriting, no ellipsis). A continuation whose
boundary text cannot be found in the supplied fragments is treated as unresolved.
Response: {"task_id":"...","relation":"continuation|new_section|uncertain","previous_block_id":"...|null","current_block_ids":["..."],"boundary_before":"...","boundary_after":"...","reason":"short grounded reason"}
"""

CROSS_PAGE_TEXT_SYSTEM_PROMPT = """You adjudicate cross-page text flow from grounded OCR/PDF context.
Return JSON only. Decide whether the previous page's final text block is cut mid-sentence and continues into the current page's first text block.
- continuous: the tail sentence or paragraph is unfinished and the head block directly continues the same running text.
- separate: the tail already ends a sentence or thought, or the head starts a new list item, number, heading, caption, table row, section or form field.
- uncertain: evidence is insufficient or genuinely ambiguous.
Never rewrite text, types or bboxes. Use only the supplied block IDs. An open tail alone is not proof of continuation.
Response: {"task_id":"...","relation":"continuous|separate|uncertain","tail_block_id":"...","head_block_id":"...","reason":"short grounded reason"}
"""

SEMANTIC_SYSTEM_PROMPT = """You are a conservative semantic adjudicator for document OCR blocks. Maximize field precision; abstain when grounded evidence is insufficient.
Return decisions only for supplied items. Never create text, blocks, bboxes, HTML, types, levels, parents, or evidence IDs outside each item's allowed values.

Decision procedure for every item:
1. Read semantic_context.current_page_reading_context first. Decide whether the target names and governs the content that follows, or instead carries content as part of a sentence, paragraph, list or instruction.
2. Use document_root_candidates, provisional_heading_path, nearby_heading_candidates, previous_page_tail and next_page_head only as context that may itself contain earlier model errors. A table-of-contents heading is not a replacement for an independent document title.
3. Treat source_votes as fallible model votes. Use the PDF entry to ground visible text and geometry; do not treat any source label or level_candidate as truth.
4. Decide type and, only for title, level together. Apply the supplied type_rules and semantic_hint.
5. Before returning, check compatibility with the target's surrounding prose and the visible document hierarchy.

Critical boundaries:
- title: a real document or section heading that names and governs following content. Numbering is supporting evidence, never a conclusion. Literal Markdown markers such as #, ## or ### are visible text only and must not support title type or level. A procedural step such as 步骤4 can be a title when it introduces and governs following explanation, screenshots or instructions; a numbered operation such as 4. 点击确定 is text when it carries the instruction itself or continues a list. A preceding screenshot cannot be governed by a later target. A following screenshot can be supporting content, but proximity to a screenshot alone does not turn a complete operation or instruction into a title.
- header/footer: page furniture supported by expected page-edge position and repetition across pages. A body heading, signature, date, attachment label, footnote, screenshot UI, Markdown thematic separator/horizontal rule, or prose near a margin is not page furniture merely because an OCR model used that label.
- text: ordinary prose, numbered or bulleted list items, procedural instructions, and table-of-contents entries. Preserve exact visible markers. Do not assign list or toc types.
- caption: requires exactly one nearby compatible parent. A source figure_title label alone is insufficient.
- OCR text whose box lies inside a PDF content-image object belongs to that image and must not become a top-level text, title or table. Containment is the geometric fact that decides this: text whose box lies outside the content-image box is an ordinary page text block even when it reads like a label of that image, so do not abstain on it for looking like a label.

For title level, infer the document hierarchy from the target's governing context, provisional heading path, nearby candidates and parallel items. Levels express document nesting, not model confidence, and the maximum level is 4. If an independent document title exists, it is level 1, top-level sections are level 2, and their subsections are level 3. Without an independent document title, top-level sections start at level 1. Level 4 is reserved for a demonstrable final structural subdivision under a level-3 parent. When source structure appears deeper than level 4, do not label every deeper leaf as a level-4 title: keep only genuine governing headings and classify requirement labels, attributes, list items and complete instructions as text. If a genuine heading's implied depth exceeds level 4, flatten it to text under the contract instead of returning unresolved, and apply the same flattening to its structural peers. Parallel headings with the same visible style and role must share the same type and level outcome; only explicit sequence or role differences justify different outcomes. Adjacency alone does not create a parent-child relation. If context is incomplete or competing hierarchies remain plausible, return unresolved.
If evidence cannot distinguish allowed alternatives, return unresolved instead of guessing.
Every decision cites only evidence IDs supplied on that item. Every item appears exactly once in decisions or unresolved.
Return one JSON object with no prose or markdown fences. Keep internal reasoning minimal. Reserve enough output budget for the complete JSON response; never spend the entire completion on hidden reasoning.
"""

SEMANTIC_USER_PROMPT = """Fill this semantic review sheet.
Response format:
{
  "task_id": "same task_id",
  "decisions": [
    {"block_id": "...", "type": "...", "level": 1, "parent_id": "...", "evidence_refs": ["..."]}
  ],
  "unresolved": [
    {"block_id": "...", "field": "type|level|parent_id", "reason": "..."}
  ]
}
Only title decisions contain level. Only table_caption/image_caption decisions contain parent_id.
Every task item must appear exactly once in decisions or unresolved.
TASK:
"""


def _validate_relation_response(task: dict[str, Any], response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("task_id") != task["task_id"]:
        raise ProviderPermanentError("document relation response has invalid task_id")
    if response.get("relation") not in task["allowed_relations"]:
        raise ProviderPermanentError("document relation response has invalid relation")
    previous_ids = {item.get("block_id") for item in task["previous_tables"]}
    current_ids = {item.get("block_id") for item in task["current_top_blocks"]}
    if response.get("previous_block_id") not in previous_ids | {None}:
        raise ProviderPermanentError("document relation response has invalid previous_block_id")
    block_ids = response.get("current_block_ids")
    if not isinstance(block_ids, list) or set(block_ids) - current_ids:
        raise ProviderPermanentError("document relation response has invalid current_block_ids")
    if response.get("relation") == "continuation" and (not response.get("previous_block_id") or not block_ids):
        raise ProviderPermanentError("continuation relation requires previous and current block IDs")
    for key in ("boundary_before", "boundary_after"):
        value = response.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ProviderPermanentError(f"document relation response has invalid {key}")
    return response


def _validate_cross_page_text_response(task: dict[str, Any], response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("task_id") != task["task_id"]:
        raise ProviderPermanentError("cross-page text response has invalid task_id")
    if response.get("relation") not in {"continuous", "separate", "uncertain"}:
        raise ProviderPermanentError("cross-page text response has invalid relation")
    if response.get("tail_block_id") != task["tail"]["block_id"]:
        raise ProviderPermanentError("cross-page text response has invalid tail_block_id")
    if response.get("head_block_id") != task["head"]["block_id"]:
        raise ProviderPermanentError("cross-page text response has invalid head_block_id")
    return response


def _first_json_object(content: str) -> dict[str, Any] | None:
    """Return the first balanced JSON object embedded in a completion, ignoring surrounding prose."""
    text = str(content or "")
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                    except Exception:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = text.find("{", start + 1)
    return None


class TaskProvider:
    def __init__(self, base: OpenAICompatibleProvider) -> None:
        self.base = base
        self.config = base.config

    def _post_with_transient_retry(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int, str | None, float]:
        attempts = 4
        elapsed_total = 0.0
        last_error: ProviderTransientError | None = None
        for attempt in range(attempts):
            try:
                envelope, status, request_id, elapsed = self.base._post(self.base._endpoint(), payload)
                return envelope, status, request_id, elapsed_total + elapsed
            except ProviderTransientError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    delay = 0.5 * (2 ** attempt)
                    time.sleep(delay)
                    elapsed_total += delay
        assert last_error is not None
        raise last_error

    def _complete_json_task(
        self,
        task: dict[str, Any],
        *,
        system_prompt: str,
        validator: Any,
        provider_name: str,
        max_tokens: int = 8192,
        compact_tokens: int = 4096,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": min(self.config.max_output_tokens, max_tokens),
            "response_format": {"type": "json_object"},
            **({"reasoning_effort": self.config.reasoning_effort} if self.config.reasoning_effort else {}),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(task, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        plain = dict(payload)
        plain.pop("response_format", None)
        compact = dict(plain)
        compact.pop("reasoning_effort", None)
        # The retry exists for malformed output, not for a short budget: the model spends most of the
        # completion on internal reasoning, so shrinking the cap here would recreate the empty-content
        # failure this variant is meant to recover from.
        compact["max_tokens"] = max(min(int(compact["max_tokens"]), compact_tokens), 4096)
        compact["messages"] = [
            compact["messages"][0],
            {"role": "user", "content": "Return the smallest valid JSON immediately.\n" + compact["messages"][1]["content"]},
        ]
        variants = [("json", payload), ("plain", plain), ("plain_compact_no_reasoning", compact)]
        attempts = []
        total_elapsed = 0.0
        last_error = "no response"
        for mode, request in variants:
            envelope, status, request_id, elapsed = self._post_with_transient_retry(request)
            total_elapsed += elapsed
            content = ""
            message = {}
            try:
                message = envelope.get("choices", [{}])[0].get("message", {})
                content = self.base._content_text(message.get("content", ""))
                try:
                    response = json.loads(self.base._strip_fence(content))
                except Exception:
                    # Some completions wrap the object in prose or truncate the fence; take the first
                    # balanced JSON object before giving up on this variant.
                    extracted = _first_json_object(content)
                    if extracted is None:
                        raise
                    response = extracted
                validator(task, response)
            except Exception as exc:
                response = None
                last_error = str(exc)
            attempts.append({"mode": mode, "status": "ok" if response is not None else "invalid_content", "http_status": status, "elapsed_seconds": elapsed, "content_chars": len(content), "message_keys": sorted(message), "usage": envelope.get("usage", {})})
            if response is not None:
                return response, {"provider": provider_name, "model": self.config.model, "task_id": task["task_id"], "request_id": request_id, "elapsed_seconds": round(total_elapsed, 3), "attempts": attempts, "http_status": status, "usage": envelope.get("usage", {}), "response": response}
        detail = "; ".join(f"{item['mode']}:{item['status']}:{item.get('finish_reason')}:{item.get('content_chars')}chars" for item in attempts)
        raise ProviderPermanentError(f"{provider_name} returned no valid decision after {len(attempts)} attempts: {last_error} [{detail}]")

    def complete_relation(self, task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._complete_json_task(
            task,
            system_prompt=RELATION_SYSTEM_PROMPT,
            validator=_validate_relation_response,
            provider_name="document_relation",
        )

    def complete_cross_page_text(self, task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._complete_json_task(
            task,
            system_prompt=CROSS_PAGE_TEXT_SYSTEM_PROMPT,
            validator=_validate_cross_page_text_response,
            provider_name="cross_page_text",
            compact_tokens=4096,
        )

    def complete_semantic(self, task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": min(self.config.max_output_tokens, 16384),
            "response_format": {"type": "json_object"},
            **({"reasoning_effort": self.config.reasoning_effort} if self.config.reasoning_effort else {}),
            "messages": [
                {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
                {"role": "user", "content": SEMANTIC_USER_PROMPT + json.dumps(task, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        variants = [("json", payload)]
        plain = dict(payload)
        plain.pop("response_format", None)
        variants.append(("plain", plain))
        attempts = []
        total_elapsed = 0.0
        last_error = "no response"
        for mode, request in variants:
            envelope, status, request_id, elapsed = self._post_with_transient_retry(request)
            total_elapsed += elapsed
            content = ""
            message: dict[str, Any] = {}
            finish_reason = None
            reasoning_chars = 0
            try:
                choice = envelope["choices"][0]
                message = choice["message"]
                finish_reason = choice.get("finish_reason")
                content = self.base._content_text(message.get("content", ""))
                reasoning_chars = len(str(message.get("reasoning_content", "") or ""))
                parsed = json.loads(self.base._strip_fence(content))
                validate_semantic_response(task, parsed)
            except Exception as exc:
                parsed = None
                last_error = str(exc)
            attempts.append({"mode": mode, "status": "ok" if parsed is not None else "invalid_content", "http_status": status, "elapsed_seconds": elapsed, "content_chars": len(content), "reasoning_chars": reasoning_chars, "finish_reason": finish_reason, "message_keys": sorted(message), "usage": envelope.get("usage", {})})
            if parsed is not None:
                return parsed, {
                    "provider": "semantic_task",
                    "model": self.config.model,
                    "task_id": task["task_id"],
                    "request_id": request_id,
                    "elapsed_seconds": round(total_elapsed, 3),
                    "attempts": attempts,
                    "usage": envelope.get("usage", {}),
                    "response": parsed,
                }
        error = ProviderPermanentError(f"semantic task returned no valid decision after {len(attempts)} attempts: {last_error}")
        error.trace = {"provider": "semantic_task", "model": self.config.model, "task_id": task["task_id"], "elapsed_seconds": round(total_elapsed, 3), "attempts": attempts}
        raise error


# Backward-compatible alias for existing callers.
DSTaskProvider = TaskProvider
