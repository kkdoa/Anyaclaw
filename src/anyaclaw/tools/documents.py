"""Document creation and reading tools.

``create_document`` renders Markdown-ish source text into Markdown, plain text,
HTML, Word, PowerPoint, Excel, PDF or CSV and attaches the generated file to the
reply. ``read_document`` extracts text back out of the same family of formats so
the model can inspect what it (or the user) produced.

Third-party libraries (python-docx, python-pptx, openpyxl, reportlab, pypdf) are
imported lazily so the core runtime keeps working without ``anyaclaw[office]``.
"""

from __future__ import annotations

import contextlib
import csv
import html as html_lib
import importlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool
from anyaclaw.tools.paths import (
    ensure_parent,
    human_size,
    portable_relative,
    resolve_workspace_path,
)

DocumentFormat = Literal["md", "txt", "html", "docx", "pptx", "xlsx", "pdf", "csv"]

#: Formats accepted by :class:`CreateDocumentTool`, in schema order.
_FORMATS: tuple[str, ...] = ("md", "txt", "html", "docx", "pptx", "xlsx", "pdf", "csv")

_EXTENSION_FORMATS: dict[str, str] = {
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".text": "txt",
    ".html": "html",
    ".htm": "html",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".pdf": "pdf",
    ".csv": "csv",
}

_TEXT_READ_SUFFIXES = frozenset({
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".csv",
    ".json",
    ".html",
    ".htm",
})

_MAX_READ_CHARS = 100_000
_MAX_SHEET_ROWS = 200
_MAX_PDF_PAGES = 200
_PDF_UNICODE_FONT = "STSong-Light"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_NUMBER_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")
_SHEET_INVALID_RE = re.compile(r"[\[\]:*?/\\]")


@dataclass(slots=True)
class Block:
    """One block-level element of a Markdown-ish document.

    ``kind`` is one of ``heading``, ``paragraph``, ``bullet``, ``number``,
    ``code`` or ``table``. ``level`` holds the heading depth (1-6) and is unused
    elsewhere. ``text`` holds the inline text; ``rows`` holds table cells.
    """

    kind: str
    level: int = 0
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Markdown-ish parsing
# ---------------------------------------------------------------------------


def _split_table_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index]
    separator = lines[index + 1]
    return "|" in header and _TABLE_SEPARATOR_RE.match(separator) is not None


def parse_markdown(content: str) -> list[Block]:
    """Parse ``content`` into block-level :class:`Block` objects.

    The grammar is deliberately small: ATX headings, ``-`` bullets, ``1.``
    numbered items, fenced code blocks, pipe tables and paragraphs. It is shared
    by every renderer so all output formats stay in sync.
    """
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            joined = " ".join(part.strip() for part in paragraph if part.strip())
            if joined:
                blocks.append(Block("paragraph", text=joined))
            paragraph.clear()

    index = 0
    length = len(lines)
    while index < length:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            index += 1
            code_lines: list[str] = []
            while index < length and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index].rstrip())
                index += 1
            index += 1  # consume the closing fence (or run past the end)
            blocks.append(Block("code", text="\n".join(code_lines)))
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            blocks.append(
                Block("heading", level=len(heading.group(1)), text=heading.group(2).strip())
            )
            index += 1
            continue

        if _is_table_start(lines, index):
            flush_paragraph()
            rows = [_split_table_row(lines[index])]
            index += 2
            while index < length and lines[index].strip() and "|" in lines[index]:
                rows.append(_split_table_row(lines[index]))
                index += 1
            blocks.append(Block("table", rows=rows, text="\t".join(rows[0])))
            continue

        bullet = _BULLET_RE.match(stripped)
        if bullet:
            flush_paragraph()
            blocks.append(Block("bullet", text=bullet.group(1).strip()))
            index += 1
            continue

        number = _NUMBER_RE.match(stripped)
        if number:
            flush_paragraph()
            blocks.append(Block("number", text=number.group(1).strip()))
            index += 1
            continue

        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    return blocks


