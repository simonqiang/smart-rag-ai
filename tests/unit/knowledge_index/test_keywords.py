"""Task 12: keyword-vector language configuration and compatibility keys."""

from ingestion.chunking import ChunkPolicy
from knowledge_index import keywords
from knowledge_index.generations import compatibility_key


def test_language_configuration_is_part_of_the_compatibility_key() -> None:
    policy = ChunkPolicy(version=3)
    assert compatibility_key("bge-m3", 1024, policy) == "bge-m3:1024:chunk-v3:lang-v1"
    assert compatibility_key("bge-m3", 1024, ChunkPolicy(version=4)) != (
        compatibility_key("bge-m3", 1024, policy)
    )


def test_keyword_configs_follow_the_spec_language_map() -> None:
    assert keywords.config_for("en") == "english"
    assert keywords.config_for("zh-Hans") == "simple"
    assert keywords.config_for("zh-Hant") == "simple"
    assert keywords.config_for("ms") == "simple"
    assert keywords.config_for("mixed") == "simple"
    assert keywords.config_for("unknown") == "simple"


def test_segment_splits_cjk_into_bigrams_and_keeps_latin_words() -> None:
    assert keywords.segment("智能检索 面向企业") == "智能 能检 检索 面向 向企 企业"
    assert keywords.segment("grounded evidence") == "grounded evidence"
    assert keywords.segment("risiko pengurusan data") == "risiko pengurusan data"
    # Mixed script: CJK words become bigrams alongside untouched Latin words;
    # a two-character word is its own single bigram.
    assert keywords.segment("using bge-m3 嵌入") == "using bge-m3 嵌入"
    assert keywords.segment("中文检索") == "中文 文检 检索"
    assert keywords.segment("单") == "单"  # a lone character stays itself


def test_or_query_joins_segmented_tokens_for_recall() -> None:
    from knowledge_index.keywords import to_or_query

    assert to_or_query("居家办公 津贴") == "'居家' | '家办' | '办公' | '津贴'"
    assert to_or_query("it's home-office") == "'it''s' | 'home-office'"


def test_or_query_on_a_question_keeps_matching_bigrams() -> None:
    from knowledge_index.keywords import segment, to_or_query

    # Question words (是多少, 什么时候) must not zero out the channel.
    query = to_or_query(segment("居家办公津贴是多少？"))
    assert "'居家'" in query and "| '办公'" in query
