"""Language-aware keyword vectors (Task 12, spec §15).

English chunks use PostgreSQL's ``english`` configuration; Malay and Chinese
use ``simple``. Chinese has no word spaces, so it is segmented here —
overlapping character bigrams, zero dependencies — before indexing and
before querying. The language-configuration version feeds the
index-generation compatibility key: bump it whenever this mapping or the
segmenter changes meaningfully, which forces a rebuild instead of silently
mixing keyword vectors.

# ponytail: character-bigram segmenter chosen over jieba-style segmentation
# to avoid a production dependency (spec decision gate). Swap in an approved
# segmenter if Task 13 evaluation shows bigram recall below threshold.
"""

from __future__ import annotations

__all__ = ["KEYWORD_CONFIGS", "LANGUAGE_CONFIG_VERSION", "config_for", "segment", "to_or_query"]

LANGUAGE_CONFIG_VERSION = 1

KEYWORD_CONFIGS = {
    "en": "english",
    "zh-Hans": "simple",
    "zh-Hant": "simple",
    "ms": "simple",
    "mixed": "simple",
}


def config_for(language: str) -> str:
    return KEYWORD_CONFIGS.get(language, "simple")


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


def segment(text: str) -> str:
    """CJK words become space-separated character bigrams; other tokens pass
    through. Word boundaries (whitespace) are respected, so bigrams never
    cross them. Deterministic and idempotent on Latin text.
    """
    pieces: list[str] = []
    for token in text.split():
        if _is_cjk(token[0]):
            if len(token) > 1:
                pieces.extend(token[i:i + 2] for i in range(len(token) - 1))
            else:
                pieces.append(token)
        else:
            pieces.append(token)
    return " ".join(pieces)


def to_or_query(text: str) -> str:
    """Segmented query text as an OR tsquery string.

    AND semantics (``plainto_tsquery``) zero out the keyword channel whenever
    a question carries one word the chunk lacks — English hides this behind
    stopword removal, Chinese bigrams do not get that mercy. OR keeps recall
    and lets ``ts_rank`` ordering pick the best chunk.
    """
    return " | ".join(f"'{token.replace(chr(39), chr(39) * 2)}'" for token in segment(text).split())
