from __future__ import annotations

import zipfile

from ocr_agent.word_reader import match_table_rows, read_word_tables, table_fragment_html


def test_word_reader_preserves_horizontal_and_vertical_merges(tmp_path) -> None:
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>
<w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>标题</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl></w:body></w:document>"""
    path = tmp_path / "source.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
    table = read_word_tables(path)[0]
    assert (table["rows"], table["cols"]) == (2, 3)
    assert table["cells"][0]["rowspan"] == 2
    assert table["cells"][0]["colspan"] == 2
    assert 'rowspan="2"' in table["html"]
    assert 'colspan="2"' in table["html"]
    match = match_table_rows(table, "B")
    assert match == {"row_start": 1, "row_end": 2, "score": 1.0}
    fragment = table_fragment_html(table, 1, 2)
    assert "B" in fragment
    assert 'colspan="2"' in fragment
