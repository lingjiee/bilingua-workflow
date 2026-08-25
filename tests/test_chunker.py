"""分块。

分块决定了三件事的上限：上下文连贯性、输出是否会被模型截断、
以及重跑时的最小粒度。这里的每条约束都对应一种真实的翻车方式。
"""

from __future__ import annotations

import pytest

from pipeline.chunker import Chunk, chunk_document
from pipeline.document import parse_markdown


def make_doc(chapters: dict[str, int], words_per_para: int = 100):
    """造一篇文档：{章节名: 段落数}。每段 words_per_para 个词。"""
    parts = []
    for name, n_paras in chapters.items():
        parts.append(f"# {name}\n")
        for i in range(n_paras):
            # 每段内容不同，避免 ID 撞车
            body = " ".join(f"w{i}x{j}" for j in range(words_per_para))
            parts.append(f"{body}\n")
    return parse_markdown("\n".join(parts), book_slug="t")


class TestChunkBoundaries:
    def test_chunk_never_crosses_chapter(self):
        doc = make_doc({"Alpha": 4, "Beta": 4})
        chunks = chunk_document(doc, target_words=10_000)
        for ch in chunks:
            assert len({b.chapter for b in ch.blocks}) == 1

    def test_every_translatable_block_lands_in_exactly_one_chunk(self):
        doc = make_doc({"Alpha": 7, "Beta": 5})
        chunks = chunk_document(doc, target_words=250)
        placed = [b.id for ch in chunks for b in ch.blocks]
        expected = [b.id for b in doc.translatable_blocks()]
        assert sorted(placed) == sorted(expected)
        assert len(placed) == len(set(placed)), "同一段被分进两个 chunk 会翻译两次"

    def test_blocks_keep_document_order(self):
        doc = make_doc({"Alpha": 9})
        chunks = chunk_document(doc, target_words=250)
        placed = [b.id for ch in chunks for b in ch.blocks]
        expected = [b.id for b in doc.translatable_blocks()]
        assert placed == expected

    def test_blocks_are_never_split_mid_block(self):
        """段落是最小翻译单位。切开段落会毁掉指代和语气。"""
        doc = make_doc({"Alpha": 5}, words_per_para=900)
        chunks = chunk_document(doc, target_words=500)
        all_texts = {b.text for ch in chunks for b in ch.blocks}
        for b in doc.translatable_blocks():
            assert b.text in all_texts

    def test_untranslatable_blocks_are_excluded(self):
        src = "# A\n\nProse here.\n\n![fig](f.png)\n\n```\ncode\n```\n\nMore prose.\n"
        doc = parse_markdown(src, book_slug="t")
        chunks = chunk_document(doc, target_words=10_000)
        kinds = {b.kind for ch in chunks for b in ch.blocks}
        assert "image" not in kinds and "code" not in kinds


class TestChunkSizing:
    def test_chunks_respect_target_size(self):
        doc = make_doc({"Alpha": 30}, words_per_para=100)
        chunks = chunk_document(doc, target_words=500, max_words=700)
        # 除每章最后一块外，都该接近目标
        for ch in chunks[:-1]:
            assert ch.word_count <= 700

    def test_oversized_single_block_gets_its_own_chunk(self):
        """一段就超预算时不能丢，也不能切开——单独成块并标记。"""
        doc = make_doc({"Alpha": 1}, words_per_para=5000)
        chunks = chunk_document(doc, target_words=500, max_words=700)
        # 标题不会被并进超限块，所以是 [标题] + [超限段] 两块
        over = [c for c in chunks if c.oversized]
        assert len(over) == 1
        assert len(over[0].blocks) == 1
        assert over[0].blocks[0].word_count == 5000

    def test_normal_chunks_are_not_flagged_oversized(self):
        doc = make_doc({"Alpha": 6}, words_per_para=100)
        chunks = chunk_document(doc, target_words=500)
        assert all(not ch.oversized for ch in chunks)

    def test_estimated_output_fits_model_ceiling(self):
        """中转站把 max_output_tokens 砍到 8192。超了模型会静默截断，
        而截断的译文校验时才发现，浪费一整轮。所以分块时就要挡住。"""
        doc = make_doc({"Alpha": 40}, words_per_para=100)
        chunks = chunk_document(doc, target_words=1500, max_output_tokens=8192)
        for ch in chunks:
            if not ch.oversized:
                assert ch.estimated_output_tokens < 8192

    def test_tight_output_ceiling_forces_smaller_chunks(self):
        doc = make_doc({"Alpha": 40}, words_per_para=100)
        loose = chunk_document(doc, target_words=1500, max_output_tokens=8192)
        tight = chunk_document(doc, target_words=1500, max_output_tokens=1000)
        assert len(tight) > len(loose)

    def test_oversized_block_is_reported_not_silently_dropped(self):
        doc = make_doc({"Alpha": 1}, words_per_para=9000)
        chunks = chunk_document(doc, target_words=500, max_output_tokens=8192)
        over = [c for c in chunks if c.oversized]
        assert len(over) == 1
        assert over[0].warnings, "超限必须留下可见记录"
        # 超限段绝不能被丢掉
        placed = {b.id for c in chunks for b in c.blocks}
        assert placed == {b.id for b in doc.translatable_blocks()}


