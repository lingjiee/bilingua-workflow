"""装配：把不可变原文和模型译文交织成双语对照稿。

这一层扛着整条流水线最重要的保证：**输出里的英文一律来自源文件，
不来自模型回包。** 模型只回传 {id, zh}，原文由脚本按 id 从 Document
里取。所以模型不可能篡改原文，也不可能凭空造出一段原文。
"""

from __future__ import annotations

import pytest

from pipeline.assemble import assemble_book, assemble_chapter
from pipeline.document import parse_markdown
from pipeline.visuals import VisualAnnotation

SRC = """# Chapter One

First paragraph here.

Second paragraph here.

![a figure](images/f1.png)

- item one
- item two

```
code stays put
```

# Chapter Two

Only paragraph of two.
"""


@pytest.fixture
def doc():
    return parse_markdown(SRC, book_slug="t")


@pytest.fixture
def full(doc):
    """给每个可译块一份译文。"""
    return {b.id: f"【译】{b.text.strip()[:12]}" for b in doc.translatable_blocks()}


# ------------------------------------------------------------ 原文来源

class TestProvenance:
    def test_every_source_text_appears_verbatim(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        for b in doc.translatable_blocks():
            if b.chapter != "chapter-one" or b.kind == "heading":
                continue
            for line in b.text.split("\n"):
                assert line.strip() in md, f"原文丢失: {line[:40]!r}"

    def test_model_cannot_inject_an_english_paragraph(self, doc):
        """回包里塞一个不存在的 id，装配时必须忽略并报告，
        绝不能凭空生出一段正文。"""
        t = {b.id: "译" for b in doc.translatable_blocks()}
        t["t/chapter-one/§deadbeef"] = "模型凭空造的段落"
        md, rep = assemble_chapter(doc, "chapter-one", t)
        assert "模型凭空造的段落" not in md
        assert "t/chapter-one/§deadbeef" in rep.stale

    def test_translation_is_placed_under_its_own_original(self, doc):
        t = {b.id: f"ZH-{i}" for i, b in enumerate(doc.translatable_blocks())}
        md, _ = assemble_chapter(doc, "chapter-one", t)
        blocks = [b for b in doc.translatable_blocks()
                  if b.chapter == "chapter-one" and b.kind == "para"]
        for b in blocks:
            zh = t[b.id]
            first_line = b.text.split("\n")[0].strip()
            assert md.index(first_line) < md.index(zh), "译文必须跟在自己的原文之后"


# ------------------------------------------------------------ 排版

class TestLayout:
    def test_original_is_blockquoted(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        assert "> First paragraph here." in md

    def test_translation_is_plain_text(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        line = next(l for l in md.split("\n") if l.startswith("【译】First"))
        assert not line.startswith(">")

    def test_model_quote_prefix_is_removed_from_body_translation(self):
        src = "# C\n\n> > First thought.\n> >\n> > Second thought.\n"
        quoted = parse_markdown(src, book_slug="t")
        translations = {
            b.id: ("标题" if b.kind == "heading" else "> 我先这样想。\n>\n> 后来改变了想法。")
            for b in quoted.translatable_blocks()
        }
        md, report = assemble_chapter(quoted, quoted.chapter_slugs()[0], translations)
        assert report.ok
        assert "> > First thought." in md
        assert "\n我先这样想。\n\n后来改变了想法。\n" in md
        assert "\n> 我先这样想。" not in md

    def test_multiline_block_is_fully_quoted(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        assert "> - item one" in md and "> - item two" in md

    def test_heading_keeps_its_level(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        assert any(l.startswith("# ") for l in md.split("\n"))

    def test_heading_carries_both_languages(self, doc):
        t = {b.id: "第一章" for b in doc.translatable_blocks() if b.kind == "heading"}
        t.update({b.id: "译" for b in doc.translatable_blocks() if b.kind != "heading"})
        md, _ = assemble_chapter(doc, "chapter-one", t)
        head = next(l for l in md.split("\n") if l.startswith("# "))
        assert "第一章" in head and "Chapter One" in head

    def test_images_pass_through_unchanged(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        assert "![a figure](images/f1.png)" in md

    def test_curated_visual_labels_follow_the_unchanged_image(self, doc, full):
        annotation = VisualAnnotation(
            path="images/f1.png",
            figure="图 1",
            summary_zh="示意图。",
            labels=(("Job Performer", "任务执行者"),),
        )
        md, _ = assemble_chapter(
            doc,
            "chapter-one",
            full,
            image_annotations={annotation.path: annotation},
        )
        image = "![a figure](images/f1.png)"
        assert md.count(image) == 1
        assert md.index(image) < md.index("图中文字中英对照")
        assert "| Job Performer | 任务执行者 |" in md
        assert "原始图片保持不变" in md

    def test_unmatched_visual_annotation_has_no_effect(self, doc, full):
        annotation = VisualAnnotation(
            path="images/other.png",
            figure="图 X",
            summary_zh="不应出现。",
            labels=(("Other", "其他"),),
        )
        plain, _ = assemble_chapter(doc, "chapter-one", full)
        annotated, _ = assemble_chapter(
            doc,
            "chapter-one",
            full,
            image_annotations={annotation.path: annotation},
        )
        assert annotated == plain

    def test_code_blocks_pass_through_unquoted(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        assert "code stays put" in md
        assert "> code stays put" not in md

    def test_block_order_is_preserved(self, doc, full):
        md, _ = assemble_chapter(doc, "chapter-one", full)
        assert md.index("First paragraph") < md.index("Second paragraph")
        assert md.index("Second paragraph") < md.index("images/f1.png")


# ------------------------------------------------------------ 缺漏

class TestMissing:
    def test_missing_translation_is_reported(self, doc, full):
        one = next(b for b in doc.translatable_blocks()
                   if b.chapter == "chapter-one" and b.kind == "para")
        partial = {k: v for k, v in full.items() if k != one.id}
        _, rep = assemble_chapter(doc, "chapter-one", partial)
        assert one.id in rep.missing

    def test_missing_translation_keeps_english_and_marks_it(self, doc, full):
        """漏译不能静默跳过——原文要留着，并且要留下可见标记。"""
        one = next(b for b in doc.translatable_blocks()
                   if b.chapter == "chapter-one" and b.kind == "para")
        partial = {k: v for k, v in full.items() if k != one.id}
        md, _ = assemble_chapter(doc, "chapter-one", partial)
        assert one.text.split("\n")[0] in md
        assert "未译" in md or "TODO" in md

    def test_complete_assembly_reports_ok(self, doc, full):
        _, rep = assemble_chapter(doc, "chapter-one", full)
        assert rep.ok
        assert rep.missing == ()

    def test_empty_translation_counts_as_missing(self, doc, full):
        one = next(b for b in doc.translatable_blocks()
                   if b.chapter == "chapter-one" and b.kind == "para")
        t = dict(full, **{one.id: "   "})
        _, rep = assemble_chapter(doc, "chapter-one", t)
        assert one.id in rep.missing


# ------------------------------------------------------------ 整本

class TestBook:
    def test_assembles_each_chapter_separately(self, doc, full):
        out = assemble_book(doc, full)
        assert {c for c, _, _ in out} == {"chapter-one", "chapter-two"}

    def test_chapter_content_does_not_leak_across(self, doc, full):
        out = dict((c, md) for c, md, _ in assemble_book(doc, full))
        assert "Only paragraph of two" not in out["chapter-one"]
        assert "First paragraph here" not in out["chapter-two"]

    def test_book_report_aggregates_missing(self, doc):
        out = assemble_book(doc, {})
        assert all(not rep.ok for _, _, rep in out)

    def test_empty_document_yields_nothing(self):
        assert assemble_book(parse_markdown("", book_slug="t"), {}) == []


class TestTableOfContents:
    def test_stale_english_toc_is_dropped(self):
        """book2md 会生成一份指向英文锚点的目录。翻译后锚点全变了，
        原样带过去就是一堆死链——必须丢掉，由发布环节重建。"""
        src = ("# Book\n\n## 目录\n\n"
               "- [Chapter One](#chapter-one)\n- [Chapter Two](#chapter-two)\n\n"
               "# Chapter One\n\nSome prose.\n")
        doc = parse_markdown(src, book_slug="t")
        t = {b.id: "译" for b in doc.translatable_blocks()}
        md, _ = assemble_chapter(doc, "book", t)
        assert "(#chapter-one)" not in md


class TestHeadingCleanup:
    """真书上发现的：book2md 从 EPUB 抽出的标题常嵌着指向原书内部锚点的
    链接（如 `# [Title](#nav.xhtml_nch3)`）。原样带进译稿就是死链。"""

    def test_link_markup_is_stripped_from_headings(self):
        src = "# [Chapter 1 The Opening](#nav.xhtml_nch3)\n\nSome prose.\n"
        doc = parse_markdown(src, book_slug="t")
        t = {b.id: "第一章" if b.kind == "heading" else "译"
             for b in doc.translatable_blocks()}
        md, _ = assemble_chapter(doc, doc.chapter_slugs()[0], t)
        head = next(l for l in md.split("\n") if l.startswith("# "))
        assert "nav.xhtml" not in head
        assert "](" not in head
        assert "Chapter 1 The Opening" in head
        assert "第一章" in head

    def test_inline_heading_image_is_preserved_once_without_broken_literal(self):
        source = (
            "# Book\n\n"
            "### **PLAY** ![Images](images/arrow1.jpg) **Conduct Interviews**\n"
        )
        doc = parse_markdown(source, book_slug="t")
        translations = {
            block.id: (
                "**实战方法** ![Images](images/arrow1.jpg) **开展访谈**"
                if block.level == 3 else "书名"
            )
            for block in doc.translatable_blocks()
        }
        markdown, _ = assemble_chapter(doc, "book", translations)
        heading = next(line for line in markdown.splitlines() if line.startswith("### "))
        assert heading.count("![Images](images/arrow1.jpg)") == 1
        assert "!Images" not in heading
        assert "**实战方法** **开展访谈**" in heading

    def test_raw_svg_wrapper_is_preserved_once_without_translation_echo(self):
        source = "# Book\n\n<svg viewBox=\"0 0 10 10\">\n\n</svg>\n"
        doc = parse_markdown(source, book_slug="t")
        translations = {
            block.id: ("书名" if block.kind == "heading" else block.text)
            for block in doc.translatable_blocks()
        }
        markdown, report = assemble_chapter(doc, "book", translations)
        assert report.ok
        assert markdown.count("<svg viewBox=\"0 0 10 10\">") == 1
        assert markdown.count("</svg>") == 1
        assert "> <svg" not in markdown

    def test_plain_heading_is_untouched(self):
        src = "# Plain Title\n\nProse.\n"
        doc = parse_markdown(src, book_slug="t")
        t = {b.id: "普通标题" if b.kind == "heading" else "译"
             for b in doc.translatable_blocks()}
        md, _ = assemble_chapter(doc, doc.chapter_slugs()[0], t)
        head = next(l for l in md.split("\n") if l.startswith("# "))
        assert head == "# 普通标题 · Plain Title"

    def test_trailing_whitespace_in_heading_is_trimmed(self):
        src = "# [Spaced Title   ](#x)\n\nProse.\n"
        doc = parse_markdown(src, book_slug="t")
        t = {b.id: "标题" if b.kind == "heading" else "译"
             for b in doc.translatable_blocks()}
        md, _ = assemble_chapter(doc, doc.chapter_slugs()[0], t)
        head = next(l for l in md.split("\n") if l.startswith("# "))
        assert head == "# 标题 · Spaced Title"

    def test_model_supplied_markdown_prefix_is_not_duplicated(self):
        src = "# Plain Title\n\nProse.\n"
        doc = parse_markdown(src, book_slug="t")
        t = {b.id: "# 普通标题" if b.kind == "heading" else "译"
             for b in doc.translatable_blocks()}
        md, _ = assemble_chapter(doc, doc.chapter_slugs()[0], t)
        head = next(l for l in md.split("\n") if l.startswith("# "))
        assert head == "# 普通标题 · Plain Title"

    def test_model_supplied_internal_link_is_stripped(self):
        src = "# [Chapter 1](#nav.xhtml_nch1)\n\nProse.\n"
        doc = parse_markdown(src, book_slug="t")
        t = {
            b.id: "# [第一章](#nav.xhtml_nch1)" if b.kind == "heading" else "译"
            for b in doc.translatable_blocks()
        }
        md, _ = assemble_chapter(doc, doc.chapter_slugs()[0], t)
        head = next(l for l in md.split("\n") if l.startswith("# "))
        assert head == "# 第一章 · Chapter 1"

    @pytest.mark.parametrize(
        ("english", "chinese", "expected"),
        [
            ("Chapter 4 Job Hunting", "第4章 寻找任务", "第四章 寻找任务"),
            ("Chapter 10 Final Notes", "第 10 章 最后的观察", "第十章 最后的观察"),
        ],
    )
    def test_chapter_number_style_is_normalized(self, english, chinese, expected):
        doc = parse_markdown(f"# {english}\n\nProse.\n", book_slug="t")
        t = {
            b.id: chinese if b.kind == "heading" else "译"
            for b in doc.translatable_blocks()
        }
        md, _ = assemble_chapter(doc, doc.chapter_slugs()[0], t)
        head = next(l for l in md.split("\n") if l.startswith("# "))
        assert head.startswith(f"# {expected} ·")
