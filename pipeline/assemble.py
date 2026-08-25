"""装配：把不可变原文和模型译文交织成双语对照稿。

这一层扛着整条流水线最重要的保证：**输出里的英文一律来自源文件。**
模型只回传 {id, zh}，原文由脚本按 id 从 Document 里取。所以模型既不能
篡改原文，也不能凭空造出一段正文——回包里出现源文件里没有的 id，
那段内容会被丢弃并记进 stale。

排版用引用块包原文、译文紧随其下。这个选择的理由是具体的：Obsidian
原生渲染、不依赖插件、复制译文时不会带上原文、原文可以整块折叠。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .document import Block, Document
from .visuals import VisualAnnotation, image_target

__all__ = [
    "AssemblyReport",
    "assemble_chapter",
    "assemble_book",
    "plain_translation",
]

MISSING_MARK = "*（未译 · 待重跑）*"

# 目录块指向英文锚点，翻译后全是死链。丢掉，由发布环节按中文标题重建。
DROP_KINDS = frozenset({"toc"})


@dataclass(frozen=True)
class AssemblyReport:
    chapter: str
    written: int = 0
    missing: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing and not self.stale


def _quote(text: str) -> str:
    """整块加引用前缀。多行块（列表、表格）每行都要加，否则渲染会断。"""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.split("\n"))


def plain_translation(text: str) -> str:
    """正文译文只能是平文；引用层级由不可变原文唯一决定。

    模型偶尔会照抄原文的 Markdown 引用前缀。逐行剥除前导 ``>``，包括
    空引用行和嵌套引用，避免中文在成品中被误渲染成英文引用块。
    """
    return "\n".join(re.sub(r"^\s*>+(?:\s?)(.*)$", r"\1", line) for line in text.split("\n"))


# EPUB 抽出的标题常嵌着指向原书内部锚点的链接，如 `# [Title](#nav.xhtml_nch3)`。
# 那些锚点在译稿里不存在，原样带过去就是死链。
# Internal text links in EPUB headings point to anchors that do not survive
# publication.  The negative lookbehind is deliberate: inline images use the
# same ``[alt](target)`` tail and must not be reduced to the broken ``!alt``
# literal that previously polluted JTBDP PLAY/STEP headings.
_INLINE_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\([^)]*\)")
_INLINE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_RAW_MARKUP = re.compile(r"^\s*</?(?:svg|image)\b", re.I)
_CHAPTER_NUMBER = re.compile(r"\bChapter\s+(\d{1,2})\b", re.I)


def _chapter_number_zh(value: int) -> str | None:
    digits = "零一二三四五六七八九"
    if 1 <= value < 10:
        return digits[value]
    if 10 <= value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    if 20 <= value < 100:
        return digits[value // 10] + "十" + (digits[value % 10] if value % 10 else "")
    return None


def _heading_line(block: Block, zh: str) -> str:
    """标题同行承载两种语言：中文在前便于扫读，英文在后便于回查和搜索。"""
    hashes = "#" * max(1, block.level)
    en = _INLINE_LINK.sub(r"\1", block.text.lstrip("#")).strip()
    zh = (zh or "").strip()
    # 提示词要求保留 Markdown，模型有时会把标题译成 `# 中文标题`。
    # 标题级别只能由不可变原文决定，否则装配后会出现 `# # 中文标题`。
    zh = re.sub(r"^#{1,6}\s+", "", zh).strip()
    # 模型也可能照抄 EPUB 标题中的内部链接包装；译稿没有这些锚点。
    zh = _INLINE_LINK.sub(r"\1", zh).strip()
    # The immutable English half keeps an inline decorative image exactly
    # once.  Remove the model-copied duplicate from the Chinese half so the
    # combined bilingual heading does not render two identical arrows.
    zh = _INLINE_IMAGE.sub("", zh)
    zh = " ".join(zh.split())
    # 章节标题统一使用中文数字，避免同一本书混出“第一章 / 第4章 / 第 10 章”。
    chapter_match = _CHAPTER_NUMBER.search(en)
    if chapter_match:
        chapter_zh = _chapter_number_zh(int(chapter_match.group(1)))
        if chapter_zh:
            zh = re.sub(
                r"^第\s*(?:\d{1,2}|[零一二三四五六七八九十]+)\s*章\s*",
                f"第{chapter_zh}章 ",
                zh,
            ).strip()
    if not zh:
        return f"{hashes} {en}"
    return f"{hashes} {zh} · {en}"


def assemble_chapter(
    doc: Document,
    chapter: str,
    translations: dict[str, str],
    image_annotations: Mapping[str, VisualAnnotation] | None = None,
) -> tuple[str, AssemblyReport]:
    """装配一章。返回 (markdown, 报告)。"""
    blocks = [b for b in doc.blocks if b.chapter == chapter]
    known = {b.id for b in doc.translatable_blocks()}
    stale = tuple(sorted(k for k in translations if k not in known))

    parts: list[str] = []
    missing: list[str] = []
    written = 0

    for b in blocks:
        if b.kind in DROP_KINDS:
            continue

        # book2md can expose raw SVG wrapper tags as paragraph blocks.  They
        # are source markup, not prose: preserve one original copy and ignore
        # the historical model echo instead of rendering the tag twice.
        if _RAW_MARKUP.match(b.text):
            parts.append(b.text)
            continue

        if not b.translatable:
            # 图片、代码原样穿过，不加引用前缀
            parts.append(b.text)
            if b.kind == "image" and image_annotations:
                target = image_target(b.text)
                annotation = image_annotations.get(target or "")
                if annotation is not None:
                    parts.append(annotation.to_markdown())
            continue

        zh = (translations.get(b.id) or "").strip()
        if not zh:
            missing.append(b.id)

        if b.kind == "heading":
            parts.append(_heading_line(b, zh))
            if not zh:
                parts.append(MISSING_MARK)
        else:
            parts.append(_quote(b.text))
            parts.append(plain_translation(zh) if zh else MISSING_MARK)

        if zh:
            written += 1

    md = "\n\n".join(parts).rstrip() + "\n" if parts else ""
    return md, AssemblyReport(
        chapter=chapter,
        written=written,
        missing=tuple(missing),
        stale=stale,
    )


def assemble_book(
    doc: Document,
    translations: dict[str, str],
    image_annotations: Mapping[str, VisualAnnotation] | None = None,
) -> list[tuple[str, str, AssemblyReport]]:
    """按章装配整本。返回 [(章节 slug, markdown, 报告)]。"""
    return [
        (
            chapter,
            *assemble_chapter(
                doc,
                chapter,
                translations,
                image_annotations=image_annotations,
            ),
        )
        for chapter in doc.chapter_slugs()
    ]
