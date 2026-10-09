from copy import deepcopy

from helpers import make_page
from ocr_agent.schemas import add_coordinate_views, new_revision
from ocr_agent.title_consistency import (
    apply_numbering_depth_convention,
    _cover_title_stack,
    _local_hierarchy_collision,
    _parallel_layout_key,
    build_cover_title_stack_tasks,
    MAX_DOCUMENT_REVIEW_ITEMS,
    apply_title_consistency_decision,
    build_title_consistency_tasks,
    mark_ambiguous_multiline_root_groups,
)


def _page(number: int, text: str, block_type: str, level: int | None = None) -> tuple[dict, dict]:
    page = make_page()
    page["page"] = number
    page["sample_id"] = "sample"
    block = page["current_candidate"]["blocks"][0]
    old_id = block["block_id"]
    block_id = f"candidate.block.p{number}.0000"
    block["block_id"] = block_id
    block["text"] = text
    block["type"] = block_type
    block["type_candidates"] = ["text", "title"]
    if block_type == "title":
        block["level"] = level or 1
    else:
        block.pop("level", None)
    block["source_refs"] = [f"pdf.line.p{number}.0000"]
    block["support_refs"] = []
    add_coordinate_views(block, page)
    evidence = page["evidence"]["pdf"][0]
    evidence["evidence_id"] = block["source_refs"][0]
    evidence["text"] = text
    fields = page["field_reviews"]["blocks"].pop(old_id)
    fields["type"] = {"status": "PASS", "method": "semantic_task", "evidence_refs": block["source_refs"]}
    if block_type == "title":
        fields["level"] = {"status": "PASS", "method": "document_heading_sequence", "evidence_refs": block["source_refs"]}
    else:
        fields.pop("level", None)
    page["field_reviews"]["blocks"][block_id] = fields
    revision = new_revision(page, 1)
    result = {"page": number, "sample_id": "sample", "revision": revision, "validation": {}, "status": "PASS"}
    return page, result


def test_consistent_sequence_needs_no_second_review() -> None:
    first_page, first_result = _page(1, "步骤1：准备", "title", 3)
    second_page, second_result = _page(2, "步骤2：执行", "title", 3)
    assert build_title_consistency_tasks("sample", {1: first_page, 2: second_page}, {1: first_result, 2: second_result}) == []


def test_consistent_child_group_at_parent_level_triggers_hierarchy_review() -> None:
    parent_page, parent_result = _page(1, "性能优化方案", "title", 2)
    first_page, first_result = _page(2, "步骤1：数据库优化", "title", 2)
    second_page, second_result = _page(3, "步骤2：服务优化", "title", 2)
    tasks = build_title_consistency_tasks(
        "sample",
        {1: parent_page, 2: first_page, 3: second_page},
        {1: parent_result, 2: first_result, 3: second_result},
    )
    consistency = [task for task in tasks if task["review_kind"] == "title_type_level_consistency"]
    assert len(consistency) == 1
    assert {item["current_level"] for item in consistency[0]["items"]} == {2}
    assert consistency[0]["items"][0]["semantic_context"]["document_root_candidates"][0]["text"] == "性能优化方案"


def test_single_title_colliding_with_local_parent_path_gets_precision_review() -> None:
    item = {
        "current_type": "title",
        "current_level": 1,
        "text": "叶子栏目",
        "semantic_context": {
            "current_page_reading_context": {
                "before": [{"type": "title", "level": 4, "text": "三级小节"}],
            }
        },
    }
    assert _local_hierarchy_collision(item) is True


def test_mixed_title_sequence_builds_target_centered_review() -> None:
    first_page, first_result = _page(1, "步骤1：准备", "text")
    second_page, second_result = _page(2, "步骤2：执行", "title", 3)
    tasks = build_title_consistency_tasks("sample", {1: first_page, 2: second_page}, {1: first_result, 2: second_result})
    assert len(tasks) == 1
    assert len(tasks[0]["items"]) == 2
    context = tasks[0]["items"][0]["semantic_context"]
    assert len(context["parallel_group"]) == 2
    assert context["current_page_reading_context"]["target"]["text"] == "步骤1：准备"


def test_toc_heading_does_not_replace_document_root_in_review_path() -> None:
    root_page, root_result = _page(1, "建设方案", "title", 1)
    toc_page, toc_result = _page(2, "目录", "title", 1)
    first_page, first_result = _page(3, "步骤1：准备", "text")
    second_page, second_result = _page(4, "步骤2：执行", "title", 3)
    pages = {1: root_page, 2: toc_page, 3: first_page, 4: second_page}
    results = {1: root_result, 2: toc_result, 3: first_result, 4: second_result}
    task = build_title_consistency_tasks("sample", pages, results)[0]
    path = task["items"][0]["semantic_context"]["provisional_heading_path"]
    assert [item["text"] for item in path] == ["建设方案"]