def _table_rows(block: Block) -> list[list[str]]:
    """Return a rectangular copy of ``block.rows`` (short rows padded)."""
    width = max((len(row) for row in block.rows), default=0)
    if width == 0:
        return []
    return [row + [""] * (width - len(row)) for row in block.rows]


def _first_heading(blocks: list[Block]) -> str:
    for block in blocks:
        if block.kind == "heading" and block.text:
            return block.text
    return ""


# ---------------------------------------------------------------------------
# Optional dependency helpers
# ---------------------------------------------------------------------------


def _import_optional(module: str, distribution: str) -> Any:
    """Import an optional module or raise a clear installation hint."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ToolError(
            f"{distribution} is required: pip install anyaclaw[office]"
        ) from exc


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_text(blocks: list[Block]) -> str:
    """Flatten blocks into plain text, dropping Markdown markup."""
    lines: list[str] = []
    number = 0
    for index, block in enumerate(blocks):
        if index and lines and lines[-1] != "":
            lines.append("")
        if block.kind == "heading" or block.kind == "paragraph":
            lines.append(block.text)
            number = 0
        elif block.kind == "bullet":
            lines.append(f"- {block.text}")
            number = 0
        elif block.kind == "number":
            number += 1
            lines.append(f"{number}. {block.text}")
        elif block.kind == "code":
            lines.extend(block.text.split("\n"))
            number = 0
        elif block.kind == "table":
            lines.extend("\t".join(row) for row in block.rows)
            number = 0
    return "\n".join(lines).strip() + "\n" if lines else ""


def _render_html(blocks: list[Block], *, title: str | None) -> str:
    """Render blocks as a small self-contained HTML5 page."""
    escape = html_lib.escape
    parts: list[str] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.kind == "bullet":
            items: list[str] = []
            while index < len(blocks) and blocks[index].kind == "bullet":
                items.append(f"<li>{escape(blocks[index].text)}</li>")
                index += 1
            parts.append("<ul>\n" + "\n".join(items) + "\n</ul>")
            continue
        if block.kind == "number":
            items = []
            while index < len(blocks) and blocks[index].kind == "number":
                items.append(f"<li>{escape(blocks[index].text)}</li>")
                index += 1
            parts.append("<ol>\n" + "\n".join(items) + "\n</ol>")
            continue
        if block.kind == "heading":
            level = min(max(block.level, 1), 6)
            parts.append(f"<h{level}>{escape(block.text)}</h{level}>")
        elif block.kind == "paragraph":
            parts.append(f"<p>{escape(block.text)}</p>")
        elif block.kind == "code":
            parts.append(f"<pre><code>{escape(block.text)}</code></pre>")
        elif block.kind == "table":
            rows = _table_rows(block)
            if rows:
                head = "<tr>" + "".join(f"<th>{escape(cell)}</th>" for cell in rows[0]) + "</tr>"
                body_rows = [
                    "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
                    for row in rows[1:]
                ]
                table = (
                    "<table>\n<thead>\n"
                    + head
                    + "\n</thead>\n<tbody>\n"
                    + "\n".join(body_rows)
                    + "\n</tbody>\n</table>"
                )
                parts.append(table)
        index += 1

    page_title = escape(title or _first_heading(blocks) or "Document")
    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
       margin: 2rem auto; max-width: 52rem; padding: 0 1rem; line-height: 1.6;
       color: #1f2328; }}
h1, h2, h3, h4, h5, h6 {{ line-height: 1.25; margin-top: 1.6em; }}
pre {{ background: #f6f8fa; border-radius: 6px; padding: 1rem; overflow-x: auto; }}
code {{ font-family: Consolas, "SFMono-Regular", monospace; }}
table {{ border-collapse: collapse; margin: 1rem 0; }}
th, td {{ border: 1px solid #d0d7de; padding: 0.4rem 0.75rem; text-align: left; }}
th {{ background: #f6f8fa; }}
</style>
</head>
<body>
<article>
{body}
</article>
</body>
</html>
"""


