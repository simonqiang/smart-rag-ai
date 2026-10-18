"""HTML extraction (Task 19a), shared with Task 20 website snapshots.

A stdlib ``html.parser`` walk keeps script/style/noscript/template content out
and skips ``nav``/``header``/``footer``/``aside`` boilerplate; visible text
lands in ordered blocks at block-element boundaries. A declared ``meta``
charset wins, then the shared UTF-8/GB18030/Big5 decode cascade applies.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ingestion.parsers.basic import RawBlock, _decode, _unwrap

DROPPED = frozenset(("script", "style", "noscript", "template"))
BOILERPLATE = frozenset(("nav", "header", "footer", "aside"))
BREAKS = frozenset(
    (
        "address",
        "article",
        "blockquote",
        "body",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "hr",
        "html",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "title",
        "tr",
        "ul",
    )
)

_META_CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)")
_DECLARED_CODECS = {
    "utf-8": "utf-8",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "big5": "big5",
    "big5-hkscs": "big5",
}


def parse_html(data: bytes) -> list[RawBlock]:
    extractor = _Extractor()
    extractor.feed(_decode_declared(data))
    extractor.close()
    return extractor.blocks


def _decode_declared(data: bytes) -> str:
    match = _META_CHARSET.search(data[:2048])
    if match:
        codec = _DECLARED_CODECS.get(match.group(1).decode("ascii", "ignore").lower())
        if codec:
            try:
                return data.decode(codec)
            except (
                UnicodeDecodeError,
                LookupError,
            ):  # pragma: no cover - guarded by table
                pass
    return _decode(data)


class _Extractor(HTMLParser):
    """Collect visible text; block tags and boilerplate boundaries flush blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._fragments: list[str] = []
        self.blocks: list[RawBlock] = []
        self._ignored = 0  # nesting depth of open dropped/boilerplate elements

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROPPED or tag in BOILERPLATE:
            if not self._ignored:
                self._flush()
            self._ignored += 1
        elif self._ignored:
            return
        elif tag == "br":  # soft wrap inside a paragraph; lines rejoin CJK-aware
            self._fragments.append("\n")
        elif tag in BREAKS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in DROPPED or tag in BOILERPLATE:
            if self._ignored:
                self._ignored -= 1
        elif not self._ignored and tag in BREAKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self._fragments.append(data)

    def _flush(self) -> None:
        if not self._fragments:
            return
        text = _unwrap("".join(self._fragments))
        self._fragments.clear()
        if text:
            self.blocks.append(RawBlock(text))