def test_bold_wrapped_steps_enter_same_consistency_group_even_when_first_is_text_only() -> None:
    first_page, first_result = _page(1, "**步骤1：准备环境**", "text")
    second_page, second_result = _page(2, "**步骤2：执行安装**", "title", 3)
    first_page["current_candidate"]["blocks"][0]["type_candidates"] = ["text"]
    tasks = build_title_consistency_tasks("sample", {1: first_page, 2: second_page}, {1: first_result, 2: second_result})
    consistency = [task for task in tasks if task["review_kind"] == "title_type_level_consistency"]
    assert len(consistency) == 1
    assert {item["current_type"] for item in consistency[0]["items"]} == {"text", "title"}


def test_parallel_layout_key_matches_same_parent_geometry_and_prefix() -> None:
    page = make_page()
    base = {
        "text": "\uf06c社区档案：",
        "bbox_image_px": [100, 400, 300, 440],
        "semantic_context": {
            "provisional_heading_path": [{"block_id": "parent", "level": 3}],
            "current_page_reading_context": {"after": [{"type": "text"}]},
        },
    }
    peer = deepcopy(base)
    peer["text"] = "\uf06c物业收费："
    peer["bbox_image_px"] = [101, 800, 301, 840]
    assert _parallel_layout_key(page, base) == _parallel_layout_key(page, peer)


def test_level_four_title_always_gets_precision_review() -> None:
    page, result = _page(1, "最深层栏目", "title", 4)
    tasks = build_title_consistency_tasks("sample", {1: page}, {1: result})
    assert len(tasks) == 1
    assert tasks[0]["review_kind"] == "deep_title_precision"
    assert tasks[0]["items"][0]["allowed_levels"] == [1, 2, 3, 4]


def test_document_review_items_are_globally_bounded_per_round() -> None:
    pages = {}
    results = {}
    for number in range(1, 50):
        pages[number], results[number] = _page(number, f"{number}.1 候选栏目", "title" if number % 2 else "text", 4)
    tasks = build_title_consistency_tasks("sample", pages, results)
    block_ids = [item["block_id"] for task in tasks for item in task["items"]]
    assert len(set(block_ids)) <= MAX_DOCUMENT_REVIEW_ITEMS
    assert len(block_ids) == len(set(block_ids))


def test_same_style_plain_labels_with_mixed_outcomes_get_alignment_task() -> None:
    pages = {}
    results = {}
    layout = {"bbox_image_px": [261, 600, 350, 632]}
    outcomes = [("title", 3), ("text", None), ("title", 4), ("text", None), ("title", 4)]
    for number, (block_type, level) in enumerate(outcomes, start=1):
        page, result = _page(number, "能力标签", block_type, level or 1)
        block = page["current_candidate"]["blocks"][0]
        block.update(deepcopy(layout))
        add_coordinate_views(block, page)
        page["current_candidate"]["blocks"][0]["type_candidates"] = ["text", "title"]
        following = deepcopy(block)
        following["block_id"] = f"candidate.block.p{number}.0001"
        following["type"] = "text"
        following["reading_order"] = 1
        following["bbox_image_px"] = [261, 660, 900, 700]
        following["text"] = "展开说明正文"
        following["source_refs"] = [f"pdf.body.p{number}"]
        add_coordinate_views(following, page)
        page["current_candidate"]["blocks"].append(following)
        page["field_reviews"]["blocks"][following["block_id"]] = {"type": {"status": "PASS", "method": "cross_model_type_agreement"}, "text": {"status": "PASS"}, "bbox": {"status": "PASS"}}
        evidence = deepcopy(page["evidence"]["pdf"][0])
        evidence["evidence_id"] = f"pdf.body.p{number}"
        evidence["text"] = following["text"]
        page["evidence"]["pdf"].append(evidence)
        result["revision"] = new_revision(page, 1)
        pages[number], results[number] = page, result
    tasks = build_title_consistency_tasks("sample", pages, results)
    alignment = [task for task in tasks if task["review_kind"] == "parallel_layout_alignment"]
    assert len(alignment) >= 1
    covered = {item["block_id"] for task in alignment for item in task["items"]}
    assert len(covered) == len(outcomes)
    assert all("flattened to text together" in item["semantic_context"]["context_note"] for task in alignment for item in task["items"])
    assert all(task["items"][0]["semantic_context"]["parallel_group"] for task in alignment)