def _render_csv(blocks: list[Block], content: str) -> str:
    """Render the first/all tables as CSV, or one column of source lines."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    tables = [block for block in blocks if block.kind == "table" and block.rows]
    if tables:
        for position, table in enumerate(tables):
            if position:
                writer.writerow([])
            for row in _table_rows(table):
                writer.writerow(row)
    else:
        for line in (content or "").splitlines():
            writer.writerow([line])
    return buffer.getvalue()


def _render_docx(blocks: list[Block], *, title: str | None, author: str | None) -> bytes:
    """Render blocks as a Word document using python-docx."""
    docx = _import_optional("docx", "python-docx")
    shared = _import_optional("docx.shared", "python-docx")

    document = docx.Document()
    properties = document.core_properties
    if title:
        properties.title = title
    if author:
        properties.author = author
    if title:
        document.add_heading(title, level=0)

    for block in blocks:
        if block.kind == "heading":
            document.add_heading(block.text, level=min(max(block.level, 1), 3))
        elif block.kind == "paragraph":
            document.add_paragraph(block.text)
        elif block.kind in ("bullet", "number"):
            style = "List Bullet" if block.kind == "bullet" else "List Number"
            try:
                document.add_paragraph(block.text, style=style)
            except KeyError:  # template without the built-in list styles
                document.add_paragraph(f"- {block.text}")
        elif block.kind == "code":
            paragraph = document.add_paragraph()
            run = paragraph.add_run()
            run.font.name = "Consolas"
            run.font.size = shared.Pt(9)
            for position, line in enumerate(block.text.split("\n")):
                if position:
                    run.add_break()
                run.add_text(line)
        elif block.kind == "table":
            rows = _table_rows(block)
            if not rows:
                continue
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            with contextlib.suppress(KeyError):  # default template has it
                table.style = "Table Grid"
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    cell = table.cell(row_index, column_index)
                    cell.text = value
                    if row_index == 0:
                        for paragraph in cell.paragraphs:
                            for run in paragraph.runs:
                                run.bold = True

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _set_slide_title(slide: Any, text: str) -> None:
    title_shape = slide.shapes.title
    if title_shape is not None:
        title_shape.text = text


def _content_frame(presentation: Any, slide: Any) -> Any:
    """Return a text frame for body bullets, adding one if the layout lacks it."""
    frame = None
    if len(slide.placeholders) > 1:
        frame = slide.placeholders[1].text_frame
    if frame is None:  # pragma: no cover - depends on the pptx template
        util = _import_optional("pptx.util", "python-pptx")
        box = slide.shapes.add_textbox(
            util.Inches(1), util.Inches(1.5), util.Inches(8), util.Inches(5)
        )
        frame = box.text_frame
    return frame


def _add_bullet(frame: Any, text: str, level: int = 0) -> None:
    paragraphs = frame.paragraphs
    if len(paragraphs) == 1 and not paragraphs[0].text:
        paragraph = paragraphs[0]
    else:
        paragraph = frame.add_paragraph()
    paragraph.text = text
    paragraph.level = level


def _render_pptx(blocks: list[Block], *, title: str | None, author: str | None) -> bytes:
    """Render blocks as a slide deck: ``#`` cover, ``##`` slides, ``###`` sub-bullets."""
    pptx = _import_optional("pptx", "python-pptx")

    presentation = pptx.Presentation()
    properties = presentation.core_properties
    if title:
        properties.title = title
    if author:
        properties.author = author

    cover_index = next(
        (
            position
            for position, block in enumerate(blocks)
            if block.kind == "heading" and block.level == 1 and block.text
        ),
        None,
    )
    cover_title = (
        blocks[cover_index].text if cover_index is not None else title or "Untitled"
    )

    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    _set_slide_title(cover, cover_title)
    subtitle = title if title and title != cover_title else (author or "")
    if subtitle:
        with contextlib.suppress(IndexError, KeyError):  # template dependent
            cover.placeholders[1].text = subtitle

    frame = None
    for position, block in enumerate(blocks):
        if position == cover_index:
            continue
        if block.kind == "heading" and block.level <= 2:
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            _set_slide_title(slide, block.text or (title or cover_title))
            frame = _content_frame(presentation, slide)
            continue
        if block.kind == "heading" and block.level >= 3:
            if frame is None:  # pragma: no cover - heading before any slide
                slide = presentation.slides.add_slide(presentation.slide_layouts[1])
                _set_slide_title(slide, title or cover_title)
                frame = _content_frame(presentation, slide)
            _add_bullet(frame, block.text, level=1)
            continue
        if block.kind == "table":
            if frame is None:
                slide = presentation.slides.add_slide(presentation.slide_layouts[1])
                _set_slide_title(slide, title or cover_title)
                frame = _content_frame(presentation, slide)
            for row in _table_rows(block):
                _add_bullet(frame, " | ".join(row))
            continue
        if block.kind == "code":
            if frame is None:
                slide = presentation.slides.add_slide(presentation.slide_layouts[1])
                _set_slide_title(slide, title or cover_title)
                frame = _content_frame(presentation, slide)
            for line in block.text.split("\n"):
                if line.strip():
                    _add_bullet(frame, line)
            continue
        if block.kind in ("paragraph", "bullet", "number") and block.text:
            if frame is None:  # pragma: no cover - content before any heading
                slide = presentation.slides.add_slide(presentation.slide_layouts[1])
                _set_slide_title(slide, title or cover_title)
                frame = _content_frame(presentation, slide)
            _add_bullet(frame, block.text)

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _sheet_title(raw: str | None) -> str:
    name = _SHEET_INVALID_RE.sub("_", (raw or "Sheet1").strip())[:31]
    return name or "Sheet1"