class TestContextWindow:
    def test_prev_context_comes_from_preceding_blocks(self):
        doc = make_doc({"Alpha": 12}, words_per_para=100)
        chunks = chunk_document(doc, target_words=300, context_blocks=2)
        assert len(chunks) > 1
        second = chunks[1]
        prev_ids = [b.id for b in second.prev_context]
        all_ids = [b.id for b in doc.translatable_blocks()]
        first_idx = all_ids.index(second.blocks[0].id)
        assert prev_ids == all_ids[max(0, first_idx - 2):first_idx]

    def test_first_chunk_of_chapter_has_no_prev_context(self):
        doc = make_doc({"Alpha": 4, "Beta": 4})
        chunks = chunk_document(doc, target_words=10_000, context_blocks=2)
        beta = [c for c in chunks if c.chapter.startswith("beta")][0]
        assert beta.prev_context == ()

    def test_next_context_is_the_following_block(self):
        doc = make_doc({"Alpha": 12}, words_per_para=100)
        chunks = chunk_document(doc, target_words=300, context_blocks=2)
        first = chunks[0]
        all_ids = [b.id for b in doc.translatable_blocks()]
        last_idx = all_ids.index(first.blocks[-1].id)
        assert [b.id for b in first.next_context] == all_ids[last_idx + 1:last_idx + 2]

    def test_last_chunk_of_chapter_has_no_next_context(self):
        doc = make_doc({"Alpha": 4, "Beta": 4})
        chunks = chunk_document(doc, target_words=10_000, context_blocks=2)
        alpha = [c for c in chunks if c.chapter.startswith("alpha")][-1]
        assert alpha.next_context == ()

    def test_context_blocks_are_not_in_translation_payload(self):
        """上下文只进不出。混进 payload 会导致重复翻译和装配时重段。"""
        doc = make_doc({"Alpha": 12}, words_per_para=100)
        chunks = chunk_document(doc, target_words=300, context_blocks=2)
        for ch in chunks:
            payload = {b.id for b in ch.blocks}
            ctx = {b.id for b in ch.prev_context} | {b.id for b in ch.next_context}
            assert payload.isdisjoint(ctx)

    def test_context_never_crosses_chapter(self):
        doc = make_doc({"Alpha": 4, "Beta": 4})
        chunks = chunk_document(doc, target_words=10_000, context_blocks=3)
        for ch in chunks:
            for b in list(ch.prev_context) + list(ch.next_context):
                assert b.chapter == ch.chapter


class TestChunkIdentity:
    def test_chunk_ids_are_unique(self):
        doc = make_doc({"Alpha": 20, "Beta": 20}, words_per_para=100)
        chunks = chunk_document(doc, target_words=300)
        ids = [c.id for c in chunks]
        assert len(ids) == len(set(ids))

    def test_chunk_ids_are_deterministic(self):
        doc = make_doc({"Alpha": 20}, words_per_para=100)
        a = [c.id for c in chunk_document(doc, target_words=300)]
        b = [c.id for c in chunk_document(doc, target_words=300)]
        assert a == b

    def test_chunk_id_carries_chapter(self):
        doc = make_doc({"Alpha": 6})
        for ch in chunk_document(doc, target_words=300):
            assert ch.chapter in ch.id