def test_step_number_restart_splits_independent_groups() -> None:
    pages = {}
    results = {}
    for page_number, text, block_type in ((1, "步骤1：甲", "text"), (2, "步骤2：乙", "title"), (3, "步骤1：丙", "title"), (4, "步骤2：丁", "title")):
        pages[page_number], results[page_number] = _page(page_number, text, block_type, 3)
    tasks = build_title_consistency_tasks("sample", pages, results)
    assert len(tasks) == 1
    assert {item["page"] for item in tasks[0]["items"]} == {1, 2}


def test_source_vote_only_text_candidate_does_not_get_second_recall() -> None:
    page, result = _page(1, "可能遗漏的小节标题", "text")
    assert build_title_consistency_tasks("sample", {1: page}, {1: result}) == []


def test_structurally_recalled_text_candidate_gets_bounded_review() -> None:
    page, result = _page(1, "可能遗漏的小节标题", "text")
    page["current_candidate"]["blocks"][0]["geometric_heading_peer_evidence"] = {
        "peer_block_ids": ["peer.1", "peer.2"],
        "following_block_id": "body.1",
    }
    tasks = build_title_consistency_tasks("sample", {1: page}, {1: result})
    assert len(tasks) == 1
    assert tasks[0]["review_kind"] == "missed_title_recall"
    assert tasks[0]["items"][0]["current_type"] == "text"


def test_close_centered_cover_title_lines_are_marked_ambiguous() -> None:
    page, result = _page(1, "数据安全管", "title", 1)
    first = page["current_candidate"]["blocks"][0]
    first["bbox_image_px"] = [200, 600, 1000, 660]
    add_coordinate_views(first, page)
    second = deepcopy(first)
    second["block_id"] = "candidate.block.p1.0001"
    second["text"] = "理实施细则"
    second["reading_order"] = 1
    second["bbox_image_px"] = [500, 688, 700, 748]
    second["source_refs"] = ["pdf.line.p1.0001"]
    add_coordinate_views(second, page)
    page["current_candidate"]["blocks"].append(second)
    page["field_reviews"]["blocks"][second["block_id"]] = deepcopy(page["field_reviews"]["blocks"][first["block_id"]])
    evidence = deepcopy(page["evidence"]["pdf"][0])
    evidence["evidence_id"] = "pdf.line.p1.0001"
    evidence["text"] = second["text"]
    page["evidence"]["pdf"].append(evidence)
    result["revision"] = new_revision(page, 1)
    updated, marked = mark_ambiguous_multiline_root_groups({1: page}, {1: result})
    assert marked == 2
    assert updated[1]["status"] == "CHECK"
    assert {item["field"] for item in updated[1]["revision"]["review_items"]} == {"structure"}
    assert all(fields["text"]["status"] == "PASS" for fields in updated[1]["revision"]["field_reviews"]["blocks"].values())
    updated_again, marked_again = mark_ambiguous_multiline_root_groups({1: page}, updated)
    assert marked_again == 2
    assert len(updated_again[1]["revision"]["review_items"]) == 2


def test_spaced_centered_cover_titles_remain_independent() -> None:
    page, result = _page(1, "公司名称", "title", 1)
    first = result["revision"]["blocks"][0]
    first["bbox_image_px"] = [350, 450, 850, 510]
    second = deepcopy(first)
    second["block_id"] = "candidate.block.p1.0001"
    second["text"] = "合作协议"
    second["reading_order"] = 1
    second["bbox_image_px"] = [300, 575, 900, 635]
    result["revision"]["blocks"].append(second)
    result["revision"]["field_reviews"]["blocks"][second["block_id"]] = deepcopy(result["revision"]["field_reviews"]["blocks"][first["block_id"]])
    updated, marked = mark_ambiguous_multiline_root_groups({1: page}, {1: result})
    assert marked == 0
    assert updated[1]["status"] == "PASS"


def test_same_decimal_heading_depth_with_mixed_levels_triggers_review() -> None:
    first_page, first_result = _page(1, "2.1.1 背景", "title", 3)
    second_page, second_result = _page(2, "2.1.2 目标", "title", 4)
    tasks = build_title_consistency_tasks("sample", {1: first_page, 2: second_page}, {1: first_result, 2: second_result})
    consistency = [task for task in tasks if task["review_kind"] == "title_type_level_consistency"]
    assert len(consistency) == 1
    assert {item["current_level"] for item in consistency[0]["items"]} == {3, 4}


def test_repeated_heading_label_in_different_context_does_not_force_review() -> None:
    first_page, first_result = _page(1, "问题分析", "title", 2)
    second_page, second_result = _page(5, "问题分析", "title", 3)
    tasks = build_title_consistency_tasks("sample", {1: first_page, 5: second_page}, {1: first_result, 5: second_result})
    assert tasks == []


