"""页级解析契约 v3：六类型标记 DSL（Markdown 主体）+ 状态槽位 + 层级推导。

这是训练与推理的**唯一来源**。训练投影（sft/projection.py）与
推理状态机（sft/document_state.py）都必须从这里导入，禁止各自实现一套。

输出格式（一行一块，标记独占行，内容为 Markdown）：
  <|header|>页眉文本
  <|footer|>页脚文本（含页码）
  <|title|><|level|>N<|text|>标题文本          # 后处理按 N 重建 markdown 的 #
  <|text|>正文/列表/公式/caption
  <|table|><table>…</table><|loc|>page,x1,y1,x2,y2
  <|image|><|loc|>page,x1,y1,x2,y2
跨页：块尾 …<|truncated|>；下页续块 <|continued|><|table|>…
"""
from __future__ import annotations

import json
import re
from typing import Any

CONTRACT_VERSION = "contract-v3.1"
PROMPT_TEMPLATE_VERSION = "prompt-v7"
IMAGE_SPEC_VERSION = "image-v1"

EMPTY_SLOT = "<empty/>"

# ---------------------------------------------------------------- 类型词表
BLOCK_TYPES = ("header", "footer", "title", "text", "table", "image")
LEVEL_MIN, LEVEL_MAX = 1, 6

TAG_RE = re.compile(r"^<\|(header|footer|title|text|table|image)\|>(.*)$", re.S)
LEVEL_RE = re.compile(r"^<\|level\|>(\d+)(?:<\|text\|>|$)")
LOC_RE = re.compile(r"<\|loc\|>\s*(\d+)(?:\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+))?\s*$")
TRUNCATED_RE = re.compile(r"<\|truncated\|>\s*$")
CONTINUED_RE = re.compile(r"^<\|continued\|>")

# ---------------------------------------------------------------- 状态槽位预算
TAIL_MIN_CHARS = 120
TAIL_MAX_CHARS = 400
TAIL_MAX_LINES = 6
CLOSED_RECENT_MAX = 3
OPEN_TREE_MAX = 8

# 图像像素预算
IMAGE_MIN_PIXELS = 448 * 448
IMAGE_MAX_PIXELS = 2880 * 2880

INSTRUCTION = """Parse the attached page image into marked Markdown blocks that continue the
document being built from its preceding pages.

Context rules:
- <title_tree> is the document state after the previous page: open_tree is the chain of headings
  still in effect (deepest last); closed_recent lists headings just closed. Choose the title
  level N so the heading nests correctly under open_tree: a sibling of the last entry keeps its
  level, a subsection gets last level + 1, and a heading that closes the current section takes
  the level of that section's parent. Never re-emit a heading that open_tree or closed_recent
  already contains unless that heading is visible again on this page.
- <prev_page_tail> is the tail of the previous page's output. If the last block was cut off
  mid-way, start this page with a <|continued|> block of the same type and continue seamlessly;
  never repeat the tail content. If it is <empty/>, this is the first page.

Output rules:
- One block per line; each block starts with exactly one type mark; the content after the mark
  is Markdown. Do not output JSON, explanations, or extra text.
- Follow the natural reading order of the page.
- Title: <|title|><|level|>N<|text|>TITLE_TEXT, where N is 1-6. Do not add markdown # yourself;
  the # is rebuilt from N.
- Text: <|text|>CONTENT. Paragraphs, list items (- / 1.), formulas ($$...$$) and captions are
  all text blocks; keep list markers and formula delimiters in CONTENT.
- Table: <|table|><table>...</table>. Tables are HTML; never use markdown pipes.
- Image: <|image|><|loc|>page,x1,y1,x2,y2. The mark plus location is the whole block.
- Header/footer: <|header|>TEXT / <|footer|>TEXT for page-top / page-bottom furniture; page
  numbers belong to footer. Do not output them as text blocks.
- Every block ends with <|loc|>page (0-based absolute page id, shown below). Table and image
  blocks use the full form <|loc|>page,x1,y1,x2,y2 with coordinates normalized to [0, 1000).
- Emit <|truncated|> at the end of a text/table block that is cut off and continues on the next
  page. Only text and table blocks can be truncated/continued; other blocks never are.
- Finish after the final complete block. Do not add a terminator or closing mark."""


