"""Image OCR via Tesseract (Task 19d).

An image is one OCR page. Every image starts with the ``chi_sim+eng`` pass:
Latin text (English, Malay) is read by the ``eng`` component, and when Han
characters come back a second ``chi_tra+eng`` pass runs and the higher total
word confidence wins, ties preferring ``chi_sim``.

# ponytail: script choice rides on tesseract confidence margins, which are
# thin on clean renders; Task 27 evaluation may swap the engine or add an
# explicit per-collection language hint.
"""

from __future__ import annotations

from io import BytesIO

from ingestion.parsers.basic import ExtractionFailed, RawBlock, _is_cjk_text, _join

MAX_IMAGE_PIXELS = 64_000_000  # ~64 MP; beyond this, scans are re-exported
MAX_OCR_SIDE = 4_096  # downscale bound: tesseract slows sharply past this
OCR_TIMEOUT_SECONDS = 120

BASE_LANG = "chi_sim+eng"
ALT_LANG = "chi_tra+eng"


def parse_image(data: bytes) -> list[RawBlock]:
    image = _load(data)
    blocks, score = _ocr(image, BASE_LANG)
    if not _has_han(blocks):
        return blocks
    alt_blocks, alt_score = _ocr(image, ALT_LANG)
    return alt_blocks if alt_score > score else blocks


def _load(data: bytes):
    from PIL import Image, ImageOps

    try:
        image = Image.open(BytesIO(data))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ExtractionFailed("image_too_large")
        prepared = ImageOps.exif_transpose(image)
        if prepared.mode != "RGB":  # flatten transparency onto white for OCR
            rgba = prepared.convert("RGBA")
            prepared = Image.new("RGB", rgba.size, "white")
            prepared.paste(rgba, mask=rgba.getchannel("A"))
        prepared.thumbnail((MAX_OCR_SIDE, MAX_OCR_SIDE))
        prepared.load()
    except ExtractionFailed:
        raise
    except Exception as exc:
        raise ExtractionFailed("corrupt_image") from exc
    return prepared


def _has_han(blocks: list[RawBlock]) -> bool:
    return any(_is_cjk_text(ch) for block in blocks for ch in block.text)


def _ocr(image, lang: str) -> tuple[list[RawBlock], float]:
    import pytesseract  # type: ignore[import-untyped]

    try:
        tsv = pytesseract.image_to_data(
            image,
            lang=lang,
            timeout=OCR_TIMEOUT_SECONDS,
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise ExtractionFailed("ocr_unavailable") from exc
    except pytesseract.TesseractError as exc:
        if "Failed loading language" in str(exc):
            raise ExtractionFailed("ocr_language_missing") from exc
        raise ExtractionFailed("ocr_failed") from exc
    except RuntimeError as exc:  # pytesseract timeout
        raise ExtractionFailed("ocr_failed") from exc
    return _paragraphs(tsv)


def _paragraphs(tsv: dict) -> tuple[list[RawBlock], float]:
    """Group TSV words into paragraph blocks; score is the total confidence."""
    blocks: list[RawBlock] = []
    score = 0.0
    words: list[str] = []
    current: tuple | None = None

    def flush() -> None:
        if words:  # words are pre-stripped and non-blank, so the join is non-empty
            blocks.append(RawBlock(_join(*words), 1))

    for key, word, raw_conf in zip(
        zip(tsv["block_num"], tsv["par_num"]), tsv["text"], tsv["conf"]
    ):
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0 or not word.strip():
            continue
        score += conf
        if key != current:
            current = key
            flush()
            words.clear()
        words.append(word.strip())
    flush()
    return blocks, score