def test_consistency_decision_updates_pages_and_does_not_repeat() -> None:
    first_page, first_result = _page(1, "步骤1：准备", "text")
    second_page, second_result = _page(2, "步骤2：执行", "title", 3)
    pages = {1: first_page, 2: second_page}
    results = {1: first_result, 2: second_result}
    task = build_title_consistency_tasks("sample", pages, results)[0]
    first_result["revision"]["review_items"].append({
        "field": "type",
        "target": task["items"][0]["block_id"],
        "issue": "request failed: timeout",
        "task_id": task["task_id"],
        "evidence_refs": [],
    })
    response = {
        "task_id": task["task_id"],
        "decisions": [
            {"block_id": item["block_id"], "type": "title", "level": 3, "evidence_refs": [item["evidence"][0]["evidence_id"]]}
            for item in task["items"]
        ],
        "unresolved": [],
    }
    updated = apply_title_consistency_decision(pages, results, task, response)
    assert all(result["revision"]["blocks"][0]["type"] == "title" for result in updated.values())
    assert all(result["validation"]["error_count"] == 0 for result in updated.values())
    assert all(not result["revision"]["review_items"] for result in updated.values())
    assert build_title_consistency_tasks("sample", pages, updated) == []


def _bundle_page(number: int, rows: list[tuple[str, str, int | None, list[float]]]) -> tuple[dict, dict]:
    page, result = _page(number, "x", "text")
    page["current_candidate"]["blocks"] = []
    result["revision"]["blocks"] = []
    result["revision"]["field_reviews"]["blocks"] = {}
    order = 0
    for text, block_type, level, box in rows:
        block_id = f"candidate.block.p{number}.{order:04d}"
        block = {
            "block_id": block_id, "type": block_type, "text": text, "bbox_image_px": box,
            "reading_order": order, "source_refs": [f"pdf.{number}.{order}"], "support_refs": [],
            "type_candidates": [block_type], "issues": [],
        }
        if block_type == "title":
            block["level"] = level
        add_coordinate_views(block, page)
        page["current_candidate"]["blocks"].append(block)
        fields = {"type": {"status": "PASS", "method": "semantic_task"}, "bbox": {"status": "PASS"}, "text": {"status": "PASS"}}
        if block_type == "title":
            fields["level"] = {"status": "PASS", "method": "document_heading_sequence"}
        result["revision"]["field_reviews"]["blocks"][block_id] = fields
        page["field_reviews"]["blocks"][block_id] = fields
        evidence = {"evidence_id": f"pdf.{number}.{order}", "kind": "pdf_line", "text": text, "bbox_image_px": box}
        page["evidence"]["pdf"].append(evidence)
        order += 1
    result["revision"] = new_revision(page, 1)
    return page, result


def _typed_bundle_page(number: int, rows: list[tuple[str, str, int | None, list[float], float, bool]]) -> tuple[dict, dict]:
    page, result = _bundle_page(number, [(text, block_type, level, box) for text, block_type, level, box, _font, _bold in rows])
    lines = {str(item["evidence_id"]): item for item in page["evidence"]["pdf"]}
    for (text, _block_type, _level, _box, font, bold), block in zip(rows, result["revision"]["blocks"]):
        line = lines.get(str(block["source_refs"][0]))
        if line is not None:
            line["font_size"] = font
            line["bold"] = bold
    return page, result


def test_cover_bundle_lines_are_not_hierarchy_collisions() -> None:
    page, result = _typed_bundle_page(
        1,
        [
            ("2024年10月份宝瓶、龙首二级", "title", 1, [290.0, 300.0, 951.0, 370.0], 22.0, False),
            ("水库入库流量预测及运行方式", "title", 1, [322.0, 400.0, 919.0, 467.0], 22.0, False),
        ],
    )
    tasks = build_title_consistency_tasks("sample", {1: page}, {1: result})
    assert not [item for task in tasks for item in task["items"] if item["block_id"].endswith("p1.0001")]