def _render_xlsx(blocks: list[Block], *, content: str, sheet_name: str | None) -> bytes:
    """Render Markdown tables (or plain lines) into an Excel workbook."""
    openpyxl = _import_optional("openpyxl", "openpyxl")
    styles = _import_optional("openpyxl.styles", "openpyxl")
    utils = _import_optional("openpyxl.utils", "openpyxl")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = _sheet_title(sheet_name)

    tables = [block for block in blocks if block.kind == "table" and block.rows]
    widths: dict[int, int] = {}
    if tables:
        row_index = 1
        for table in tables:
            rows = _table_rows(table)
            for offset, row in enumerate(rows):
                for column, value in enumerate(row, start=1):
                    cell = sheet.cell(row=row_index + offset, column=column, value=value)
                    if offset == 0:
                        cell.font = styles.Font(bold=True)
                    widths[column] = max(widths.get(column, 0), len(str(value)))
            row_index += len(rows) + 1
        sheet.freeze_panes = "A2"
    else:
        for offset, line in enumerate((content or "").splitlines(), start=1):
            sheet.cell(row=offset, column=1, value=line)
            widths[1] = max(widths.get(1, 0), len(line))

    for column, width in widths.items():
        letter = utils.get_column_letter(column)
        sheet.column_dimensions[letter].width = min(max(width + 2, 10), 60)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pdf_latin1(text: str, unicode_ok: bool) -> str:
    """Escape XML and, without a Unicode font, replace non latin-1 characters."""
    value = "" if text is None else str(text)
    if not unicode_ok:
        value = value.encode("latin-1", "replace").decode("latin-1")
    return html_lib.escape(value)


def _pdf_unicode_font() -> tuple[str, bool]:
    """Register the built-in CJK font; fall back to Helvetica on failure."""
    try:
        pdfmetrics = _import_optional("reportlab.pdfbase.pdfmetrics", "reportlab")
        cidfonts = _import_optional("reportlab.pdfbase.cidfonts", "reportlab")
        pdfmetrics.registerFont(cidfonts.UnicodeCIDFont(_PDF_UNICODE_FONT))
        return _PDF_UNICODE_FONT, True
    except Exception:  # noqa: BLE001 - font registration is best effort
        return "Helvetica", False


