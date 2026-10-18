"""Spreadsheet extraction: XLSX and CSV (Task 19c).

Sheets (XLSX) become pages; the first non-blank row of a table is the header
and later rows render as ``Header=Value`` pairs joined by ``|``, which keeps
column meaning in the chunk text for retrieval. Blank values never render,
merged-cell continuations arrive as ``None`` and vanish, and ``read_only``
plus ``data_only`` keeps memory bounded while formula caches supply values
(uncached formulas contribute nothing).

# ponytail: uncached formulas are silently skipped; surfacing a warning needs
# a non-read-only pass (memory risk) — revisit if evaluation misses them.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from io import BytesIO, StringIO

from ingestion.parsers.archives import validate_zip
from ingestion.parsers.basic import ExtractionFailed, ParseResult, RawBlock, _decode

MAX_SHEET_ROWS = 10_000
MAX_CELL_CHARS = 2_000
MAX_FIELD_CHARS = 10_000_000  # csv's own limit; beyond this the file is not CSV


def parse_csv(data: bytes) -> ParseResult:
    warnings: list[str] = []
    try:
        csv.field_size_limit(MAX_FIELD_CHARS)
        text = _decode(data).replace("\r\n", "\n").replace("\x00", "")
        reader = csv.reader(StringIO(text))
        rows = (tuple(row) for row in reader if any(row))
        blocks = [RawBlock(block) for block in _render_table(rows, warnings)]
    except csv.Error as exc:
        raise ExtractionFailed("corrupt_csv") from exc
    return ParseResult(blocks, warnings)


def parse_xlsx(data: bytes) -> ParseResult:
    from openpyxl import load_workbook  # type: ignore[import-untyped]

    validate_zip(data)
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionFailed("corrupt_xlsx") from exc

    blocks: list[RawBlock] = []
    warnings: list[str] = []
    for page, sheet in enumerate(workbook.worksheets, start=1):
        rows = (row for row in sheet.iter_rows(values_only=True) if any(row))
        blocks.extend(RawBlock(text, page) for text in _render_table(rows, warnings))
    return ParseResult(blocks, warnings)


def _render_table(rows: Iterator[tuple], warnings: list[str]) -> Iterator[str]:
    """Render ``Header=Value`` rows; the row cap bounds rendered data rows."""
    header: tuple | None = None
    count = 0
    for row in rows:
        if header is None:
            header = row
            continue
        if count >= MAX_SHEET_ROWS:
            warnings.append("rows_truncated")
            break
        count += 1
        yield _render_row(header, row, warnings)


def _render_row(header: tuple, row: tuple, warnings: list[str]) -> str:
    pairs: list[str] = []
    for index, value in enumerate(row):
        if value is None or str(value).strip() == "":
            continue
        if index < len(header) and str(header[index]).strip():
            name = str(header[index])
        else:  # blank or exhausted header cell: fall back to the column position
            name = f"Column{index + 1}"
        text = str(value)
        if len(text) > MAX_CELL_CHARS:
            text = text[:MAX_CELL_CHARS]
            warnings.append("cells_truncated")
        pairs.append(f"{name}={text}")
    return " | ".join(pairs)