def test_cover_bundle_stays_atomic_when_one_line_is_decided() -> None:
    page, result = _typed_bundle_page(
        1,
        [
            ("金蝶云系统业务流程", "title", 1, [290.0, 300.0, 951.0, 370.0], 22.0, False),
            ("主数据操作指引", "title", 1, [322.0, 400.0, 919.0, 467.0], 22.0, False),
        ],
    )
    members = [
        {"block_id": block["block_id"], "page": 1, "text": block["text"], "type": block["type"], "level": block.get("level")}
        for block in result["revision"]["blocks"]
    ]
    reviewed = result["revision"]["blocks"][0]
    items = [
        {
            "page": 1,
            "block_id": reviewed["block_id"],
            "text": reviewed["text"],
            "reading_order": reviewed["reading_order"],
            "bbox_image_px": reviewed["bbox_image_px"],
            "current_type": reviewed["type"],
            "current_level": reviewed.get("level"),
            "allowed_types": ["text", "title"],
            "allowed_levels": [1, 2, 3, 4],
            "needs": ["type", "level"],
            "evidence": [{"evidence_id": reviewed["source_refs"][0]}],
            "semantic_context": {"bound_group": members},
        }
    ]
    task = {
        "task_version": "ocr-agent-title-consistency-task.v1",
        "task_id": "sample.title_type_level_consistency.1.1",
        "sample_id": "sample",
        "review_kind": "title_type_level_consistency",
        "heading_complete": True,
        "items": items,
    }
    response = {
        "task_id": task["task_id"],
        "decisions": [{"block_id": items[0]["block_id"], "type": "text", "evidence_refs": [items[0]["evidence"][0]["evidence_id"]]}],
        "unresolved": [],
    }
    updated = apply_title_consistency_decision({1: page}, {1: result}, task, response)
    types = [block["type"] for block in updated[1]["revision"]["blocks"]]
    assert types == ["text", "text"]
    sibling = updated[1]["revision"]["field_reviews"]["blocks"][members[1]["block_id"]]
    assert sibling["type"]["method"] == "document_cover_title_bundle_atomicity"


def test_cover_bundle_levels_follow_the_leading_line() -> None:
    page, result = _typed_bundle_page(
        1,
        [
            ("深信服桌面云", "title", 1, [188.0, 493.0, 538.0, 578.0], 28.0, False),
            ("实施方案", "title", 2, [188.0, 639.0, 388.0, 712.0], 24.0, False),
        ],
    )
    members = [
        {"block_id": block["block_id"], "page": 1, "text": block["text"], "type": block["type"], "level": block.get("level")}
        for block in result["revision"]["blocks"]
    ]
    items = []
    for block in result["revision"]["blocks"]:
        items.append(
            {
                "page": 1,
                "block_id": block["block_id"],
                "text": block["text"],
                "reading_order": block["reading_order"],
                "bbox_image_px": block["bbox_image_px"],
                "current_type": block["type"],
                "current_level": block.get("level"),
                "allowed_types": ["text", "title"],
                "allowed_levels": [1, 2, 3, 4],
                "needs": ["type", "level"],
                "evidence": [{"evidence_id": block["source_refs"][0]}],
                "semantic_context": {"bound_group": members},
            }
        )
    task = {
        "task_version": "ocr-agent-cover-title-stack-task.v2",
        "task_id": "sample.cover_title_stack.1",
        "sample_id": "sample",
        "review_kind": "cover_title_stack_alignment",
        "items": items,
    }
    response = {
        "task_id": task["task_id"],
        "decisions": [
            {"block_id": items[0]["block_id"], "type": "title", "level": 1, "evidence_refs": [items[0]["evidence"][0]["evidence_id"]]},
            {"block_id": items[1]["block_id"], "type": "title", "level": 2, "evidence_refs": [items[1]["evidence"][0]["evidence_id"]]},
        ],
        "unresolved": [],
    }
    updated = apply_title_consistency_decision({1: page}, {1: result}, task, response)
    levels = [block.get("level") for block in updated[1]["revision"]["blocks"]]
    assert levels == [1, 1]
    sibling = updated[1]["revision"]["field_reviews"]["blocks"][items[1]["block_id"]]
    assert sibling["level"]["method"] == "document_cover_title_bundle_leading_line"


def test_cover_stack_follows_typography_not_bbox_height() -> None:
    rows = [
        ("浙江炜冈科技股份有限公司", "title", 1, [320, 302, 920, 375], 24.0, True),
        ("金蝶云系统业务流程", "title", 1, [414, 411, 827, 477], 22.0, True),
        ("主数据操作指引", "title", 1, [460, 510, 781, 577], 22.0, True),
        ("晋政发〔2023〕15号", "text", None, [458, 398, 782, 445], 15.5, True),
        ("各市、县人民政府", "text", None, [187, 496, 931, 543], 15.5, False),
    ]
    page, result = _typed_bundle_page(1, rows)
    stack = _cover_title_stack(page, result)
    assert [block["text"] for block in stack] == ["浙江炜冈科技股份有限公司", "金蝶云系统业务流程", "主数据操作指引"]


