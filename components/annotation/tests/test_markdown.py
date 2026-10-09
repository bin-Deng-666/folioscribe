from ocr_agent.markdown import blocks_to_markdown


def test_markdown_keeps_numbered_text_as_text() -> None:
    blocks = [
        {"type": "title", "level": 1, "text": "目录", "reading_order": 0},
        {"type": "text", "text": "一、背景........3", "reading_order": 1},
        {"type": "text", "text": "正文", "reading_order": 2},
        {"type": "text", "text": "1．第一项", "reading_order": 3},
        {"type": "text", "text": "● 第二项", "reading_order": 4},
    ]
    assert blocks_to_markdown(blocks) == "# 目录\n\n一、背景........3\n\n正文\n\n1．第一项\n\n● 第二项\n"
