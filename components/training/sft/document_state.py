"""文档状态机 v3：把逐页 DSL 块流合并成整档（训练投影与推理共用同一份实现）。

与 output_format.py 的分工：contract 定义格式（标记词表/解析/推导规则），state 维护跨页事实
（未闭合块、标题树、闭合历史），并产出下一页 prompt 的两个槽位。
"""
from __future__ import annotations

import re
from typing import Any

from .output_format import (
    build_title_tree,
    derive_heading_level,
    dsl_to_markdown,
    parse_blocks,
)
from .markdown import join_text


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _html_rows(html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", cell).strip()
                 for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, flags=re.S | re.I)]
        if cells:
            rows.append(cells)
    return rows


def _html_append(target_html: str, extra_html: str, header: list[str] | None) -> str:
    """把续页 <tr> 追加进已有表格；续页重复表头时丢弃首行。"""
    extra_rows = re.findall(r"<tr[^>]*>.*?</tr>", extra_html, flags=re.S | re.I)
    if header and extra_rows:
        first_cells = [re.sub(r"<[^>]+>", "", cell).strip()
                       for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", extra_rows[0], flags=re.S | re.I)]
        if first_cells and _norm("".join(first_cells)) == _norm("".join(header)):
            extra_rows = extra_rows[1:]
    return re.sub(
        r"</table\s*>",
        lambda _: "\n".join(extra_rows) + "\n</table>",
        target_html.rstrip(), count=1, flags=re.I,
    )


class DocState:
    """逐页摄入 DSL 块流；训练（GT 事实）与推理（模型输出）共用。"""

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []        # 合并后的块（含跨页拼接）
        self.headings: list[dict[str, Any]] = []      # 全部历史标题
        self.open_block: dict[str, Any] | None = None  # 未闭合块（blocks 里的引用）
        self.pages: list[str] = []
        self.trace: list[dict[str, Any]] = []
        self._previous_blocks: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 状态快照
    def open_tree_nodes(self) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for heading in self.headings:
            while active and active[-1]["level"] >= heading["level"]:
                active.pop()
            active.append(dict(heading))
        return active

    def closed_recent(self) -> list[dict[str, Any]]:
        open_keys = {(h["level"], h["title"]) for h in self.open_tree_nodes()}
        return [h for h in self.headings if (h["level"], h["title"]) not in open_keys]

    def state_snapshot(self) -> dict[str, Any]:
        return build_title_tree(open_tree=self.open_tree_nodes(), closed_recent=self.closed_recent())

    def context_for(self, page_no: int) -> dict[str, Any]:
        """推理路径：当前状态 -> 下一页 prompt 的两个槽位。

        prev_page_tail 总是给出：截断时取截断块尾部；未截断取最后一块尾部。
        """
        from .output_format import build_prev_tail
        previous = self._previous_blocks
        tail_block = next((b for b in reversed(previous)
                           if b.get("truncated") and b["type"] in ("text", "table")), None)
        block_tail = tail_block.get("content") if tail_block else None
        last_tail = previous[-1].get("content") if previous else None
        tail = build_prev_tail("", tail_block is not None, block_tail, last_tail)
        return {"title_tree": self.state_snapshot(), "prev_page_tail": tail}

    # ------------------------------------------------------------ ingest
    def ingest(self, page_no: int, raw_dsl: str) -> dict[str, Any]:
        """摄入一页 DSL 输出；返回 trace（动作审计）。"""
        blocks = parse_blocks(raw_dsl)
        self._previous_blocks = [dict(block) for block in blocks]
        record: dict[str, Any] = {"page": page_no, "n_blocks": len(blocks), "actions": []}
        adjacent = bool(self.trace) and self.trace[-1]["page"] + 1 == page_no
        target = self.open_block if adjacent else None
        self.open_block = None
        tree = self.open_tree_nodes()
        seen_body = False
        page_body: list[dict[str, Any]] = []

        for block in blocks:
            furniture = block["type"] in ("header", "footer")
            merged = False
            if block.get("continued"):
                if (not seen_body and target is not None
                        and target["type"] == block["type"]
                        and block["type"] in ("text", "table")):
                    if block["type"] == "table":
                        previous_html = target.get("content") or ""
                        extra_html = block.get("content") or ""
                        if re.search(r"</table\s*>", previous_html, re.I) and _html_rows(extra_html):
                            target["content"] = _html_append(previous_html, extra_html, target.get("header"))
                            merged = True
                    else:
                        target["content"] = join_text(target.get("content") or "", block.get("content") or "")
                        merged = True
                    if merged:
                        target["truncated"] = block.get("truncated", False)
                        record["actions"].append(f"merge_{block['type']}")
                if not merged:
                    record["actions"].append("half_pair_head")

            if not furniture:
                seen_body = True
            if merged:
                page_body.append(target)
                continue
            if block["type"] == "title":
                level = derive_heading_level(tree, block.get("level"))
                block["level"] = level
                heading = {"level": level, "title": block.get("content") or ""}
                self.headings.append(heading)
                while tree and tree[-1]["level"] >= level:
                    tree.pop()
                tree.append(dict(heading))
            self.blocks.append(block)
            if not furniture:
                page_body.append(block)

        # Only the current page's final body block can continue to the next page.
        # Empty pages and page gaps break the pair; merged blocks keep the new flag.
        if page_body:
            last = page_body[-1]
            if last.get("truncated") and last["type"] in ("text", "table"):
                if last["type"] == "table" and not last.get("header"):
                    rows = _html_rows(last.get("content") or "")
                    last["header"] = rows[0] if rows else None
                self.open_block = last
                record["actions"].append(f"open_{last['type']}")

        self.pages.append(raw_dsl.strip("\n"))
        record["actions"] = record["actions"] or ["plain"]
        self.trace.append(record)
        return record

    # ------------------------------------------------------------ output
    def assemble_markdown(self) -> str:
        """整档 Markdown（剥标记、跨页已合并）。"""
        return dsl_to_markdown(self.blocks)

    def assemble_blocks(self) -> list[dict[str, Any]]:
        """整档结构化块（viewer / Contract GT 导出用）。"""
        return self.blocks