def test_cover_stack_requires_matching_weight() -> None:
    rows = [
        ("最高人民法院", "title", 1, [446, 162, 796, 194], 10.5, False),
        ("关于适用中华人民共和国民法典", "title", 1, [446, 194, 796, 226], 10.5, True),
        ("合同编通则若干问题的解释", "title", 1, [446, 227, 796, 259], 10.5, True),
        ("法释〔2023〕13号", "text", None, [446, 259, 796, 291], 10.5, False),
        ("为正确审理合同纠纷案件", "text", None, [188, 325, 1052, 422], 10.5, False),
    ]
    page, result = _typed_bundle_page(1, rows)
    stack = _cover_title_stack(page, result)
    assert [block["text"] for block in stack] == ["关于适用中华人民共和国民法典", "合同编通则若干问题的解释"]


def test_cover_stack_falls_back_to_geometry_without_font_metadata() -> None:
    rows = [
        ("公司名", "text", None, [446, 162, 796, 194]),
        ("系统名", "text", None, [446, 195, 796, 227]),
        ("文档名", "title", 2, [489, 227, 752, 259]),
        ("为正确审理合同纠纷案件", "text", None, [188, 325, 1052, 422]),
    ]
    page, result = _bundle_page(1, rows)
    stack = _cover_title_stack(page, result)
    assert [block["text"] for block in stack] == ["公司名", "系统名", "文档名"]


def test_cover_bundle_task_fires_for_mixed_centered_stack() -> None:
    rows = [
        ("最高人民法院", "title", 1, [446, 162, 796, 194]),
        ("关于适用中华人民共和国民法典", "text", None, [446, 195, 796, 227]),
        ("合同编通则若干问题的解释", "text", None, [489, 227, 752, 259]),
        ("为正确审理合同纠纷案件", "text", None, [188, 325, 1052, 422]),
    ]
    page, result = _bundle_page(1, rows)
    tasks = build_cover_title_stack_tasks("sample", {1: page}, {1: result})
    assert len(tasks) == 1
    assert [item["current_type"] for item in tasks[0]["items"]][:3] == ["title", "text", "text"]
    note = tasks[0]["items"][0]["semantic_context"]["context_note"]
    assert "Mixed outcomes inside the bundle are not allowed" in note


def test_cover_bundle_ignores_left_aligned_body_pages() -> None:
    rows = [
        ("一、一般规定", "title", 2, [188, 422, 319, 454]),
        ("第一条 人民法院依据民法典", "text", None, [188, 455, 1052, 552]),
        ("第二条 下列情形", "text", None, [188, 552, 1052, 617]),
    ]
    page, result = _bundle_page(1, rows)
    assert build_cover_title_stack_tasks("sample", {1: page}, {1: result}) == []


def test_cover_bundle_decision_applies_shared_level() -> None:
    rows = [
        ("公司名", "text", None, [446, 162, 796, 194]),
        ("系统名", "text", None, [446, 195, 796, 227]),
        ("文档名", "title", 2, [489, 227, 752, 259]),
    ]
    page, result = _bundle_page(1, rows)
    task = build_cover_title_stack_tasks("sample", {1: page}, {1: result})[0]
    for item in task["items"]:
        item["evidence"] = [{"evidence_id": f"pdf.1.{item['reading_order']}"}]
    response = {
        "task_id": task["task_id"],
        "decisions": [
            {"block_id": item["block_id"], "type": "title", "level": 1, "evidence_refs": [f"pdf.1.{item['reading_order']}"]} for item in task["items"]
        ],
        "unresolved": [],
    }
    updated = apply_title_consistency_decision({1: page}, {1: result}, task, response)
    blocks = updated[1]["revision"]["blocks"]
    assert all(block["type"] == "title" and block["level"] == 1 for block in blocks)


def test_numbering_depth_convention_demotes_isolated_list_headings() -> None:
    pages: dict[int, dict] = {}
    results: dict[int, dict] = {}
    rows = [
        ("4.1 采购管理部门负责组织", "text", None, 14.0, False),
        ("4.2 财务管理部门负责价格", "text", None, 14.0, False),
        ("4.3 纪检审计部门负责监督", "text", None, 14.0, False),
        ("5.5 各单位须建立台账", "text", None, 14.0, False),
        ("5.6 废旧物资称重由专人", "text", None, 14.0, False),
        ("5.9 相关记录由各单位保管", "text", None, 14.0, False),
        ("7.1 申请单由采购部门保管", "text", None, 14.0, False),
        ("5.7 自主拍卖时：", "title", 3, 14.0, False),
        ("5.8 委托拍卖时：", "title", 3, 14.0, False),
    ]
    for index, (text, block_type, level, font, bold) in enumerate(rows, start=1):
        page, result = _typed_bundle_page(index, [(text, block_type, level, [100.0, 100.0, 600.0, 140.0], font, bold)])
        pages[index] = page
        results[index] = result
    output, touched = apply_numbering_depth_convention(pages, results)
    assert touched == 2
    assert all(output[index]["revision"]["blocks"][0]["type"] == "text" for index in (8, 9))
    assert output[8]["revision"]["field_reviews"]["blocks"]["candidate.block.p8.0000"]["type"]["method"] == "document_numbering_depth_convention"


