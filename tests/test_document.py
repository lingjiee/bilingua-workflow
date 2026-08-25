"""段落身份与文档解析。

这是整条流水线的地基：段落 ID 错了，缓存失效计算、断点续传、
"改一个术语只重建受影响段落"全都塌掉。所以先测它。
"""

from __future__ import annotations

import pytest

from pipeline.document import Block, Document, parse_markdown, paragraph_id


# ---------------------------------------------------------------- 段落 ID

class TestParagraphId:
    def test_same_text_same_chapter_gives_same_id(self):
        a = paragraph_id("cal", "ch03", "The quick brown fox.", occurrence=0)
        b = paragraph_id("cal", "ch03", "The quick brown fox.", occurrence=0)
        assert a == b

    def test_different_text_gives_different_id(self):
        a = paragraph_id("cal", "ch03", "The quick brown fox.", occurrence=0)
        b = paragraph_id("cal", "ch03", "The quick brown cat.", occurrence=0)
        assert a != b

    def test_whitespace_only_change_keeps_id_stable(self):
        """重排空白、加尾空格不该让整段重译。"""
        a = paragraph_id("cal", "ch03", "The quick  brown fox.", occurrence=0)
        b = paragraph_id("cal", "ch03", "  The quick brown fox.  ", occurrence=0)
        assert a == b

    def test_same_text_different_chapter_gives_different_id(self):
        a = paragraph_id("cal", "ch03", "Same words.", occurrence=0)
        b = paragraph_id("cal", "ch07", "Same words.", occurrence=0)
        assert a != b

    def test_same_text_different_book_gives_different_id(self):
        a = paragraph_id("cal", "ch03", "Same words.", occurrence=0)
        b = paragraph_id("wck", "ch03", "Same words.", occurrence=0)
        assert a != b

    def test_duplicate_paragraph_in_same_chapter_is_disambiguated(self):
        """同章出现两段一模一样的文字（章节小结、重复引语），ID 必须能区分，
        否则装配时两段译文会互相覆盖。"""
        a = paragraph_id("cal", "ch03", "Repeated line.", occurrence=0)
        b = paragraph_id("cal", "ch03", "Repeated line.", occurrence=1)
        assert a != b

    def test_id_format_is_readable_and_parseable(self):
        pid = paragraph_id("cal", "ch03", "Hello.", occurrence=0)
        assert pid.startswith("cal/ch03/§")
        book, chapter, frag = pid.split("/")
        assert book == "cal"
        assert chapter == "ch03"
        assert frag.startswith("§")

    def test_id_is_short_enough_to_eyeball(self):
        pid = paragraph_id("cal", "ch03", "Hello.", occurrence=0)
        assert len(pid) < 40


# ---------------------------------------------------------------- 解析

FRONTMATTER = """---
title: Example Book
source_file: example.epub
source_sha256: abc123
extraction_warnings:
  - no printed page numbers in this EPUB
---
"""

SAMPLE = FRONTMATTER + """
# Example Book

## 目录

- [First Chapter](#first-chapter)
- [Second Chapter](#second-chapter)

# First Chapter

## Opening

A first paragraph of prose.

A second paragraph of prose.

![figure one](images/fig01.png)

- a list item
- another list item

> a quoted line

```
code block, not prose
```

# Second Chapter

Prose under the second chapter.
"""


