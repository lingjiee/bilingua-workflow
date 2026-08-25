"""装配后复检：直接验证最终 Markdown，而不是只检查模型回包。"""

from __future__ import annotations

import re

from .assemble import _RAW_MARKUP, MISSING_MARK, _heading_line, _quote, plain_translation
from .document import Document
from .verify import Finding, VerificationReport

__all__ = ["verify_artifact"]

_CJK = re.compile(r"[\u3400-\u9fff]")
_INLINE_TEXT_LINK = re.compile(r"(?<!!)\[[^\]]*\]\([^)]+\)")
_UNEXPECTED_SCRIPT = re.compile(
    r"[\u0370-\u03ff\u1f00-\u1fff]"  # Greek
    r"|[\u0400-\u052f]"  # Cyrillic
    r"|[\u3040-\u30ff]"  # Hiragana / Katakana
    r"|\ufffd"
)
# 这是高置信度的常用异体拦截表，不尝试把所有汉字都做自动简繁转换。
# 自动转换会误伤人名、书名和地区习惯字；这里只把已知不应进入简体稿的
# 常用繁体字设为硬错误。
_COMMON_TRADITIONAL = re.compile(
    "[萬與專業東絲兩嚴喪個豐臨為麗舉麼義烏樂喬習鄉書買亂爭於虧雲亞產畝親褻億僅從侖倉儀們價眾優會傘偉傳傷倫偽體餘傭傾儲兒兌黨"
    "蘭關興養獸內岡冊寫軍農馮凍淨減凱別劑劍劇勸辦務動勵勞勢區醫華協單賣盧衛卻廠廳歷壓厭廁廂廈廚縣參雙發變敘臺葉號後嚇呂嗎聽啟"
    "吳員嗆嗚詠響喚嘆嘗噴圍國圖圓聖場壞塊堅壇壩墳墜壟壯夾奪奮獎婦媽嬰孫學寧寶實審寬將尋對導屆屬島嶺嶽峽幣幫幹庫廢廣慶應廟開異"
    "棄張彌彎彙強彈當錄徹徑憶懷態慘慣憂戲戶擔據擁擇擊擋擬攏攝攤敵數齊斷無時曆術機殺雜權條來楊極構槍樓標樣樹橋檔檢櫃歡歐殘毀氣"
    "漢湯溝滅濕灣滿濾濫災爐爺牆獨獲環現電畫疊療監盤盡礎禮禱禍離種稱穩窮竄竅競筆筍節範築簡糧糾紀約紅紋納純紗紙級細終組結絕統經"
    "綠維綱網緊緒線練縮績織續罰羅羆職聯聰肅腦臉舊艦艙藝蘇藥虛蟲襲裝複見觀規覺覽觸訂計訊討訓託記講許論設訪證評詞該詳語誤說課調"
    "談請諒謀謂謝識譜讀讓豈貝貞負財貢貧貨販貪貫責貴貸費貼貿賀賓賠賴贊贈贏趙趨躍車軌軟轉輪輸轄辭邊遼達遷過運還進遠違連遲適選遺"
    "郵鄧鄭鄰醜釋裏鑒鐘鋼錢錦錯鍵鎖鏡長門閃閉問閒間聞閣隊陽陰陣階險隨隱雖難霧靜頂項順須預領頓頗頸頻題額顏風飛飯飲館馬駛驗驚鬥"
    "魚鳥鳴鹽麥黃點齡龍]"
)


def _finding(block_id: str, rule: str, detail: str) -> Finding:
    return Finding(block_id=block_id, rule=rule, detail=detail)


def verify_artifact(
    doc: Document,
    chapter: str,
    translations: dict[str, str],
    markdown: str,
) -> VerificationReport:
    """核对落盘前的最终 Markdown：顺序、配对、渲染和字符卫生。"""
    findings: list[Finding] = []
    cursor = 0

    if MISSING_MARK in markdown:
        findings.append(_finding("", "artifact.missing", "成品仍含未译标记。"))

    for block in (b for b in doc.blocks if b.chapter == chapter):
        if block.kind == "toc":
            continue
        zh = (translations.get(block.id) or "").strip()
        if _RAW_MARKUP.match(block.text):
            expected = (block.text,)
        elif not block.translatable:
            expected = (block.text,)
        elif block.kind == "heading":
            expected = (_heading_line(block, zh),)
        else:
            expected = (_quote(block.text), plain_translation(zh) if zh else MISSING_MARK)

        for index, token in enumerate(expected):
            pos = markdown.find(token, cursor)
            if pos < 0:
                findings.append(
                    _finding(
                        block.id,
                        "artifact.pairing",
                        "成品缺少预期内容，或原文与译文的先后顺序被破坏。",
                    )
                )
                break
            if block.translatable and block.kind != "heading" and index == 1:
                line_start = markdown.rfind("\n", 0, pos) + 1
                if markdown[line_start:pos].lstrip().startswith(">"):
                    findings.append(
                        _finding(
                            block.id,
                            "artifact.translation_quote",
                            "正文译文仍位于 Markdown 引用块中。",
                        )
                    )
            cursor = pos + len(token)

        if not block.translatable or not zh:
            continue
        cleaned = plain_translation(zh) if block.kind != "heading" else zh
        if _UNEXPECTED_SCRIPT.search(cleaned):
            findings.append(
                _finding(
                    block.id,
                    "artifact.foreign_script",
                    "译文中混入希腊/西里尔/日文字符或 Unicode 替换符。",
                )
            )
        trad = sorted(set(_COMMON_TRADITIONAL.findall(cleaned)))
        if trad:
            findings.append(
                _finding(
                    block.id,
                    "artifact.traditional",
                    "简体稿中出现常用繁体字：" + "、".join(trad),
                )
            )

    for line_number, line in enumerate(markdown.splitlines(), start=1):
        if not line.startswith("#"):
            continue
        if not re.match(r"^#{1,6} [^#].+ · .+$", line):
            findings.append(
                _finding(
                    "",
                    "artifact.heading",
                    f"第 {line_number} 行标题不符合“中文 · English”结构。",
                )
            )
        if _INLINE_TEXT_LINK.search(line):
            findings.append(
                _finding(
                    "",
                    "artifact.heading_link",
                    f"第 {line_number} 行标题仍含内部链接。",
                )
            )
        if _CJK.search(line) and re.match(r"^#{1,6}\s+#{1,6}\s+", line):
            findings.append(
                _finding(
                    "",
                    "artifact.heading_prefix",
                    f"第 {line_number} 行标题含重复 Markdown 前缀。",
                )
            )

    malformed_image = re.search(r"!(?!\[)(?:Images?|图像)\b", markdown, re.I)
    if malformed_image:
        findings.append(
            _finding(
                "",
                "artifact.malformed_image",
                "成品含被截断的 Markdown 图片字面量（例如 !Images）。",
            )
        )

    return VerificationReport(chapter=chapter, findings=tuple(findings))
