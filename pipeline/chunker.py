"""把文档切成可翻译的 chunk。

三条约束决定了这里的每个判断：

1. **段落是最小单位。** 切开段落会毁掉指代和语气，所以 chunk 边界永远
   落在块边界上，宁可让某个 chunk 超预算也不切开一段。
2. **输出有硬上限。** 中转站常把 max_output_tokens 砍得很低（实测这家
   只有 8192）。超了模型会静默截断，而截断要等校验时才发现，白跑一轮。
   所以分块时就按预估输出量挡住。
3. **上下文只进不出。** prev/next 上下文给模型看，但不许它翻译，
   否则 chunk 边界会产生重复段落。
"""

from __future__ import annotations

from dataclasses import dataclass

from .document import Block, Document

__all__ = ["Chunk", "chunk_document"]

# 英译中的膨胀系数：1 个英文词大约 1.6 个汉字，1 个汉字约 1.1 token。
# 取偏高值，宁可 chunk 小一点也不要撞上截断。
OUTPUT_TOKENS_PER_WORD = 1.8
# 每段在 JSON 回包里的结构开销（id 字段、括号、转义）
PER_BLOCK_OVERHEAD_TOKENS = 40


@dataclass(frozen=True)
class Chunk:
    id: str
    chapter: str
    blocks: tuple[Block, ...]
    prev_context: tuple[Block, ...] = ()
    next_context: tuple[Block, ...] = ()
    oversized: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def word_count(self) -> int:
        return sum(b.word_count for b in self.blocks)

    @property
    def estimated_output_tokens(self) -> int:
        return (
            int(self.word_count * OUTPUT_TOKENS_PER_WORD)
            + len(self.blocks) * PER_BLOCK_OVERHEAD_TOKENS
        )

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(b.id for b in self.blocks)


def _estimate(words: int, n_blocks: int) -> int:
    return int(words * OUTPUT_TOKENS_PER_WORD) + n_blocks * PER_BLOCK_OVERHEAD_TOKENS


def _group_by_chapter(blocks: list[Block]) -> list[tuple[str, list[Block]]]:
    """保序分组。不用 dict 是因为同名章节可能不相邻。"""
    groups: list[tuple[str, list[Block]]] = []
    for b in blocks:
        if groups and groups[-1][0] == b.chapter:
            groups[-1][1].append(b)
        else:
            groups.append((b.chapter, [b]))
    return groups


def _is_section_head(b: Block) -> bool:
    return b.kind == "heading" and b.level == 2


def _split_chapter(
    blocks: list[Block],
    target_words: int,
    max_words: int,
    max_output_tokens: int,
    min_split_words: int,
) -> list[tuple[list[Block], bool, list[str]]]:
    """把一章切成若干 (blocks, oversized, warnings)。"""
    out: list[tuple[list[Block], bool, list[str]]] = []
    cur: list[Block] = []
    cur_words = 0

    def flush(oversized: bool = False, warns: list[str] | None = None) -> None:
        nonlocal cur, cur_words
        if cur:
            out.append((cur, oversized, warns or []))
            cur, cur_words = [], 0

    for b in blocks:
        w = b.word_count
        has_content = any(x.kind != "heading" for x in cur)

        # 单块自己就超预算：不能切开，单独成块并留下记录
        if w > max_words or _estimate(w, 1) > max_output_tokens:
            flush()
            warns = [
                f"单块 {w} 词，预估输出 {_estimate(w, 1)} tokens，"
                f"超出上限（{max_words} 词 / {max_output_tokens} tokens）——"
                f"段落不可切分，原样提交，需人工留意是否被截断"
            ]
            out.append(([b], True, warns))
            continue

        # 遇到小节标题就断开，让边界落在语义位置——但要求当前块已攒够量。
        # 无条件断会把小节密集的书切成一堆碎块（实测某本 8 万词被切成 103 块，
        # 最小的只有 11 词），每块都要重发风格卡和术语表，固定开销被乘穿。
        if _is_section_head(b) and has_content and cur_words >= min_split_words:
            flush()

        # 词数或预估输出任一超限就断开
        would_words = cur_words + w
        would_tokens = _estimate(would_words, len(cur) + 1)
        if cur and (would_words > target_words or would_tokens > max_output_tokens):
            flush()

        cur.append(b)
        cur_words += w

    flush()
    return out


def chunk_document(
    doc: Document,
    target_words: int = 1500,
    max_words: int | None = None,
    context_blocks: int = 2,
    max_output_tokens: int = 8192,
    min_split_words: int | None = None,
) -> list[Chunk]:
    """把文档切成 chunk 列表。

    target_words       每个 chunk 的目标英文词数
    max_words          单块超过它就独立成块并标记 oversized（默认 target 的 1.2 倍）
    context_blocks     注入多少个前文块作为上下文（不翻译）
    max_output_tokens  模型输出上限，预估超出就提前断开
    min_split_words    攒够这么多词才允许在小节标题处断开，防碎片化
    """
    if max_words is None:
        max_words = int(target_words * 1.2)
    if min_split_words is None:
        min_split_words = int(target_words * 0.4)

    blocks = doc.translatable_blocks()
    if not blocks:
        return []

    chunks: list[Chunk] = []
    for chapter, chapter_blocks in _group_by_chapter(blocks):
        index = {b.id: i for i, b in enumerate(chapter_blocks)}
        pieces = _split_chapter(
            chapter_blocks,
            target_words,
            max_words,
            max_output_tokens,
            min_split_words,
        )
        for n, (piece, oversized, warns) in enumerate(pieces, start=1):
            first_i = index[piece[0].id]
            last_i = index[piece[-1].id]
            chunks.append(
                Chunk(
                    id=f"{chapter}/c{n:02d}",
                    chapter=chapter,
                    blocks=tuple(piece),
                    # 上下文一律取自同章，跨章的前文对理解没有帮助只有干扰
                    prev_context=tuple(chapter_blocks[max(0, first_i - context_blocks) : first_i]),
                    # 后文只给一块，用途仅是消解指代歧义
                    next_context=tuple(chapter_blocks[last_i + 1 : last_i + 2]),
                    oversized=oversized,
                    warnings=tuple(warns),
                )
            )
    return chunks


def summarize(chunks: list[Chunk]) -> str:
    """给人看的一行式统计。"""
    if not chunks:
        return "0 chunks"
    words = sum(c.word_count for c in chunks)
    over = [c for c in chunks if c.oversized]
    tokens = sum(c.estimated_output_tokens for c in chunks)
    biggest = max(chunks, key=lambda c: c.word_count)
    return (
        f"{len(chunks)} chunks · {words:,} 词 · 预估输出 {tokens:,} tokens · "
        f"最大 chunk {biggest.word_count} 词 · 超限 {len(over)} 块"
    )