# ---------------------------------------------------------------- prompt 构造
def build_prompt(page_no: int, page_count: int, title_tree: dict[str, Any], prev_page_tail: str,
                 instruction: str | None = None) -> str:
    """固定模板：不变指令 + Page 行 + 两个状态槽位。槽位顺序与措辞都是契约的一部分。"""
    tree = json.dumps(title_tree, ensure_ascii=False, separators=(", ", ": ")) if title_tree else EMPTY_SLOT
    previous = (prev_page_tail or "").strip() or EMPTY_SLOT
    return (
        f"{instruction or INSTRUCTION}\n\n"
        f"Page: {page_no} / {page_count}\n\n"
        f"<title_tree>\n{tree}\n</title_tree>\n\n"
        f"<prev_page_tail>\n{previous}\n</prev_page_tail>"
    )


def build_title_tree(open_tree: list[dict[str, Any]] | None,
                     closed_recent: list[dict[str, Any]] | None) -> dict[str, Any]:
    """构造 <title_tree> 槽位内容（训练投影与推理 DocState 共用）。

    两分量：open_tree（未闭合标题路径，层级推导锚点）+ closed_recent（最近闭合，
    防重复输出旧标题）。跨页表状态不进 prompt：续块内容由模型输出，表头去重是
    DocState 合并期的事（原 open_table 与 prev_page_tail 冗余，v3 去除）。
    """
    return {
        "open_tree": (open_tree or [])[-OPEN_TREE_MAX:],
        "closed_recent": (closed_recent or [])[-CLOSED_RECENT_MAX:],
    }


# ---------------------------------------------------------------- DSL 解析
def strip_outer_fence(text: str) -> tuple[str, bool]:
    lines = str(text or "").strip("\n").split("\n")
    if len(lines) >= 2 and re.match(r"^\s*```", lines[0]) and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1]), True
    return text, False


def parse_block_line(line: str) -> dict[str, Any] | None:
    """解析一行 DSL 块 -> {type, level?, content, page?, bbox?, truncated?, continued?}。"""
    line = line.rstrip("\n")
    continued = bool(CONTINUED_RE.match(line))
    if continued:
        line = line[len("<|continued|>"):]
    match = TAG_RE.match(line)
    if not match:
        return None
    btype, rest = match.group(1), match.group(2)
    truncated = bool(TRUNCATED_RE.search(rest))
    rest = TRUNCATED_RE.sub("", rest).rstrip()
    block: dict[str, Any] = {"type": btype, "continued": continued, "truncated": truncated}
    if btype == "title":
        level_match = LEVEL_RE.match(rest)
        if level_match:
            block["level"] = max(LEVEL_MIN, min(LEVEL_MAX, int(level_match.group(1))))
            rest = rest[level_match.end():]
    loc = LOC_RE.search(rest)
    if loc:
        block["page"] = int(loc.group(1))
        if loc.group(2) is not None:
            block["bbox"] = [int(loc.group(i)) for i in range(2, 6)]
        rest = rest[:loc.start()].rstrip()
    block["content"] = rest.strip()
    return block


def parse_blocks(raw: str) -> list[dict[str, Any]]:
    """整页 DSL 输出 -> 块列表。非标记行并入上一个块的内容（多行 text/table）。"""
    raw, _ = strip_outer_fence(raw)
    blocks: list[dict[str, Any]] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            block = parse_block_line("\n".join(pending))
            if block is not None:
                blocks.append(block)
            pending.clear()

    for line in str(raw or "").split("\n"):
        # Parse metadata only after all continuation lines have been collected.
        # A table's loc/truncated markers may occur after its final </table>.
        if TAG_RE.match(CONTINUED_RE.sub("", line)):
            flush()
            pending.append(line)
        elif pending:
            pending.append(line)
    flush()
    return blocks