def _render_pdf(blocks: list[Block], *, title: str | None) -> bytes:
    """Render blocks with reportlab's platypus flowables."""
    pagesizes = _import_optional("reportlab.lib.pagesizes", "reportlab")
    colors = _import_optional("reportlab.lib.colors", "reportlab")
    styles_module = _import_optional("reportlab.lib.styles", "reportlab")
    units = _import_optional("reportlab.lib.units", "reportlab")
    platypus = _import_optional("reportlab.platypus", "reportlab")

    font_name, unicode_ok = _pdf_unicode_font()
    base_styles = styles_module.getSampleStyleSheet()
    body = styles_module.ParagraphStyle(
        "DocumentBody",
        parent=base_styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=15,
        spaceAfter=6,
    )
    title_style = styles_module.ParagraphStyle(
        "DocumentTitle",
        parent=base_styles["Title"],
        fontName=font_name,
        fontSize=22,
        leading=27,
        spaceAfter=14,
    )
    heading_styles = {
        level: styles_module.ParagraphStyle(
            f"DocumentHeading{level}",
            parent=base_styles["Heading1"],
            fontName=font_name,
            fontSize=size,
            leading=size + 4,
            spaceBefore=10,
            spaceAfter=6,
        )
        for level, size in ((1, 16), (2, 14), (3, 12))
    }
    bullet_style = styles_module.ParagraphStyle(
        "DocumentBullet", parent=body, leftIndent=14, bulletIndent=4
    )
    cell_style = styles_module.ParagraphStyle(
        "DocumentCell", parent=body, fontSize=8.5, leading=11, spaceAfter=0
    )
    header_style = styles_module.ParagraphStyle(
        "DocumentHeader",
        parent=cell_style,
        fontName=font_name if unicode_ok else "Helvetica-Bold",
    )
    code_styles = {
        True: styles_module.ParagraphStyle(
            "DocumentCodeMono",
            parent=base_styles["Code"],
            fontName="Courier",
            fontSize=8.5,
            leading=11,
            backColor=colors.HexColor("#f4f4f4"),
            borderPadding=4,
            leftIndent=6,
            spaceAfter=8,
        ),
        False: styles_module.ParagraphStyle(
            "DocumentCodeCjk",
            parent=base_styles["Code"],
            fontName=font_name,
            fontSize=8.5,
            leading=11,
            backColor=colors.HexColor("#f4f4f4"),
            borderPadding=4,
            leftIndent=6,
            spaceAfter=8,
        ),
    }

    story: list[Any] = []
    if title:
        story.append(platypus.Paragraph(_pdf_latin1(title, unicode_ok), title_style))
        story.append(platypus.Spacer(1, 8))

    number = 0
    for block in blocks:
        if block.kind == "heading" and block.text:
            style = heading_styles[min(max(block.level, 1), 3)]
            story.append(platypus.Paragraph(_pdf_latin1(block.text, unicode_ok), style))
            number = 0
        elif block.kind == "paragraph" and block.text:
            story.append(platypus.Paragraph(_pdf_latin1(block.text, unicode_ok), body))
            number = 0
        elif block.kind in ("bullet", "number") and block.text:
            if block.kind == "number":
                number += 1
                marker = f"{number}."
            else:
                number = 0
                marker = "-" if not unicode_ok else "\u2022"
            story.append(
                platypus.Paragraph(
                    _pdf_latin1(block.text, unicode_ok), bullet_style, bulletText=marker
                )
            )
        elif block.kind == "code" and block.text:
            escaped = _pdf_latin1(block.text, unicode_ok).replace("\n", "<br/>")
            all_latin1 = all(ord(char) < 256 for char in block.text)
            story.append(
                platypus.Paragraph(escaped, code_styles[all_latin1])
            )
            number = 0
        elif block.kind == "table":
            rows = _table_rows(block)
            if not rows:
                continue
            data = [
                [
                    platypus.Paragraph(
                        _pdf_latin1(cell, unicode_ok),
                        header_style if row_index == 0 else cell_style,
                    )
                    for cell in row
                ]
                for row_index, row in enumerate(rows)
            ]
            available = pagesizes.A4[0] - 4 * units.cm
            column_width = available / max(len(rows[0]), 1)
            table = platypus.Table(
                data,
                repeatRows=1,
                hAlign="LEFT",
                colWidths=[column_width] * len(rows[0]),
            )
            table.setStyle(
                platypus.TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c9c9c9")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(table)
            story.append(platypus.Spacer(1, 8))
            number = 0

    if not story:  # reportlab needs at least one flowable
        story.append(platypus.Spacer(1, 1))

    buffer = io.BytesIO()
    document = platypus.SimpleDocTemplate(
        buffer,
        pagesize=pagesizes.A4,
        leftMargin=2 * units.cm,
        rightMargin=2 * units.cm,
        topMargin=2 * units.cm,
        bottomMargin=2 * units.cm,
        title=title or "",
    )
    document.build(story)
    return buffer.getvalue()


def _render_document(
    fmt: str,
    blocks: list[Block],
    content: str,
    *,
    title: str | None,
    author: str | None,
    sheet_name: str | None,
) -> bytes:
    """Render ``blocks``/``content`` into bytes for ``fmt``."""
    if fmt == "md":
        return content.encode("utf-8")
    if fmt == "txt":
        return _render_text(blocks).encode("utf-8")
    if fmt == "html":
        return _render_html(blocks, title=title).encode("utf-8")
    if fmt == "csv":
        return _render_csv(blocks, content).encode("utf-8")
    if fmt == "docx":
        return _render_docx(blocks, title=title, author=author)
    if fmt == "pptx":
        return _render_pptx(blocks, title=title, author=author)
    if fmt == "xlsx":
        return _render_xlsx(blocks, content=content, sheet_name=sheet_name)
    if fmt == "pdf":
        return _render_pdf(blocks, title=title)
    raise ToolError(f"unsupported document format: {fmt}")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def _is_plain_name(raw: str) -> bool:
    """True when ``raw`` is a bare file name (no directory component)."""
    text = raw.strip()
    if not text or text in {".", ".."}:
        return False
    if "/" in text or "\\" in text:
        return False
    path = Path(text)
    return not path.is_absolute() and len(path.parts) == 1 and path.parts[0] not in {".", ".."}


# ---------------------------------------------------------------------------
# create_document
# ---------------------------------------------------------------------------


class CreateDocumentTool(Tool):
    """Create a document file from Markdown-ish content."""

    name = "create_document"
    description = (
        "Create a document from Markdown-style content. Supported formats: md, txt, html, "
        "docx, pptx, xlsx, pdf and csv. Headings (#, ##, ###), bullets (-), numbered lists "
        "(1.), fenced code blocks, pipe tables and plain paragraphs are rendered natively. "
        "The generated file is attached to the reply automatically."
    )
    readonly = False
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Target path. A bare file name is resolved inside the generated-files "
                    "directory; other relative paths and absolute paths are resolved inside "
                    "the workspace."
                ),
            },
            "format": {
                "type": "string",
                "enum": list(_FORMATS),
                "description": "Output format; inferred from the path suffix when omitted.",
            },
            "content": {
                "type": "string",
                "description": (
                    "Markdown-ish source text: #/##/### headings, - bullets, 1. numbered "
                    "lists, ``` fenced code blocks, | a | b | tables and plain paragraphs."
                ),
            },
            "title": {
                "type": "string",
                "description": "Document title (docx title, pptx cover, pdf title).",
            },
            "author": {
                "type": "string",
                "description": "Author stored in docx core properties and pptx metadata.",
            },
            "sheet_name": {
                "type": "string",
                "description": "Worksheet name for xlsx output.",
                "default": "Sheet1",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Path | str,
        files_dir: Path | str,
        *,
        max_bytes: int = 20_000_000,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.files_dir = Path(files_dir).expanduser().resolve()
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)

    # -- argument handling -------------------------------------------------
    def _resolve_format(self, raw: str, explicit: str | None) -> str:
        if explicit:
            fmt = explicit.strip().lower().lstrip(".")
            if fmt not in _FORMATS:
                raise ToolError(
                    f"unsupported format: {explicit!r}; expected one of {', '.join(_FORMATS)}"
                )
            return fmt
        fmt = _EXTENSION_FORMATS.get(Path(raw).suffix.lower())
        if fmt is None:
            raise ToolError(
                f"cannot infer document format from {raw!r}; pass format= one of "
                f"{', '.join(_FORMATS)}"
            )
        return fmt

    def _resolve_target(self, raw: str) -> Path:
        if _is_plain_name(raw):
            self.files_dir.mkdir(parents=True, exist_ok=True)
            return resolve_workspace_path(self.files_dir, raw)
        return resolve_workspace_path(self.workspace, raw)

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path")
        content = kwargs.get("content")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("path must be a non-empty string")
        if not isinstance(content, str):
            raise ToolError("content must be a string")

        title = _optional_string(kwargs.get("title"), "title")
        author = _optional_string(kwargs.get("author"), "author")
        sheet_name = _optional_string(kwargs.get("sheet_name"), "sheet_name")
        explicit_format = _optional_string(kwargs.get("format"), "format")

        fmt = self._resolve_format(raw_path, explicit_format)
        target = self._resolve_target(raw_path)
        if target.exists() and target.is_dir():
            raise ToolError(f"path is a directory: {raw_path!r}")

        blocks = parse_markdown(content)
        data = _render_document(
            fmt,
            blocks,
            content,
            title=title,
            author=author,
            sheet_name=sheet_name,
        )
        if len(data) > self.max_bytes:
            raise ToolError(
                f"document is too large: {human_size(len(data))} exceeds the "
                f"{human_size(self.max_bytes)} limit"
            )

        try:
            ensure_parent(target)
            target.write_bytes(data)
        except OSError as exc:
            raise ToolError(f"cannot write document {raw_path!r}: {exc}") from exc

        display_path = portable_relative(target, self.workspace)
        return ToolResult.ok(
            f"created {fmt} document: {display_path} ({human_size(len(data))})",
            files=[str(target)],
            meta={"format": fmt, "bytes": len(data), "path": str(target)},
        )


