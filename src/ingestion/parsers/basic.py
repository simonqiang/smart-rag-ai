"""Basic parsers: plain text, Markdown, and PDF (Task 11).

Text decoding uses charset-normalizer over the supported encodings
(UTF-8/GB18030/Big5) with a strict-decode fallback cascade. PDF text comes
straight from Docling's parse backend — page-located text cells with reading
order — so extraction stays offline and deterministic.

# ponytail: PDF uses the parse backend without Docling's layout models, so
# blocks are line-stitched cells rather than model-segmented structure; the
# StandardPdfPipeline (and OCR) arrives with the Task 19 adapters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

from charset_normalizer import from_bytes

MAX_PDF_PAGES = 2000  # owner-confirmed limit (spec §15)

_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_MARKUP_ITEM = re.compile(r"^[-*+]\s+")

FAILURE_MESSAGES = {
    "unsupported_type": "unsupported media type for extraction",
    "decode_failed": "text encoding could not be detected (tried UTF-8, GB18030, Big5)",
    "corrupt_pdf": "file is not a readable PDF document",
    "encrypted_pdf": "password-protected PDFs are not supported",
    "too_many_pages": "PDF exceeds the 2,000-page extraction limit",
}


class ExtractionFailed(Exception):
    """The object cannot be extracted; ``reason`` is a stable machine code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(FAILURE_MESSAGES.get(reason, reason))


@dataclass(frozen=True)
class RawBlock:
    text: str
    page: int | None = None


_SUPPORTED_ENCODINGS = frozenset(("ascii", "utf_8", "gb18030", "big5"))


def _decode(data: bytes) -> str:
    # Only the supported encodings count as a detection; charset-normalizer's
    # other guesses (cp1252 & friends) would silently mojibake CJK text.
    best = from_bytes(data).best()
    if best is not None and best.encoding in _SUPPORTED_ENCODINGS:
        return str(best)
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractionFailed("decode_failed")


def parse_text(data: bytes) -> list[RawBlock]:
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = (chunk.strip() for chunk in re.split(r"\n\s*\n", text))
    return [RawBlock(_unwrap(paragraph)) for paragraph in paragraphs if paragraph]


def parse_markdown(data: bytes) -> list[RawBlock]:
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[RawBlock] = []
    for chunk in re.split(r"\n\s*\n", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("#"):
            for line in chunk.split("\n"):
                match = _HEADING.match(line.strip())
                if match and match.group(1).strip():
                    blocks.append(RawBlock(match.group(1).strip()))
        else:
            lines = [_MARKUP_ITEM.sub("", line).strip() for line in chunk.split("\n")]
            body = _unwrap("\n".join(line for line in lines if line))
            if body:  # pragma: no branch - chunk.strip() keeps the last line non-empty
                blocks.append(RawBlock(body))
    return blocks


def parse_pdf(data: bytes) -> list[RawBlock]:
    if not data.startswith(b"%PDF-"):
        raise ExtractionFailed("corrupt_pdf")
    if b"/Encrypt" in data:
        raise ExtractionFailed("encrypted_pdf")

    from docling.backend.docling_parse_backend import (
        ThreadedDoclingParseDocumentBackend,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import DocumentLimits, InputDocument

    try:
        document = InputDocument(
            path_or_stream=BytesIO(data),
            filename="document.pdf",
            format=InputFormat.PDF,
            backend=ThreadedDoclingParseDocumentBackend,
            limits=DocumentLimits(max_num_pages=MAX_PDF_PAGES),
        )
    except Exception as exc:
        raise ExtractionFailed("corrupt_pdf") from exc
    if not document.valid:
        rejection = getattr(document, "_rejection", None)
        if rejection is not None and "max_num_pages" in str(rejection.message):
            raise ExtractionFailed("too_many_pages")
        raise ExtractionFailed("corrupt_pdf")

    cells: list[tuple[int, float, float, float, str]] = []
    # InputDocument opens the backend during __init__ and validates there.
    # iter_pages lives on the concrete threaded backend, not the abstract base.
    for page in cast(Any, document._backend).iter_pages():
        if not page.is_valid():
            raise ExtractionFailed("corrupt_pdf")
        for cell in page.get_text_cells():
            if cell.text.strip():  # pragma: no branch - docling-parse trims cells
                rect = cell.rect
                cells.append((
                    page.page_no, rect.r_y0, rect.r_x0, abs(rect.r_y0 - rect.r_y2),
                    cell.text,
                ))
    return [
        RawBlock(text, page)
        for page, text in _stitch_cells(sorted(cells, key=lambda c: (c[0], c[1])))
    ]


def _stitch_cells(cells: list[tuple[int, float, float, float, str]]) -> list[tuple[int, str]]:
    """Merge consecutive same-column lines (small vertical pitch) per page."""
    blocks: list[tuple[int, str]] = []
    current: tuple[int, str, float, float, float] | None = None
    for cell in cells:
        if current is not None and _continues(current, cell):
            page, text, _top, _x0, _h = current
            current = (page, _join(text, cell[4]), cell[1], cell[2], cell[3])
        else:
            if current is not None:
                blocks.append((current[0], current[1]))
            current = (cell[0], cell[4], cell[1], cell[2], cell[3])
    if current is not None:
        blocks.append((current[0], current[1]))
    return blocks


def _continues(
    current: tuple[int, str, float, float, float],
    cell: tuple[int, float, float, float, str],
) -> bool:
    page, _text, top, x0, height = current
    cell_page, cell_top, cell_x0, _cell_height, _text = cell
    return (
        page == cell_page
        and cell_top - top <= height * 1.5
        and abs(cell_x0 - x0) <= height
    )


def _unwrap(paragraph: str) -> str:
    lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
    return _join(*lines) if lines else ""


def _join(*parts: str) -> str:
    out = ""
    for part in parts:
        if out and not (_is_cjk_text(out[-1]) or _is_cjk_text(part[0])):
            out += " "
        out += part
    return out


def _is_cjk_text(ch: str) -> bool:
    code = ord(ch)
    return 0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF
