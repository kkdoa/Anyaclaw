"""Tests for ``anyaclaw.tools.documents``.

Run with ``PYTHONPATH=src python -m pytest tests/test_documents.py -q``.
Optional-format tests skip cleanly when the ``office`` extra is absent.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest

from anyaclaw.core.errors import ToolError
from anyaclaw.tools.documents import (
    CreateDocumentTool,
    ReadDocumentTool,
    parse_markdown,
)

SAMPLE = """# Quarterly Report

Intro paragraph.

## Highlights

- Growth up 12%
- Costs flat
1. Ship v1
2. Ship v2

```python
print("hi")
```

| Region | Revenue |
| --- | --- |
| EMEA | 120 |
| APAC | 90 |

## Closing

Final words.
"""


def make_creator(tmp_path: Path, **kwargs: object) -> CreateDocumentTool:
    return CreateDocumentTool(tmp_path, tmp_path / "files", **kwargs)


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------


def test_parse_markdown_blocks() -> None:
    blocks = parse_markdown(SAMPLE)
    assert [block.kind for block in blocks] == [
        "heading",
        "paragraph",
        "heading",
        "bullet",
        "bullet",
        "number",
        "number",
        "code",
        "table",
        "heading",
        "paragraph",
    ]
    assert blocks[0].level == 1 and blocks[0].text == "Quarterly Report"
    assert blocks[2].level == 2
    assert blocks[7].text == 'print("hi")'
    table = blocks[8]
    assert table.rows == [["Region", "Revenue"], ["EMEA", "120"], ["APAC", "90"]]


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    outside = tmp_path.parent / f"evil-{tmp_path.name}.md"
    with pytest.raises(ToolError):
        asyncio.run(creator.execute(path="../evil.md", content="nope"))
    assert not outside.exists()


def test_absolute_path_outside_workspace_is_rejected(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    outside = tmp_path.parent / f"outside-{tmp_path.name}.md"
    with pytest.raises(ToolError):
        asyncio.run(creator.execute(path=str(outside), content="nope"))
    assert not outside.exists()


def test_bare_name_lands_in_files_dir(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    result = asyncio.run(creator.execute(path="notes.md", content="# Notes\n\nHello."))
    assert Path(result.files[0]) == (tmp_path / "files" / "notes.md").resolve()
    assert Path(result.files[0]).is_file()


def test_nested_relative_path_stays_in_workspace(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    result = asyncio.run(creator.execute(path="docs/notes.md", content="# Nested"))
    assert Path(result.files[0]) == (tmp_path / "docs" / "notes.md").resolve()
    assert "created md document: docs/notes.md" in result.content


def test_max_bytes_is_enforced(tmp_path: Path) -> None:
    creator = make_creator(tmp_path, max_bytes=32)
    with pytest.raises(ToolError):
        asyncio.run(creator.execute(path="big.md", content="x" * 100))


# ---------------------------------------------------------------------------
# Text formats
# ---------------------------------------------------------------------------


def test_markdown_roundtrip_with_read_tool(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    reader = ReadDocumentTool(tmp_path)
    result = asyncio.run(creator.execute(path="report.md", content=SAMPLE))

    assert result.is_error is False
    assert result.meta["format"] == "md"
    assert result.meta["bytes"] > 0
    assert result.content.startswith("created md document: files/report.md")

    created = Path(result.files[0])
    assert created.read_text(encoding="utf-8") == SAMPLE

    read = asyncio.run(reader.execute(path=str(created)))
    assert read.is_error is False
    assert read.content.splitlines()[0] == "# files/report.md"
    assert "Quarterly Report" in read.content
    assert "Growth up 12%" in read.content
    assert read.meta["format"] == "md"
    assert read.meta["truncated"] is False


def test_txt_strips_markdown_markup(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    result = asyncio.run(creator.execute(path="report.txt", content=SAMPLE))
    text = Path(result.files[0]).read_text(encoding="utf-8")

    assert result.meta["format"] == "txt"
    assert "Quarterly Report" in text
    assert "## Highlights" not in text
    assert "```" not in text
    assert "EMEA\t120" in text


def test_html_renders_basic_markup(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    result = asyncio.run(creator.execute(path="report.html", content=SAMPLE))
    page = Path(result.files[0]).read_text(encoding="utf-8")

    assert page.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in page
    assert "<h1>Quarterly Report</h1>" in page
    assert "<h2>Highlights</h2>" in page
    assert "<ul>" in page and "<li>Growth up 12%</li>" in page
    assert "<ol>" in page
    assert "<pre><code>" in page
    assert "<table>" in page and "<th>Region</th>" in page
    assert "<td>APAC</td>" in page

    read = asyncio.run(ReadDocumentTool(tmp_path).execute(path=str(Path(result.files[0]))))
    assert "<h1>Quarterly Report</h1>" in read.content


def test_csv_writes_table_rows(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    result = asyncio.run(creator.execute(path="report.csv", content=SAMPLE))
    assert result.meta["format"] == "csv"

    with Path(result.files[0]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["Region", "Revenue"]
    assert rows[1] == ["EMEA", "120"]
    assert rows[2] == ["APAC", "90"]


def test_format_override_wins_over_suffix(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    result = asyncio.run(
        creator.execute(path="report.bin", format="md", content="# Explicit")
    )
    assert result.meta["format"] == "md"
    assert Path(result.files[0]).read_text(encoding="utf-8") == "# Explicit"


def test_unknown_format_is_rejected(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    with pytest.raises(ToolError):
        asyncio.run(creator.execute(path="report.xyz", content="data"))


# ---------------------------------------------------------------------------
# Read tool behaviour
# ---------------------------------------------------------------------------


def test_read_truncates_long_content(tmp_path: Path) -> None:
    creator = make_creator(tmp_path)
    reader = ReadDocumentTool(tmp_path, max_chars=1000)
    result = asyncio.run(creator.execute(path="long.md", content="A" * 100))
    read = asyncio.run(reader.execute(path=str(result.files[0]), max_chars=10))

    assert read.content.endswith("…[truncated 90 chars]")
    assert read.meta["truncated"] is True
    assert len(read.content) < 200


def test_read_missing_file_raises(tmp_path: Path) -> None:
    reader = ReadDocumentTool(tmp_path)
    with pytest.raises(ToolError):
        asyncio.run(reader.execute(path="missing.md"))


def test_read_unsupported_extension_raises(tmp_path: Path) -> None:
    (tmp_path / "data.bin").write_bytes(b"\x00\x01")
    reader = ReadDocumentTool(tmp_path)
    with pytest.raises(ToolError):
        asyncio.run(reader.execute(path="data.bin"))


# ---------------------------------------------------------------------------
# Office / PDF formats (skipped without the optional dependencies)
# ---------------------------------------------------------------------------


def test_docx_creation_and_readback(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    creator = make_creator(tmp_path)
    reader = ReadDocumentTool(tmp_path)

    result = asyncio.run(
        creator.execute(
            path="report.docx",
            content=SAMPLE,
            title="Quarterly Report",
            author="Anyaclaw",
        )
    )
    created = Path(result.files[0])
    assert created.is_file() and created.stat().st_size > 0
    assert result.meta["format"] == "docx"

    document = docx.Document(str(created))
    assert document.core_properties.title == "Quarterly Report"
    assert document.core_properties.author == "Anyaclaw"
    assert any(
        paragraph.style.name == "Heading 1" and paragraph.text == "Quarterly Report"
        for paragraph in document.paragraphs
    )
    assert len(document.tables) == 1
    assert document.tables[0].cell(0, 0).text == "Region"
    assert document.tables[0].cell(1, 0).text == "EMEA"
    assert any(
        run.font.name == "Consolas"
        for paragraph in document.paragraphs
        for run in paragraph.runs
    )

    read = asyncio.run(reader.execute(path=str(created)))
    assert "Growth up 12%" in read.content
    assert "Region | Revenue" in read.content


def test_pptx_creation(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    creator = make_creator(tmp_path)
    reader = ReadDocumentTool(tmp_path)

    result = asyncio.run(
        creator.execute(path="report.pptx", content=SAMPLE, author="Anyaclaw")
    )
    created = Path(result.files[0])
    assert created.is_file() and created.stat().st_size > 0

    deck = pptx.Presentation(str(created))
    titles = [
        slide.shapes.title.text
        for slide in deck.slides
        if slide.shapes.title is not None
    ]
    assert titles[0] == "Quarterly Report"
    assert "Highlights" in titles
    assert "Closing" in titles

    read = asyncio.run(reader.execute(path=str(created)))
    assert "Growth up 12%" in read.content


def test_pptx_level_three_heading_becomes_sub_bullet(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    creator = make_creator(tmp_path)
    content = "# Cover\n\n## Topic\n\n### Detail\n\nBody text\n"

    result = asyncio.run(creator.execute(path="deck.pptx", content=content))
    deck = pptx.Presentation(str(Path(result.files[0])))

    levels: dict[str, int] = {}
    for slide in deck.slides:
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                if paragraph.text:
                    levels[paragraph.text] = paragraph.level
    assert levels.get("Detail") == 1
    assert levels.get("Body text") == 0


def test_xlsx_table_written(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    creator = make_creator(tmp_path)
    reader = ReadDocumentTool(tmp_path)

    result = asyncio.run(
        creator.execute(path="report.xlsx", content=SAMPLE, sheet_name="Report")
    )
    created = Path(result.files[0])
    assert created.is_file() and created.stat().st_size > 0

    workbook = openpyxl.load_workbook(str(created))
    assert "Report" in workbook.sheetnames
    sheet = workbook["Report"]
    assert sheet["A1"].value == "Region"
    assert sheet["B1"].value == "Revenue"
    assert sheet["A1"].font.bold is True
    assert sheet.freeze_panes == "A2"
    assert sheet["A2"].value == "EMEA"
    assert sheet["A3"].value == "APAC"
    workbook.close()

    read = asyncio.run(reader.execute(path=str(created)))
    assert "Region | Revenue" in read.content


def test_pdf_creation(tmp_path: Path) -> None:
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    creator = make_creator(tmp_path)

    result = asyncio.run(
        creator.execute(path="report.pdf", content=SAMPLE, title="Quarterly Report")
    )
    created = Path(result.files[0])
    assert created.is_file() and created.stat().st_size > 0
    assert result.meta["format"] == "pdf"

    reader = pypdf.PdfReader(str(created))
    assert len(reader.pages) >= 1


def test_pdf_handles_chinese_content(tmp_path: Path) -> None:
    pytest.importorskip("reportlab")
    creator = make_creator(tmp_path)
    content = "# 季度报告\n\n中文内容测试，包含标点。\n\n| 地区 | 收入 |\n| --- | --- |\n| 亚洲 | 120 |\n"
    result = asyncio.run(creator.execute(path="chinese.pdf", content=content))
    assert Path(result.files[0]).stat().st_size > 0