# ---------------------------------------------------------------------------
# read_document
# ---------------------------------------------------------------------------


def _read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")  # pragma: no cover - latin-1 always fits


def _read_docx(path: Path) -> str:
    docx = _import_optional("docx", "python-docx")
    document = docx.Document(str(path))
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = paragraph.style.name if paragraph.style is not None else ""
        match = re.match(r"Heading (\d+)", style or "")
        if match:
            lines.append(f"{'#' * min(int(match.group(1)), 6)} {text}")
        elif style == "Title":
            lines.append(f"# {text}")
        else:
            lines.append(text)
    for position, table in enumerate(document.tables, start=1):
        lines.append("")
        lines.append(f"## Table {position}")
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def _read_pptx(path: Path) -> str:
    pptx = _import_optional("pptx", "python-pptx")
    presentation = pptx.Presentation(str(path))
    lines: list[str] = []
    for position, slide in enumerate(presentation.slides, start=1):
        lines.append(f"## Slide {position}")
        title_shape = slide.shapes.title
        title_id = getattr(title_shape, "shape_id", None)
        title_text = (title_shape.text or "").strip() if title_shape is not None else ""
        if title_text:
            lines.append(title_text)
        for shape in slide.shapes:
            if getattr(shape, "shape_id", None) == title_id:
                continue
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs).strip()
                if text:
                    lines.append(f"{'  ' * paragraph.level}- {text}")
        lines.append("")
    return "\n".join(lines).strip()