class TestFlatStructureFallback:
    """Kalbach 那本 3041 段只有 2 个 H1。按章切会切出一个几千段的巨块。"""

    def test_huge_flat_chapter_still_produces_bounded_chunks(self):
        doc = make_doc({"OnlyChapter": 400}, words_per_para=100)
        chunks = chunk_document(doc, target_words=1500, max_words=1800)
        assert len(chunks) > 20
        assert all(c.word_count <= 1800 or c.oversized for c in chunks)

    def test_h2_subheadings_are_preferred_split_points(self):
        """有 H2 时优先在 H2 处断开，边界落在语义位置上。"""
        src = ["# Only Chapter\n"]
        for s in range(4):
            src.append(f"## Section {s}\n")
            for i in range(6):
                src.append(" ".join(f"s{s}p{i}w{j}" for j in range(100)) + "\n")
        doc = parse_markdown("\n".join(src), book_slug="t")
        chunks = chunk_document(doc, target_words=600, max_words=800)
        # 每个 chunk 里最多出现一个 H2，说明没跨小节乱切
        for ch in chunks:
            heads = [b for b in ch.blocks if b.kind == "heading" and b.level == 2]
            assert len(heads) <= 1


class TestEdgeCases:
    def test_empty_document_yields_no_chunks(self):
        doc = parse_markdown("", book_slug="t")
        assert chunk_document(doc) == []

    def test_document_with_only_untranslatable_blocks_yields_no_chunks(self):
        doc = parse_markdown("![a](a.png)\n\n```\nx\n```\n", book_slug="t")
        assert chunk_document(doc) == []

    def test_single_short_paragraph_yields_one_chunk(self):
        doc = parse_markdown("# A\n\nShort.\n", book_slug="t")
        chunks = chunk_document(doc)
        assert len(chunks) == 1
        assert len(chunks[0].blocks) == 2  # 标题 + 段落


class TestFragmentation:
    """真实书上发现的：H2 断点太激进会切出一堆碎块。
    碎块本身不算错，但每个 chunk 都要重发风格卡和术语表，
    切得越碎固定开销被乘得越多，上下文也越薄。"""

    def test_h2_split_does_not_produce_tiny_chunks(self):
        """小节很短时不该每节一个 chunk。"""
        src = ["# Chapter\n"]
        for s in range(12):
            src.append(f"## Section {s}\n")
            src.append(" ".join(f"s{s}w{j}" for j in range(60)) + "\n")
        doc = parse_markdown("\n".join(src), book_slug="t")
        chunks = chunk_document(doc, target_words=600)
        tiny = [c for c in chunks if c.word_count < 100]
        assert not tiny, f"出现 {len(tiny)} 个不足 100 词的碎块"

    def test_h2_split_still_happens_once_chunk_has_substance(self):
        """攒够量之后仍要在 H2 处断开，保住语义边界。"""
        src = ["# Chapter\n"]
        for s in range(4):
            src.append(f"## Section {s}\n")
            for i in range(6):
                src.append(" ".join(f"s{s}p{i}w{j}" for j in range(100)) + "\n")
        doc = parse_markdown("\n".join(src), book_slug="t")
        chunks = chunk_document(doc, target_words=600)
        for ch in chunks:
            heads = [b for b in ch.blocks if b.kind == "heading" and b.level == 2]
            assert len(heads) <= 1

    def test_min_split_words_is_tunable(self):
        src = ["# Chapter\n"]
        for s in range(12):
            src.append(f"## Section {s}\n")
            src.append(" ".join(f"s{s}w{j}" for j in range(60)) + "\n")
        doc = parse_markdown("\n".join(src), book_slug="t")
        eager = chunk_document(doc, target_words=600, min_split_words=0)
        lazy = chunk_document(doc, target_words=600, min_split_words=400)
        assert len(eager) > len(lazy)