def test_numbering_depth_convention_keeps_visibly_distinct_headings() -> None:
    pages: dict[int, dict] = {}
    results: dict[int, dict] = {}
    rows = [
        ("4.1 采购管理部门负责组织", "text", None, 14.0, False),
        ("4.2 财务管理部门负责价格", "text", None, 14.0, False),
        ("4.3 纪检审计部门负责监督", "text", None, 14.0, False),
        ("5.5 各单位须建立台账", "text", None, 14.0, False),
        ("5.7 自主拍卖时：", "title", 3, 18.0, True),
    ]
    for index, (text, block_type, level, font, bold) in enumerate(rows, start=1):
        page, result = _typed_bundle_page(index, [(text, block_type, level, [100.0, 100.0, 600.0, 140.0], font, bold)])
        pages[index] = page
        results[index] = result
    output, touched = apply_numbering_depth_convention(pages, results)
    assert touched == 0
    assert output[5]["revision"]["blocks"][0]["type"] == "title"


def test_parallel_marker_group_is_made_uniform() -> None:
    """Bulleted peers that share marker, indent and line height share one role."""
    from ocr_agent.title_consistency import apply_parallel_group_uniformity

    def block(block_id: str, text: str, kind: str, level: int | None, top: float) -> dict:
        entry = {"block_id": block_id, "type": kind, "text": text,
                 "bbox_image_px": [232.0, top, 400.0, top + 30.0], "reading_order": int(top)}
        if level is not None:
            entry["level"] = level
        return entry

    blocks = [
        block("b1", "\uf0d8事前：", "title", 4, 325.0),
        block("b2", "定时快照，提前开启定时快照", "text", None, 374.0),
        block("b3", "\uf0d8事件中", "title", 4, 463.0),
        block("b4", "阻断访问，发现勒索攻击", "text", None, 512.0),
        block("b5", "\uf0d8事后", "text", None, 699.0),
        block("b6", "生成已加密文件列表", "text", None, 748.0),
    ]
    page = {"page_image": {"width": 1241, "height": 1754}, "evidence": {}}
    results = {7: {"revision": {"blocks": blocks, "field_reviews": {"blocks": {item["block_id"]: {} for item in blocks}}}}}
    output, adjusted = apply_parallel_group_uniformity({7: page}, results)
    assert adjusted == 1
    members = {item["block_id"]: item for item in output[7]["revision"]["blocks"]}
    assert members["b5"]["type"] == "title" and members["b5"]["level"] == 4
    review = output[7]["revision"]["field_reviews"]["blocks"]["b5"]["type"]
    assert review["method"] == "document_title_parallel_group_uniform"
    assert review["previous_type"] == "text" and review["applied_type"] == "title"
    # the body lines under the peers are a different marker family and stay untouched
    assert members["b2"]["type"] == "text" and members["b4"]["type"] == "text"


def test_parallel_marker_group_keeps_text_when_the_group_agrees_so() -> None:
    from ocr_agent.title_consistency import apply_parallel_group_uniformity

    blocks = [
        {"block_id": "a", "type": "text", "text": "\uf0d8甲", "bbox_image_px": [100.0, 300.0, 200.0, 330.0], "reading_order": 0},
        {"block_id": "b", "type": "text", "text": "\uf0d8乙", "bbox_image_px": [100.0, 360.0, 200.0, 390.0], "reading_order": 1},
    ]
    page = {"page_image": {"width": 1000, "height": 1400}, "evidence": {}}
    results = {1: {"revision": {"blocks": blocks, "field_reviews": {"blocks": {"a": {}, "b": {}}}}}}
    output, adjusted = apply_parallel_group_uniformity({1: page}, results)
    assert adjusted == 0


def test_title_level_never_skips_a_step() -> None:
    """A title may sit at most one level below the heading that governs it."""
    from ocr_agent.title_consistency import apply_title_level_coherence

    def title(block_id: str, level: int, text: str) -> dict:
        return {"block_id": block_id, "type": "title", "level": level, "text": text,
                "bbox_image_px": [100.0, 100.0, 300.0, 140.0], "reading_order": 0, "source_refs": []}

    blocks = [title("a", 1, "文档标题"), title("b", 3, "（一）单位就业登记"), title("c", 4, "（一）子项"), title("d", 2, "二、另一节")]
    page = {"page_image": {"width": 1000, "height": 1400}, "evidence": {}}
    results = {1: {"revision": {"blocks": blocks, "field_reviews": {"blocks": {item["block_id"]: {} for item in blocks}}}}}
    output, adjusted = apply_title_level_coherence({1: page}, results)
    levels = {item["block_id"]: item.get("level") for item in output[1]["revision"]["blocks"]}
    assert levels == {"a": 1, "b": 2, "c": 3, "d": 2}  # clamped to parent + 1, cascade included
    assert adjusted == 2
    review = output[1]["revision"]["field_reviews"]["blocks"]["b"]["level"]
    assert review["method"] == "document_title_level_coherence" and review["previous_level"] == 3