class TestParseMarkdown:
    @pytest.fixture
    def doc(self) -> Document:
        return parse_markdown(SAMPLE, book_slug="ex")

    def test_frontmatter_is_captured_not_translated(self, doc):
        assert doc.meta["source_sha256"] == "abc123"
        assert all(b.kind != "meta" or not b.translatable for b in doc.blocks)

    def test_source_sha_is_available_for_staleness_checks(self, doc):
        assert doc.source_sha256 == "abc123"

    def test_extraction_warnings_are_preserved(self, doc):
        assert any("page numbers" in w for w in doc.warnings)

    def test_prose_paragraphs_are_translatable(self, doc):
        prose = [b for b in doc.blocks if b.kind == "para"]
        assert len(prose) >= 3
        assert all(b.translatable for b in prose)

    def test_headings_are_translatable(self, doc):
        heads = [b for b in doc.blocks if b.kind == "heading"]
        assert heads, "标题也要翻译，否则目录是英文正文是中文"
        assert all(b.translatable for b in heads)

    def test_images_are_not_translatable(self, doc):
        imgs = [b for b in doc.blocks if b.kind == "image"]
        assert len(imgs) == 1
        assert not imgs[0].translatable

    def test_code_blocks_are_not_translatable(self, doc):
        code = [b for b in doc.blocks if b.kind == "code"]
        assert len(code) == 1
        assert not code[0].translatable

    def test_toc_is_not_translatable(self, doc):
        """目录是自动生成的链接列表，翻正文时会自动重建，不该消耗 token。"""
        toc = [b for b in doc.blocks if b.kind == "toc"]
        assert toc, "应识别出目录块"
        assert all(not b.translatable for b in toc)

    def test_lists_and_quotes_are_translatable(self, doc):
        kinds = {b.kind for b in doc.blocks}
        assert "list" in kinds and "quote" in kinds
        for b in doc.blocks:
            if b.kind in ("list", "quote"):
                assert b.translatable

    def test_blocks_carry_chapter_path(self, doc):
        prose = [b for b in doc.blocks if b.kind == "para" and "second" in b.text.lower()]
        assert prose
        assert prose[0].chapter != ""

    def test_chapters_are_split_on_h1(self, doc):
        chapters = doc.chapter_slugs()
        assert len(chapters) >= 2

    def test_explicit_h2_promotion_preserves_source_and_splits_chapters(self):
        source = "# Book\n\nFront prose.\n\n## Chapter One\n\nBody.\n"
        doc = parse_markdown(source, book_slug="x", chapter_levels=(1, 2))
        assert doc.chapter_slugs() == ["book", "chapter-one"]
        body = next(block for block in doc.blocks if block.text == "Body.")
        assert body.chapter == "chapter-one"
        assert next(block for block in doc.blocks if block.text.startswith("##")).level == 2

    def test_invalid_chapter_level_is_rejected(self):
        with pytest.raises(ValueError):
            parse_markdown("# Book\n", book_slug="x", chapter_levels=(0,))

    def test_every_translatable_block_has_unique_id(self, doc):
        ids = [b.id for b in doc.blocks if b.translatable]
        assert len(ids) == len(set(ids)), "段落 ID 撞车会导致译文互相覆盖"

    def test_reparsing_same_source_yields_identical_ids(self):
        a = parse_markdown(SAMPLE, book_slug="ex")
        b = parse_markdown(SAMPLE, book_slug="ex")
        assert [x.id for x in a.blocks] == [x.id for x in b.blocks]

    def test_editing_one_paragraph_does_not_shift_other_ids(self):
        """这是 hash 主键相对序号主键的全部意义所在。"""
        original = parse_markdown(SAMPLE, book_slug="ex")
        edited_src = SAMPLE.replace(
            "A first paragraph of prose.", "A first paragraph of prose, revised."
        )
        edited = parse_markdown(edited_src, book_slug="ex")

        orig_ids = {b.id for b in original.blocks if b.translatable}
        new_ids = {b.id for b in edited.blocks if b.translatable}
        # 只有被改的那段换了 ID，其余全部保留
        assert len(orig_ids - new_ids) == 1
        assert len(new_ids - orig_ids) == 1

    def test_word_count_counts_only_translatable_prose(self, doc):
        assert doc.word_count() > 0
        # 代码块里的词不该计入翻译预算
        assert doc.word_count() < len(SAMPLE.split())


class TestRealBookShape:
    """针对 book2md 真实产物的形状回归。"""

    def test_handles_document_with_no_frontmatter(self):
        doc = parse_markdown("# Title\n\nSome prose.\n", book_slug="x")
        assert doc.meta == {}
        assert doc.source_sha256 == ""
        assert any(b.translatable for b in doc.blocks)

    def test_handles_empty_document(self):
        doc = parse_markdown("", book_slug="x")
        assert doc.blocks == []
        assert doc.word_count() == 0

    def test_consecutive_list_lines_group_into_one_block(self):
        doc = parse_markdown("# T\n\n- one\n- two\n- three\n", book_slug="x")
        lists = [b for b in doc.blocks if b.kind == "list"]
        assert len(lists) == 1, "连续列表行应合成一个块，否则上下文被切碎"
        assert lists[0].text.count("\n") == 2
