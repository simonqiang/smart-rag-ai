"""Extraction contract and normalization (Task 11).

``extract`` turns a stored source object into an ``ExtractedDocument``: an
ordered list of ``TextBlock`` s, each carrying a language tag from the
supported strata (``en``, ``zh-Hans``, ``zh-Hant``, ``ms``, ``mixed``) and a
``SourceLocation`` (page for PDFs, block ordinal everywhere) that later
becomes the citation anchor. Failures raise ``ExtractionFailed`` with a
machine reason; nothing here writes state, so a failed extraction can never
activate anything.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ingestion.parsers.basic import (
    ExtractionFailed,
    RawBlock,
    parse_markdown,
    parse_pdf,
    parse_text,
)

__all__ = [
    "LANGUAGES",
    "ExtractedDocument",
    "ExtractionFailed",
    "SourceLocation",
    "SourceObject",
    "TextBlock",
    "detect_language",
    "extract",
]

LANGUAGES = ("en", "zh-Hans", "zh-Hant", "ms", "mixed")


@dataclass(frozen=True)
class SourceObject:
    source_version_id: str
    media_type: str
    filename: str
    data: bytes


@dataclass(frozen=True)
class SourceLocation:
    page: int | None  # 1-based page number for PDFs, None for plain text
    block: int  # 0-based ordinal across the whole document


@dataclass(frozen=True)
class TextBlock:
    text: str
    language: str
    location: SourceLocation


@dataclass(frozen=True)
class ExtractedDocument:
    source_version_id: str
    media_type: str
    language: str
    blocks: list[TextBlock]
    warnings: list[str]


_PARSERS = {
    "text/plain": parse_text,
    "text/markdown": parse_markdown,
    "application/pdf": parse_pdf,
}


def extract(source: SourceObject) -> ExtractedDocument:
    parser = _PARSERS.get(source.media_type)
    if parser is None:
        raise ExtractionFailed("unsupported_type")
    raw_blocks: list[RawBlock] = parser(source.data)
    blocks = [
        TextBlock(raw.text, detect_language(raw.text), SourceLocation(raw.page, ordinal))
        for ordinal, raw in enumerate(raw_blocks)
    ]
    return ExtractedDocument(
        source_version_id=source.source_version_id,
        media_type=source.media_type,
        language=_document_language(blocks),
        blocks=blocks,
        warnings=["empty"] if not blocks else [],
    )


def detect_language(text: str) -> str:
    """Tag one block against the supported strata.

    # ponytail: heuristic tagger — Malay needs function-word hits and Chinese
    # script splits on a common-character table; mis-tags on short/odd blocks
    # are possible. Revisit if evaluation (Task 13) shows stratum misses.
    """
    cjk = sum(1 for ch in text if _is_cjk(ch))
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    total = cjk + latin
    if total == 0:
        return "en"
    if cjk * 20 >= total * 3 and latin * 20 >= total * 3:  # both scripts >= 15%
        return "mixed"
    if cjk >= latin:
        return _chinese_script(text)
    return _latin_language(text)


def _document_language(blocks: list[TextBlock]) -> str:
    weights: Counter[str] = Counter()
    for block in blocks:
        weights[block.language] += max(len(block.text.replace(" ", "")), 1)
    if not weights:
        return "en"
    ranked = weights.most_common()
    if len(ranked) >= 2 and ranked[1][1] * 4 >= weights.total():  # runner-up >= 25%
        return "mixed"
    return ranked[0][0]


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK unified ideographs
        or 0x3400 <= code <= 0x4DBF  # extension A
        or 0xF900 <= code <= 0xFAFF  # compatibility ideographs
        or 0x3000 <= code <= 0x303F  # CJK punctuation (。、《》 …)
    )


# Simplified/traditional pairs as "简繁" chunks; shared characters are omitted.
# # ponytail: ~170 common pairs cover typical prose; swap for OpenCC tables if
# evaluation shows script misses on rare characters.
_SIMP_TRAD = (
    "个個 们們 东東 严嚴 丽麗 举舉 义義 乐樂 习習 乡鄉 买買 乱亂 争爭 云雲 亚亞 产產 亲親 "
    "亿億 从從 众眾 优優 会會 传傳 伤傷 伟偉 伪偽 体體 佣傭 侠俠 侣侶 侦偵 侧側 侨僑 俭儉 "
    "债債 倾傾 偿償 儿兒 兰蘭 关關 兴興 养養 兽獸 内內 冈岡 军軍 农農 冯馮 冲衝 决決 况況 "
    "冻凍 净淨 凉涼 减減 几幾 凤鳳 凭憑 凯凱 击擊 凿鑿 刘劉 则則 刚剛 创創 删刪 别別 刽劊 "
    "剂劑 剐剮 剑劍 剥剝 剧劇 劝勸 办辦 务務 动動 劲勁 劳勞 势勢 勋勳 华華 协協 单單 卖賣 "
    "卫衛 厂廠 厅廳 历歷 厉厲 压壓 厕廁 厘釐 厢廂 厨廚 厩廄 县縣 参參 双雙 发發 变變 叙敘 "
    "叠疊 叶葉 号號 叹嘆 吓嚇 吕呂 吗嗎 员員 团團 园園 围圍 国國 图圖 圆圓 坛壇 坏壞 块塊 "
    "坚堅 声聲 处處 备備 复復 够夠 头頭 夹夾 夺奪 奋奮 妇婦 妈媽 妆妝 娄婁 学學 宝寶 实實 "
    "宠寵 审審 对對 寻尋 导導 寿壽 将將 尔爾 尘塵 尝嘗 层層 岁歲 岛島 岗崗 币幣 帅帥 师師 "
    "帐帳 帘簾 帮幫 归歸 当當 录錄 彻徹 径徑 忆憶 忧憂 怀懷 态態 怜憐 总總 恋戀 恳懇 恶惡 "
    "惊驚 惧懼 愿願 战戰 户戶 扑撲 执執 扩擴 扫掃 扬揚 扰擾 抚撫 护護 报報 担擔 拟擬 拢攏 "
    "拣揀 择擇 挂掛 挚摯 挠撓 挡擋 挣掙 挥揮 损損 换換 捞撈 摊攤 摆擺 摇搖 撑撐 拥擁 无無 "
    "这這 进進 远遠 运運 连連 迟遲 适適 选選 递遞 开開 异異 弃棄 张張 弥彌 弯彎 时時 语語 "
    "书書 画畫 广廣 庆慶 门門 问問 闻聞 间間 闷悶 队隊 阶階 阴陰 阳陽 随隨 隐隱 难難 电電 "
    "页頁 顶頂 项項 顺順 顾顧 顿頓 预預 领領 题題 颜顏 额額 风風 飞飛 饭飯 饮飲 馆館 马馬 "
    "驱驅 驶駛 驾駕 验驗 龙龍 龟龜 丰豐 临臨 为為 乌烏 乔喬 庄莊 应應 么麼 请請 经經 负負 "
    "责責 须須"
)

_SIMPLIFIED = frozenset(pair[0] for pair in _SIMP_TRAD.split())
_TRADITIONAL = frozenset(pair[1] for pair in _SIMP_TRAD.split())


def _chinese_script(text: str) -> str:
    simplified = sum(1 for ch in text if ch in _SIMPLIFIED)
    traditional = sum(1 for ch in text if ch in _TRADITIONAL)
    return "zh-Hant" if traditional > simplified else "zh-Hans"


_MALAY_MARKERS = frozenset((
    "yang", "dan", "ini", "itu", "dengan", "untuk", "adalah", "tidak", "juga",
    "pada", "dalam", "akan", "atau", "telah", "dari", "kami", "kita", "mereka",
    "saya", "boleh", "serta", "jika", "maka", "hingga", "kerana", "secara",
    "antara", "semua", "oleh", "hendaklah",
))

_ENGLISH_MARKERS = frozenset((
    "the", "and", "of", "to", "in", "is", "are", "was", "were", "be", "been",
    "for", "with", "that", "this", "it", "as", "at", "by", "from", "on", "or",
    "an", "have", "has", "had", "not", "but", "they", "we", "you", "he", "she",
    "which", "their", "there", "these", "those", "will", "would", "can",
    "could", "should",
))

_WORD = re.compile(r"[a-z]+")


def _latin_language(text: str) -> str:
    words = set(_WORD.findall(text.lower()))
    malay = len(words & _MALAY_MARKERS)
    english = len(words & _ENGLISH_MARKERS)
    return "ms" if malay > english else "en"