def events_of(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """块流 -> 页级 head/tail 事件（与 v2 下游语义兼容）。"""
    head = tail = None
    for block in blocks:
        if block.get("continued") and head is None and block["type"] in ("text", "table"):
            head = f"{block['type']}_from_prev"
        if block.get("truncated") and block["type"] in ("text", "table"):
            tail = f"{block['type']}_to_next"  # 页尾最后一个截断块生效
    return {"head": head, "tail": tail}


def dsl_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """块流 -> 纯 Markdown（剥标记；title 按 level 重建 #）。"""
    parts: list[str] = []
    for block in blocks:
        btype = block["type"]
        content = block.get("content") or ""
        if btype == "title":
            level = block.get("level") or 1
            if content:
                parts.append(f"{'#' * level} {content}")
        elif btype == "table":
            if content:
                parts.append(content)
        elif btype == "image":
            if block.get("bbox"):
                x1, y1, x2, y2 = block["bbox"]
                parts.append(f'<img src="images/bbox_{x1}_{y1}_{x2}_{y2}.jpg" />')
        elif btype in ("text", "header", "footer"):
            if content:
                parts.append(content)
    return "\n\n".join(parts)


def blocks_to_dsl(blocks: list[dict[str, Any]]) -> str:
    """块字典流 -> DSL 字符串（训练目标构造；与 parse_blocks 互逆）。

    块字段：type / content / level?(title) / page / bbox? / truncated? / continued?
    """
    lines: list[str] = []
    for block in blocks:
        prefix = "<|continued|>" if block.get("continued") else ""
        btype = block["type"]
        content = (block.get("content") or "").strip("\n")
        suffix = ""
        if block.get("bbox") and len(block["bbox"]) == 4:
            x1, y1, x2, y2 = block["bbox"]
            suffix = f"<|loc|>{block.get('page', 0)},{x1},{y1},{x2},{y2}"
        elif block.get("page") is not None:
            suffix = f"<|loc|>{block['page']}"
        if btype == "title":
            level = block.get("level") or 1
            lines.append(f"{prefix}<|title|><|level|>{level}<|text|>{content}{suffix}".rstrip())
        else:
            lines.append(f"{prefix}<|{btype}|>{content}{suffix}".rstrip())
        if block.get("truncated"):
            lines[-1] = lines[-1].rstrip() + "<|truncated|>"
    return "\n".join(lines)


# ---------------------------------------------------------------- 层级推导
def derive_heading_level(open_tree: list[dict[str, Any]], raw_tag_level: int | None = None) -> int:
    """标题层级推导（训推同源）：新标题层级由状态树推导，raw tag 仅作参考。

      - open_tree 为空    -> 1
      - raw 与末元素同层  -> 同级
      - raw 更深          -> 末元素 level + 1（封顶 +2）
      - raw 更浅          -> 回退到 open_tree 中最近的同层祖先
    """
    if not open_tree:
        return 1
    parent = open_tree[-1]["level"]
    if raw_tag_level is None:
        return parent + 1
    if raw_tag_level == parent:
        return parent
    if raw_tag_level > parent:
        return min(parent + 2, raw_tag_level)
    for node in reversed(open_tree):
        if node["level"] == raw_tag_level:
            return raw_tag_level
    return 1


# ---------------------------------------------------------------- prev_page_tail
def build_prev_tail(previous_body: str, truncated: bool = False, truncated_block_tail: str | None = None,
                    last_block_tail: str | None = None,
                    min_chars: int = TAIL_MIN_CHARS, max_chars: int = TAIL_MAX_CHARS,
                    max_lines: int = TAIL_MAX_LINES) -> str:
    """构造 <prev_page_tail>：总是给出（模型需要知道上文讲到哪，即使未截断）。

      - 截断：取截断块内部尾部（续接依据）
      - 未截断：取上一页最后一块尾部（上下文连续性；v1 实测"给整页会照抄"，
        尾部预算 TAIL_MAX_CHARS/MAX_LINES 控制住了照抄）
    """
    source = (truncated_block_tail if truncated else last_block_tail) or ""
    source = source.strip("\n")
    if not source:
        return _last_tail_lines(previous_body, max_chars, max_lines)
    lines = [line for line in source.split("\n") if line.strip()]
    if not lines:
        return ""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if kept and total + len(line) > max_chars:
            break
        if len(kept) >= max_lines or total >= min_chars:
            break
        kept.append(line)
        total += len(line) + 1
    tail = "\n".join(reversed(kept))
    return tail[-max_chars:] if len(tail) > max_chars else tail


def _last_tail_lines(text: str, max_chars: int = TAIL_MAX_CHARS, max_lines: int = TAIL_MAX_LINES) -> str:
    lines = [line for line in str(text or "").split("\n") if line.strip()]
    if not lines:
        return ""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if len(kept) >= max_lines or (kept and total + len(line) > max_chars):
            break
        kept.append(line)
        total += len(line) + 1
    tail = "\n".join(reversed(kept))
    return tail[-max_chars:] if len(tail) > max_chars else tail