def test_cover_title_below_a_masthead_image_is_still_a_cover() -> None:
    """A logo or banner across the top must not disqualify the title below it as a cover title."""
    from ocr_agent.title_consistency import _cover_title_stack

    def block(block_id: str, text: str, kind: str, level: int | None, top: float) -> dict:
        entry = {"block_id": block_id, "type": kind, "text": text,
                 "bbox_image_px": [208.0, top, 1033.0, top + 95.0], "reading_order": int(top), "source_refs": [f"pdf.line.{block_id}"]}
        if level is not None:
            entry["level"] = level
        return entry

    blocks = [
        {"block_id": "title", "type": "title", "level": 1, "text": "奇安信新一代智慧防火墙",
         "bbox_image_px": [208.0, 624.0, 1033.0, 733.0], "reading_order": 0, "source_refs": ["pdf.line.title"]},
        {"block_id": "model", "type": "text", "text": "NSG5000-TG55",
         "bbox_image_px": [308.0, 774.0, 933.0, 861.0], "reading_order": 1, "source_refs": ["pdf.line.model"]},
        {"block_id": "book", "type": "text", "text": "产品白皮书",
         "bbox_image_px": [420.0, 861.0, 821.0, 970.0], "reading_order": 2, "source_refs": ["pdf.line.book"]},
        {"block_id": "contact", "type": "text", "text": "奇安信集团", "bbox_image_px": [188.0, 1343.0, 297.0, 1375.0], "reading_order": 3, "source_refs": ["pdf.line.contact"]},
    ]
    for index in range(4):  # the page's body size is the most frequent line size
        blocks.append({"block_id": f"body{index}", "type": "text", "text": f"联系方式{index}",
                       "bbox_image_px": [188.0, 1400.0 + index * 34, 500.0, 1434.0 + index * 34],
                       "reading_order": 4 + index, "source_refs": [f"pdf.line.body{index}"]})
    lines = {
        "pdf.line.title": {"evidence_id": "pdf.line.title", "kind": "pdf_line", "bbox_image_px": [208.0, 624.0, 1033.0, 733.0], "text": "奇安信新一代智慧防火墙", "font_size": 36.0, "bold": True},
        "pdf.line.model": {"evidence_id": "pdf.line.model", "kind": "pdf_line", "bbox_image_px": [308.0, 774.0, 933.0, 861.0], "text": "NSG5000-TG55", "font_size": 36.0, "bold": True},
        "pdf.line.book": {"evidence_id": "pdf.line.book", "kind": "pdf_line", "bbox_image_px": [420.0, 861.0, 821.0, 970.0], "text": "产品白皮书", "font_size": 36.0, "bold": True},
        "pdf.line.contact": {"evidence_id": "pdf.line.contact", "kind": "pdf_line", "bbox_image_px": [188.0, 1343.0, 297.0, 1375.0], "text": "奇安信集团", "font_size": 10.5, "bold": False},
    }
    for index in range(4):
        lines[f"pdf.line.body{index}"] = {"evidence_id": f"pdf.line.body{index}", "kind": "pdf_line", "bbox_image_px": [188.0, 1400.0 + index * 34, 500.0, 1434.0 + index * 34],
                                          "text": f"联系方式{index}", "font_size": 10.5, "bold": False}
    page = {"page_image": {"width": 1241, "height": 1754}, "evidence": {"pdf": list(lines.values())}}
    stack = _cover_title_stack(page, {"revision": {"blocks": blocks}})
    assert [item["block_id"] for item in stack] == ["title", "model", "book"]
    # a body-sized line above the seed still disqualifies it: that page is not a cover
    lines["pdf.line.above"] = {"evidence_id": "pdf.line.above", "kind": "pdf_line", "bbox_image_px": [188.0, 500.0, 900.0, 520.0], "text": "正文", "font_size": 10.5, "bold": False}
    page["evidence"]["pdf"] = list(lines.values())
    blocks[0]["source_refs"] = ["pdf.line.title", "pdf.line.above"]
    assert _cover_title_stack(page, {"revision": {"blocks": blocks}}) == []
