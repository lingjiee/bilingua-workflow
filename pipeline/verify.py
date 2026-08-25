"""装配前校验：把模型的静默错误变成可重跑的明确报告。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from .document import Block, Document
from .glossary import APPROVED, Sense

__all__ = [
    "Severity",
    "Finding",
    "VerificationReport",
    "verify_block",
    "verify_chapter",
    "harvest_candidates",
    "TranslationSplitCandidate",
    "harvest_translation_splits",
]


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    block_id: str
    rule: str
    detail: str
    severity: Severity = Severity.ERROR


@dataclass(frozen=True)
class VerificationReport:
    chapter: str
    findings: tuple[Finding, ...] = ()

    @property
    def error_count(self) -> int:
        return sum(f.severity == Severity.ERROR for f in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(f.severity == Severity.WARNING for f in self.findings)

    @property
    def ok(self) -> bool:
        return self.error_count == 0

    @property
    def blocks_to_retry(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            f.block_id for f in self.findings
            if f.severity == Severity.ERROR and f.block_id
        ))

    def to_markdown(self) -> str:
        status = "通过" if self.ok else "未通过"
        lines = [
            f"# 校验报告 · {self.chapter}",
            "",
            f"状态：**{status}** · 错误 {self.error_count} · 警告 {self.warning_count}",
        ]
        if not self.findings:
            lines.extend(["", "未发现问题。"])
        else:
            lines.extend(["", "| 严重度 | 规则 | 段落 | 详情 |", "|---|---|---|---|"])
            for finding in self.findings:
                detail = finding.detail.replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| {finding.severity.value} | {finding.rule} | "
                    f"`{finding.block_id}` | {detail} |"
                )
        return "\n".join(lines).rstrip() + "\n"


# 数字不能嵌在英文字母或连字符构成的词里：A4、COVID-19 不强制对齐。
# 结尾边界写成正向选择，防止 ``2,500-a-year`` 回溯后误匹配成 ``2``。
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_-])\d+(?:[.,]\d+)*"
    r"(?=$|-(?:a|per)-|[.,](?!\d)|[^A-Za-z0-9_.,-])",
    re.I,
)
_FOOTNOTE = re.compile(r"\[\d+\]")
_LINK_TARGET = re.compile(r"\]\(([^)]+)\)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)?")
_CJK = re.compile(r"[\u3400-\u9fff]")
_UNEXPECTED_SCRIPT = re.compile(
    r"[\u0370-\u03ff\u1f00-\u1fff]|[\u0400-\u052f]|[\u3040-\u30ff]|\ufffd"
)
_TAG = re.compile(r"</?(?:thinking|analysis|reasoning|tool|assistant)\b", re.I)
_JSON_FRAGMENT = re.compile(r"[\{,]\s*[\"'](?:id|zh|translations)[\"']\s*:", re.I)
_ENDNOTE_CITATION = re.compile(
    r"^\s*\[(?:\d+\.|\\?\[\d+\\?\])\]\(#[^)]+\)\s*"
)
_BIBLIOGRAPHY_ENTRY = re.compile(
    r"^\s*(?:"
    r"[A-Z][A-Za-z'’‘-]+,\s*(?:[A-Z]\.|[A-Z][a-z]+)|"
    r"[A-Z][A-Za-z'’‘-]+\s+(?:&|[“\"])"
    r").{3,}$",
    re.DOTALL,
)
_AUTHOR_TITLE_CITATION = re.compile(
    r"^\s*"
    r"[A-Z][A-Za-z'’.-]+(?:\s+(?:[A-Z][A-Za-z'’.-]+|van|von|de|der|da|di)){1,7}"
    r"(?:(?:,\s*(?:and\s+)?|\s+and\s+)"
    r"[A-Z][A-Za-z'’.-]+(?:\s+(?:[A-Z][A-Za-z'’.-]+|van|von|de|der|da|di)){1,7})*"
    r"(?:(?:\s+et\s+al\.,?)|(?:,|\.))\s*(?:[“\"]|\*)",
)
_ORGANIZATION_TITLE_CITATION = re.compile(
    r"^\s*[A-Z][A-Za-z0-9&.-]{1,30}\.\s*(?:[“\"]|\*)"
)
_MARKUP_FRAGMENT = re.compile(
    r"^\s*</?(?:svg|image)\b|^\s*<svg\b",
    re.I,
)


def _reference_entry(text: str) -> bool:
    # Resource lists in JTBDP use a literal bullet followed by an author and
    # preserve publication titles in English.  Classify the entry after
    # removing only that structural prefix.
    candidate = re.sub(r"^\s*(?:[-*+•])\s+", "", text)
    return bool(
        _ENDNOTE_CITATION.match(candidate)
        or _BIBLIOGRAPHY_ENTRY.match(candidate)
        or _AUTHOR_TITLE_CITATION.match(candidate)
        or _ORGANIZATION_TITLE_CITATION.match(candidate)
    )


def _proper_name_paragraph(text: str) -> bool:
    """Long acknowledgements may legitimately preserve mostly Latin names."""
    words = _WORD.findall(text)
    if len(words) < 20 or text.count(",") < 8:
        return False
    capitalised = sum(bool(word and word[0].isupper()) for word in words)
    return capitalised / len(words) >= 0.65


def _finding(block: Block, rule: str, detail: str,
             severity: Severity = Severity.ERROR) -> Finding:
    return Finding(block_id=block.id, rule=rule, detail=detail, severity=severity)


def _numbers(text: str) -> Counter[str]:
    return Counter(m.group(0).replace(",", "") for m in _NUMBER.finditer(text))


def _small_int_to_chinese(value: int) -> str | None:
    """Return the conventional Chinese form for common small integers."""
    digits = "零一二三四五六七八九"
    if 0 <= value < 10:
        return digits[value]
    if 10 <= value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    if 20 <= value < 100:
        return digits[value // 10] + "十" + (digits[value % 10] if value % 10 else "")
    if value == 100:
        return "百"
    return None


def _integer_to_chinese(value: int) -> str | None:
    """Standard Chinese numeral for non-negative integers below 10,000."""
    if not 0 <= value < 10_000:
        return None
    if value == 0:
        return "零"
    digits = "零一二三四五六七八九"
    units = ("", "十", "百", "千")
    parts: list[str] = []
    zero_pending = False
    for power in range(3, -1, -1):
        divisor = 10 ** power
        digit = (value // divisor) % 10
        if digit:
            if zero_pending and parts:
                parts.append("零")
            parts.extend((digits[digit], units[power]))
            zero_pending = False
        elif parts and value % divisor:
            zero_pending = True
    rendered = "".join(parts)
    if 10 <= value < 20 and rendered.startswith("一十"):
        rendered = rendered[1:]
    return rendered


def _heading_ordinal_preserved(block: Block, token: str, translation: str) -> bool:
    """Chapter 2 -> 第二章 is preservation, not a dropped number."""
    if block.kind != "heading" or not token.isdigit():
        return False
    if not re.search(rf"\b(?:chapter|section|part)\s+{re.escape(token)}\b", block.text, re.I):
        return False
    chinese = _small_int_to_chinese(int(token))
    return bool(chinese and f"第{chinese}" in translation)


def _scaled_number_preserved_count(source: str, token: str, translation: str) -> int:
    """Count exact scale conversions such as 1 billion -> 10 亿.

    A source paragraph can contain the same numeric token at multiple scales
    (for example ``1 billion`` and ``1 million``), so a boolean loses
    multiplicity. ``billon`` is accepted as an evidenced source typo.
    """
    matches = list(re.finditer(
        rf"(?<![A-Za-z0-9_.-]){re.escape(token)}\s+(million|billion|billon)\b",
        source,
        re.I,
    ))
    if not matches:
        return 0
    try:
        value = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return 0
    searchable_translation = translation.replace(",", "")
    available: Counter[str] = Counter()
    rendered_options: list[list[str]] = []
    for match in matches:
        conversions = (
            ((value * Decimal(100), "万"), (value / Decimal(100), "亿"))
            if match.group(1).casefold() == "million"
            else ((value * Decimal(10), "亿"),)
        )
        options: list[str] = []
        for converted, unit in conversions:
            rendered = format(converted, "f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            key = f"{rendered}|{unit}"
            options.append(key)
            if key not in available:
                available[key] = len(re.findall(
                    rf"(?<![\d.]){re.escape(rendered)}\s*{unit}",
                    searchable_translation,
                ))
            if converted == converted.to_integral_value():
                chinese = _integer_to_chinese(int(converted))
                if chinese:
                    chinese_key = f"{chinese}|{unit}"
                    options.append(chinese_key)
                    if chinese_key not in available:
                        available[chinese_key] = translation.count(f"{chinese}{unit}")
        rendered_options.append(options)

    preserved = 0
    for options in rendered_options:
        selected = next((option for option in options if available[option] > 0), None)
        if selected is not None:
            available[selected] -= 1
            preserved += 1
    return preserved


def _scaled_number_preserved(source: str, token: str, translation: str) -> bool:
    return _scaled_number_preserved_count(source, token, translation) > 0


def _percentage_preserved(source: str, token: str, translation: str) -> bool:
    if not token.isdigit() or not re.search(
        rf"(?<![A-Za-z0-9_.-]){re.escape(token)}\s+percent\b", source, re.I
    ):
        return False
    chinese = _small_int_to_chinese(int(token))
    return bool(chinese and f"百分之{chinese}" in translation)


def _large_number_preserved(token: str, translation: str) -> bool:
    """Accept exact positional conversions such as 120,000 -> 12 万."""
    try:
        value = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return False
    if abs(value) < 10_000:
        return False
    searchable = translation.replace(",", "")
    for converted, unit in ((value / Decimal(10_000), "万"),
                            (value / Decimal(100_000_000), "亿")):
        rendered = format(converted, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        if re.search(rf"(?<![\d.]){re.escape(rendered)}\s*{unit}", searchable):
            return True
    return False


def _year_and_note_preserved(token: str, translation: str) -> bool:
    """EPUB extraction can collapse ``2013`` + note ``3`` into ``2013.3``."""
    match = re.fullmatch(r"(\d{4})\.(\d{1,2})", token)
    if not match:
        return False
    translated = _numbers(translation)
    return translated[match.group(1)] > 0 and translated[match.group(2)] > 0


def _time_component_preserved(source: str, token: str, translation: str) -> bool:
    for match in re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", source):
        hour, minute = match.groups()
        chinese_hour = _small_int_to_chinese(int(hour))
        hour_forms = {hour, chinese_hour or ""}
        if hour == "2":
            hour_forms.add("两")
        has_hour = any(form and re.search(rf"{re.escape(form)}\s*点", translation)
                       for form in hour_forms)
        if token == hour and has_hour:
            return True
        if token == minute and minute == "00" and has_hour:
            return True
    return False


def _continuous_service_preserved(source: str, token: str, translation: str) -> bool:
    if token not in {"24", "7"} or not re.search(r"(?<!\d)24/7(?!\d)", source):
        return False
    return "全天候" in translation or "二十四小时" in translation


def _proper_name_list(text: str) -> bool:
    items = re.findall(r"^\s*[-*+]\s+(.+?)\s*$", text, re.MULTILINE)
    token = r"[A-Z][A-Za-z0-9]*(?:\.[a-z]+)?"
    return bool(items) and all(re.fullmatch(rf"{token}(?:\s+{token})*", item) for item in items)


def _surface_present(surface: str, text: str) -> bool:
    if _reference_entry(text) or _MARKUP_FRAGMENT.match(text):
        # References preserve titles and publication metadata in English; a
        # title containing a glossary surface is not explanatory prose.
        return False
    core = re.escape(surface.strip()).replace(r"\ ", r"\s+")
    folded = surface.strip().casefold()
    suffix = r"(?:d|s|ing)?" if folded in {"hire", "fire"} else r"(?:s|es)?"
    # A Markdown target is transport metadata, not prose.  JTBDP repeats an
    # image path containing ``jobs-to-be-done`` in every PLAY/STEP heading;
    # allowing glossary matching inside the target creates dozens of false
    # ``job`` hits while the visible heading contains no such word.
    searchable = _LINK_TARGET.sub("]()", text)
    if folded == "fire":
        searchable = re.sub(
            r"\bforest\s+fires?\b|\bon\s+fire\b", "", searchable, flags=re.I
        )
    if folded == "job":
        # Here job names the occupation/role of managers, not Christensen's
        # progress concept.  Removing only this evidenced generic collocation
        # keeps the broad conceptual matcher strict everywhere else.
        searchable = re.sub(
            r"\bjob\s+of\s+(?:general\s+)?managers?\b|"
            r"\b(?:find|get|land)(?:ing|s|ting)?\s+(?:a\s+)?better\s+jobs?\b",
            "",
            searchable,
            flags=re.I,
        )
        # Capitalised Jobs is the surname or a word inside an evidenced
        # English title/query below, never the lowercase JTBD concept here.
        searchable = re.sub(r"\bJobs(?:['’]s)?\b", "", searchable)
        searchable = re.sub(
            r"\bfull-length\s+book\s+on\s+\*Jobs\s+to\s+Be\s+Done\*|"
            r"\bIn\s+\*Jobs\s+to\s+Be\s+Done[,]?\*\s*,?\s+he\s+states|"
            r"\bpresentation\s+[“\"][^”\"]*Jobs\s+to\s+Be\s+Done[^”\"]*[”\"]|"
            r"[“\"]jobs\s+to\s+be\s+done[”\"]\s+(?:or|as\s+a\s+keyword)",
            "",
            searchable,
            flags=re.I,
        )
        searchable = re.sub(
            r"\binterviewing\s+job\s+candidates?\b|"
            r"\b(?:current|previous|old|new|full-time|part-time)\s+jobs?\b|"
            r"\bSteve\s+Jobs(?:['’]s)?\b|"
            r"\bJobs\s+Atlas\b|"
            r"\bThe\s+Jobs\s+To\s+Be\s+Done\s+Playbook\b",
            "",
            searchable,
            flags=re.I,
        )
        searchable = re.sub(
            r"\bhemorrhag(?:e|es|ed|ing)\s+jobs?\b|"
            r"\btools\s+doctors?\s+need\s+to\s+do\s+their\s+jobs?\b|"
            r"\bin\s+the\s+course\s+of\s+(?:his|her|their)\s+jobs?\b|"
            r"\bjob\s+responsibilities\b|"
            r"\bmanagers?\s+(?:is|are|was|were)\s+doing\s+(?:his|her|their)\s+jobs?\b",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "disruptive innovation" and _ENDNOTE_CITATION.match(text):
        # A preserved English bibliography title is not a use of the concept
        # in the explanatory prose of the endnote.
        searchable = re.sub(
            r"\*[^*]*\bdisruptive\s+innovation\b[^*]*\*",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "hire":
        searchable = re.sub(
            r"\bhir(?:e|es|ed|ing)\s+(?:another|a|an|the)\s+"
            r"(?:person|employee|worker|accountant|car\s+service)\b",
            "",
            searchable,
            flags=re.I,
        )
        searchable = re.sub(
            r"\bwhich\s+is\s+hired\s+to\s+help\s+companies\b",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "progress":
        # Astronomical motion in the Aristotle/Ptolemy example is ordinary
        # progress along an orbit, not the defined customer-progress term.
        searchable = re.sub(
            r"\bprogress\s+of\s+the\s+planets?\b|"
            r"\bprogress\s+along\s+Aristotle['’]s\s+circles?\b|"
            r"\btechnological\s+progress\b",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "push":
        # The approved sense is the named force (a noun), not ordinary verbs
        # such as "push up a metric" or "push customers away".
        return bool(re.search(
            r"\b(?:a|an|the|this|that|its|their)\s+push\b|"
            r"\bpush\s+(?:of|and|vs\.?|versus)\b|\bpush\s*[:—-]",
            searchable,
            re.I,
        ))
    if folded == "pull":
        # As with push, the approved sense is the named force. Ordinary verbs
        # and idioms such as "pull the curtain back" must remain natural.
        return bool(re.search(
            r"\b(?:a|an|the|this|that|its|their)\s+pull\b|"
            r"\bpull\s+(?:of|and|vs\.?|versus)\b|\bpull\s*[:—-]",
            searchable,
            re.I,
        ))
    if folded == "struggle":
        # "Entrepreneurs struggle with isolation" is an ordinary verb. The
        # glossary sense names the customer's struggle as a concept.
        searchable = re.sub(
            r"\b(?:struggle|struggles)\s+(?:with|to)\b",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "lemon":
        searchable = re.sub(r"\bLemon\s+V8\b", "", searchable, flags=re.I)
    if folded == "jobs to be done":
        searchable = re.sub(
            r"\bJobs\s+to\s+be\s+Done\s+Handbook\b", "", searchable, flags=re.I
        )
        # These are evidenced publication-title and literal search-query uses.
        # Preserve the English title/query while keeping conceptual prose strict.
        searchable = re.sub(
            r"\b(?:book|work)\s*,?\s*\*Jobs\s+to\s+be\s+Done[,.]?\*|"
            r"\b(?:book|work)\s+(?:called|titled)\s+[“\"]Jobs\s+to\s+be\s+Done[,.]?[”\"]|"
            r"[“\"]jobs\s+to\s+be\s+done[”\"]\s+(?:or|as\s+a\s+keyword)",
            "",
            searchable,
            flags=re.I,
        )
        searchable = re.sub(
            r"\bThe\s+Jobs\s+To\s+Be\s+Done\s+Playbook\b|"
            r"\bfull-length\s+book\s+on\s+\*Jobs\s+to\s+Be\s+Done\*|"
            r"\bIn\s+\*Jobs\s+to\s+Be\s+Done[,]?\*\s*,?\s+he\s+states|"
            r"\bpresentation\s+[“\"][^”\"]*Jobs\s+to\s+Be\s+Done[^”\"]*[”\"]",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "customer":
        searchable = re.sub(
            r"\bCustomer\s+Case\s+Research\b|"
            r"\bPutting\s+Customer\s+Jobs\s+to\s+Work\b|"
            r"\bWho\s+Do\s+You\s+Want\s+Your\s+Customers\s+to\s+Become\b|"
            r"\bThe\s+Customer-Driven\s+Playbook\b",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "customer jobs":
        searchable = re.sub(
            r"\bPutting\s+Customer\s+Jobs\s+to\s+Work\b",
            "",
            searchable,
            flags=re.I,
        )
    if folded == "needs":
        # The frozen sense is the plural noun meaning customer requirements.
        # Third-person verb uses ("the form needs to...", "a developer needs
        # a way...") are ordinary grammar and should not demand 需求.
        return bool(re.search(
            r"\b(?:customer|user|consumer|market|human|functional|social|emotional|"
            r"unmet|underserved|stated|unstated|latent|individual|their|your|our|"
            r"the|these|those)\s+needs\b|"
            r"\bneeds\s+(?:are|were|of|for|and|or|that|across|within|based)\b|"
            r"\b(?:identify|understand|discover|prioritize|meet|satisfy|address|"
            r"serve|capture|rank|fulfill|align(?:ing)?)\s+(?:\w+\s+){0,3}needs\b",
            searchable,
            re.I,
        ))
    if folded == "jtbd play":
        # In "JTBD plays a key role", plays is a verb, not the named method.
        searchable = re.sub(r"\bJTBD\s+plays\s+(?:a|an|the)\b", "", searchable, flags=re.I)
    return bool(core and re.search(rf"\b{core}{suffix}\b", searchable, re.I))


def verify_block(
    block: Block,
    translation: str,
    senses: list[Sense] | tuple[Sense, ...] | None = None,
) -> list[Finding]:
    """校验单个译文块。返回空列表表示通过。"""
    zh = translation or ""
    findings: list[Finding] = []

    if not zh.strip():
        return [_finding(block, "empty", "译文为空或只有空白。")]

    source_numbers = _numbers(block.text)
    translated_numbers = _numbers(zh)
    missing_numbers = source_numbers - translated_numbers
    for token in tuple(missing_numbers):
        scaled = _scaled_number_preserved_count(block.text, token, zh)
        if scaled:
            missing_numbers[token] -= min(missing_numbers[token], scaled)
            if missing_numbers[token] <= 0:
                del missing_numbers[token]
                continue
        if (_heading_ordinal_preserved(block, token, zh)
                or _percentage_preserved(block.text, token, zh)
                or _large_number_preserved(token, zh)
                or _year_and_note_preserved(token, zh)
                or _time_component_preserved(block.text, token, zh)
                or _continuous_service_preserved(block.text, token, zh)):
            missing_numbers[token] -= 1
            if missing_numbers[token] <= 0:
                del missing_numbers[token]
    if missing_numbers:
        missing = ", ".join(
            token if count == 1 else f"{token}×{count}"
            for token, count in sorted(missing_numbers.items())
        )
        findings.append(_finding(block, "numbers", f"译文缺少原文数字：{missing}"))

    source_marks = set(_FOOTNOTE.findall(block.text))
    missing_marks = sorted(mark for mark in source_marks if mark not in zh)
    # EPUB 标题里的内部锚点会由装配器主动剥掉，因为译稿不存在对应锚点；
    # 标题译文不应被要求保留这种注定失效的链接。
    source_targets = (
        set() if block.kind == "heading" else set(_LINK_TARGET.findall(block.text))
    )
    missing_targets = sorted(target for target in source_targets if target not in zh)
    if missing_marks or missing_targets:
        parts = []
        if missing_marks:
            parts.append("脚注 " + ", ".join(missing_marks))
        if missing_targets:
            parts.append("链接目标 " + ", ".join(missing_targets))
        findings.append(_finding(block, "markup", "译文丢失" + "；".join(parts)))

    if block.kind == "list":
        source_items = len(_LIST_ITEM.findall(block.text))
        translated_items = len(_LIST_ITEM.findall(zh))
        if translated_items != source_items:
            findings.append(_finding(
                block,
                "structure",
                f"列表结构不一致：原文 {source_items} 项，译文 {translated_items} 项。",
            ))

    # 同一 surface 可能有多个义项；只接受本次冻结快照中的首选译名。
    # aliases_zh 只用于检索旧译名，不应让同一次构建出现多个译法。
    grouped: dict[str, list[Sense]] = {}
    for sense in senses or ():
        if sense.status == APPROVED and sense.surface:
            grouped.setdefault(sense.surface.casefold(), []).append(sense)
    for grouped_senses in grouped.values():
        surface = grouped_senses[0].surface
        if not _surface_present(surface, block.text):
            continue
        accepted = {sense.zh for sense in grouped_senses if sense.zh}
        # Chinese title quotes may wrap the semantic name while leaving a type
        # suffix outside: “任务即进展”模型. Compare after removing quote marks.
        searchable_zh = re.sub(r"[“”「」『』]", "", zh)
        accepted_forms = set(accepted)
        accepted_forms.update(
            value[:-2] for value in accepted if value.endswith("模型")
        )
        if accepted and not any(value in searchable_zh for value in accepted_forms):
            findings.append(_finding(
                block,
                "glossary",
                f"原文术语 “{surface}” 未使用批准译名：{' / '.join(sorted(accepted))}",
            ))

    source_words = [w.casefold() for w in _WORD.findall(block.text)]
    translated_words = [w.casefold() for w in _WORD.findall(zh)]
    if _UNEXPECTED_SCRIPT.search(zh):
        findings.append(_finding(
            block,
            "foreign_script",
            "译文中混入希腊/西里尔/日文字符或 Unicode 替换字符。",
        ))

    if (
        len(source_words) >= 40
        and not _reference_entry(block.text)
        and not _MARKUP_FRAGMENT.match(block.text)
        and not _proper_name_paragraph(block.text)
        and len(_CJK.findall(zh)) < len(source_words) * 0.35
    ):
        findings.append(_finding(
            block,
            "suspicious_short",
            f"长段译文过短：原文 {len(source_words)} 个英文词，"
            f"译文仅 {len(_CJK.findall(zh))} 个汉字，疑似截断或大段漏译。",
        ))

    # 纯书目型尾注理应保留作者、文章名、期刊名等拉丁文字；把它当作整段
    # 回抄会误报。含解释性正文的尾注仍会因中文比例足够而自然通过。
    if (len(source_words) >= 8
            and not _reference_entry(block.text)
            and not _MARKUP_FRAGMENT.match(block.text)
            and not (block.kind == "list" and _proper_name_list(block.text))):
        source_counts = Counter(source_words)
        overlap = sum((source_counts & Counter(translated_words)).values())
        overlap_ratio = overlap / len(source_words)
        if overlap_ratio >= 0.75 and len(_CJK.findall(zh)) < 4:
            findings.append(_finding(
                block,
                "residual_en",
                f"译文与英文原文重合度过高（{overlap_ratio:.0%}），疑似直接回抄。",
            ))

    if _TAG.search(zh) or _JSON_FRAGMENT.search(zh):
        findings.append(_finding(
            block,
            "tag_leak",
            "译文中残留推理标签或 JSON 包装字段。",
        ))

    return findings


def verify_chapter(
    doc: Document,
    chapter: str,
    translations: dict[str, str],
    senses: list[Sense] | tuple[Sense, ...] | None = None,
) -> VerificationReport:
    findings: list[Finding] = []
    for block in doc.blocks:
        if block.chapter != chapter or not block.translatable:
            continue
        findings.extend(verify_block(block, translations.get(block.id, ""), senses=senses))
    return VerificationReport(chapter=chapter, findings=tuple(findings))


_CAPITALISED_PHRASE = re.compile(
    r"\b(?:The|A|An|[A-Z][a-z]+)"
    r"(?:\s+(?:of|the|and|to|for|in|on|with|[A-Z][a-z]+)){1,6}\b"
)


def _normalise_candidate(value: str) -> str:
    return re.sub(r"^(?:The|A|An)\s+", "", " ".join(value.split())).strip()


def harvest_candidates(
    texts: list[str] | tuple[str, ...],
    known_surfaces: set[str],
    min_count: int = 3,
) -> list[str]:
    """从重复出现的英文大写名词短语中收集人工复核候选。"""
    known = {" ".join(value.casefold().split()) for value in known_surfaces}
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for text in texts:
        for match in _CAPITALISED_PHRASE.finditer(text):
            candidate = _normalise_candidate(match.group(0))
            if not candidate or len(candidate.split()) < 2:
                continue
            key = candidate.casefold()
            if key in known:
                continue
            counts[key] += 1
            display.setdefault(key, candidate)
    return [
        display[key]
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if count >= min_count
    ]


@dataclass(frozen=True)
class TranslationSplitCandidate:
    """A frequent English word whose aligned Chinese appears split in two camps."""

    surface: str
    source_block_count: int
    variants: tuple[tuple[str, int], tuple[str, int]]
    covered_blocks: int


_COMMON_ENGLISH = {
    "about", "after", "again", "against", "also", "among", "another", "because",
    "before", "being", "between", "both", "could", "does", "doing", "during",
    "each", "from", "have", "having", "into", "itself", "just", "more", "most",
    "other", "over", "same", "should", "some", "such", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "under",
    "very", "what", "when", "where", "which", "while", "with", "would", "your",
}
_COMMON_ZH_BIGRAMS = {
    "一个", "一些", "一种", "不过", "不是", "不能", "为了", "他们", "以及",
    "仍然", "但是", "你们", "例如", "公司", "其中", "可以", "可能", "因为",
    "如果", "我们", "所以", "这个", "这些", "这种", "那个", "那些", "通过",
}


def _source_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for raw in _WORD.findall(text):
        word = raw.casefold().replace("’", "'")
        if len(word) < 4 or word in _COMMON_ENGLISH or "'" in word:
            continue
        # Merge ordinary plural blocks into the singular candidate bucket.
        if word.endswith("ies") and len(word) > 5:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 4:
            word = word[:-1]
        terms.add(word)
    return terms


def _zh_bigrams(text: str) -> set[str]:
    found: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        found.update(run[index:index + 2] for index in range(len(run) - 1))
    return found - _COMMON_ZH_BIGRAMS


def harvest_translation_splits(
    aligned: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    known_surfaces: set[str],
    min_source_blocks: int = 12,
    min_variant_blocks: int = 4,
) -> list[TranslationSplitCandidate]:
    """Heuristically flag high-frequency source words with two translation camps.

    This is a candidate harvester, not an automatic glossary editor. It uses
    paragraph alignment, association lift and low co-occurrence to find pairs
    such as ``客户 / 顾客``. Results must pass the existing human glossary gate.
    """
    known = {value.casefold().strip() for value in known_surfaces}
    source_blocks: dict[str, set[int]] = {}
    zh_blocks: dict[str, set[int]] = {}
    for index, (source, translation) in enumerate(aligned):
        for term in _source_terms(source):
            source_blocks.setdefault(term, set()).add(index)
        for bigram in _zh_bigrams(translation):
            zh_blocks.setdefault(bigram, set()).add(index)

    total = max(1, len(aligned))
    results: list[TranslationSplitCandidate] = []
    for surface, blocks in source_blocks.items():
        if surface in known or len(blocks) < min_source_blocks:
            continue
        candidates: list[tuple[str, int, float, set[int]]] = []
        outside_count = max(1, total - len(blocks))
        for token, token_blocks in zh_blocks.items():
            matched = blocks & token_blocks
            count = len(matched)
            if count < min_variant_blocks:
                continue
            inside_rate = count / len(blocks)
            outside_rate = len(token_blocks - blocks) / outside_count
            # One-block additive floor keeps all-corpus terms measurable without
            # collapsing their lift to < 1 when there are no outside blocks.
            lift = inside_rate / max(outside_rate, 1 / total)
            if inside_rate >= 0.08 and lift >= 1.5:
                candidates.append((token, count, lift, matched))
        candidates.sort(key=lambda item: (item[1] * min(item[2], 8.0)), reverse=True)

        best: tuple[int, str, int, str, int, int] | None = None
        for left_index, left in enumerate(candidates[:24]):
            for right in candidates[left_index + 1:24]:
                smaller = min(left[1], right[1])
                if max(left[1], right[1]) > smaller * 4:
                    continue
                overlap = len(left[3] & right[3])
                if overlap > max(1, int(smaller * 0.25)):
                    continue
                covered = len(left[3] | right[3])
                if covered < len(blocks) * 0.45:
                    continue
                # Alternative Chinese renderings often share a semantic head
                # (客户/顾客, 方案/办法); reward that over boundary bigrams such
                # as 户选 which merely straddle adjacent words.
                shared_chars = len(set(left[0]) & set(right[0]))
                shifted_head = any(
                    left[0].index(char) != right[0].index(char)
                    for char in set(left[0]) & set(right[0])
                )
                score = (
                    covered - overlap * 2 + shared_chars * len(blocks)
                    + int(shifted_head) * len(blocks)
                )
                proposal = (score, left[0], left[1], right[0], right[1], covered)
                if best is None or proposal > best:
                    best = proposal
        if best:
            _, left_token, left_count, right_token, right_count, covered = best
            variants = tuple(sorted(
                ((left_token, left_count), (right_token, right_count)),
                key=lambda item: (-item[1], item[0]),
            ))
            results.append(TranslationSplitCandidate(
                surface=surface,
                source_block_count=len(blocks),
                variants=variants,  # type: ignore[arg-type]
                covered_blocks=covered,
            ))
    return sorted(results, key=lambda item: (-item.covered_blocks, item.surface))