def _read_xlsx(path: Path) -> str:
    openpyxl = _import_optional("openpyxl", "openpyxl")
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for sheet in workbook.worksheets:
            lines.append(f"## Sheet: {sheet.title}")
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if row_number > _MAX_SHEET_ROWS:
                    lines.append(f"…[sheet truncated after {_MAX_SHEET_ROWS} rows]")
                    break
                cells = ["" if value is None else str(value) for value in row]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    lines.append(" | ".join(cells))
            lines.append("")
    finally:
        workbook.close()
    return "\n".join(lines).strip()


def _read_pdf(path: Path) -> str:
    pypdf = _import_optional("pypdf", "pypdf")
    reader = pypdf.PdfReader(str(path))
    pages: list[str] = []
    total = len(reader.pages)
    for position in range(min(total, _MAX_PDF_PAGES)):
        text = (reader.pages[position].extract_text() or "").strip()
        if text:
            pages.append(text)
    if total > _MAX_PDF_PAGES:
        pages.append(f"…[{total - _MAX_PDF_PAGES} pages omitted]")
    return "\n\n".join(pages)


class ReadDocumentTool(Tool):
    """Extract text from a document inside the workspace."""

    name = "read_document"
    description = (
        "Read a document and return its text. Supports Markdown, plain text, CSV, JSON, "
        "HTML, DOCX, PPTX, XLSX and PDF. Long documents are truncated at max_chars."
    )
    readonly = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Document path inside the workspace."},
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_READ_CHARS,
                "description": "Maximum number of characters to return (hard caps at 100000).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path | str, *, max_chars: int = 20_000) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.max_chars = int(max_chars)

    def _resolve_limit(self, value: Any) -> int:
        if value is None:
            return max(1, min(self.max_chars, _MAX_READ_CHARS))
        if isinstance(value, bool):
            raise ToolError("max_chars must be an integer")
        try:
            requested = int(value)
        except (TypeError, ValueError) as exc:
            raise ToolError("max_chars must be an integer") from exc
        if requested <= 0:
            raise ToolError("max_chars must be positive")
        return min(requested, _MAX_READ_CHARS)

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("path must be a non-empty string")
        limit = self._resolve_limit(kwargs.get("max_chars"))

        path = resolve_workspace_path(self.workspace, raw_path, must_exist=True)
        if path.is_dir():
            raise ToolError(f"path is a directory: {raw_path!r}")
        if not path.is_file():
            raise ToolError(f"not a regular file: {raw_path!r}")

        suffix = path.suffix.lower()
        if suffix in _TEXT_READ_SUFFIXES:
            content = _read_text_file(path)
            fmt = _EXTENSION_FORMATS.get(suffix, suffix.lstrip("."))
        elif suffix == ".docx":
            content = _read_docx(path)
            fmt = "docx"
        elif suffix == ".pptx":
            content = _read_pptx(path)
            fmt = "pptx"
        elif suffix == ".xlsx":
            content = _read_xlsx(path)
            fmt = "xlsx"
        elif suffix == ".pdf":
            content = _read_pdf(path)
            fmt = "pdf"
        else:
            raise ToolError(
                f"unsupported document format: {suffix or '(no extension)'}; supported: "
                "md, txt, csv, json, html, docx, pptx, xlsx, pdf"
            )

        truncated = False
        if len(content) > limit:
            omitted = len(content) - limit
            content = content[:limit] + f"\n\n…[truncated {omitted} chars]"
            truncated = True

        display_path = portable_relative(path, self.workspace)
        body = content.strip() if content.strip() else "(empty document)"
        return ToolResult.ok(
            f"# {display_path}\n\n{body}",
            meta={
                "format": fmt,
                "path": str(path),
                "chars": len(content),
                "truncated": truncated,
            },
        )
