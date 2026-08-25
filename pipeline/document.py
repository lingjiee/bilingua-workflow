"""把 book2md 产出的 markdown 解析成带稳定身份的块序列。

段落身份是整条流水线的地基。用序号做主键的话，源书改一个字、或者
book2md 修一次抽取逻辑，后面所有段落的 ID 都会平移，缓存全废。
所以主键是 **书 + 章节 + 内容 hash**：改哪段只有哪段的 ID 变。

块的边界就是空行。book2md 的产物里段落不硬换行，所以这条规则可靠。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

__all__ = ["Block", "Document", "parse_markdown", "paragraph_id", "slugify"]

# 需要翻译的块：正文、标题、列表、引用、表格
TRANSLATABLE_KINDS = frozenset({"para", "heading", "list", "quote", "table"})

# 不翻译的块：图片引用、代码、目录、水平线
OPAQUE_KINDS = frozenset({"image", "code", "toc", "rule"})

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_IMAGE = re.compile(r"^!\[")
_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_QUOTE = re.compile(r"^\s*>")
_FENCE = re.compile(r"^\s*```")
_TABLE = re.compile(r"^\s*\|")
_RULE = re.compile(r"^\s*(\*\s*){3,}$|^\s*(-\s*){3,}$|^\s*(_\s*){3,}$")
# 目录条目：`- [标题](#锚点)` 或裸的 `[标题](#锚点)`
_TOC_LINE = re.compile(r"^\s*(?:[-*+]\s+)?\[[^\]]*\]\(#[^)]*\)\s*$")

_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_SPACE = re.compile(r"[\s_]+")


def slugify(text: str, max_len: int = 32) -> str:
    """标题 → 可读、可作路径片段的 slug。不含 `/`，因为 ID 用 `/` 分段。"""
    s = unicodedata.normalize("NFKD", text).strip().lower()
    s = _SLUG_STRIP.sub("", s)
    s = _SLUG_SPACE.sub("-", s).strip("-")
    return s[:max_len].strip("-") or "untitled"


def _normalize_for_hash(text: str) -> str:
    """哈希前归一化：折叠空白。这样重排空白不会让整段重译。"""
    return " ".join(text.split())


def paragraph_id(book: str, chapter: str, text: str, occurrence: int = 0) -> str:
    """稳定段落主键 `book/chapter/§hash`。

    occurrence 用于同章内出现完全相同的两段文字时消歧——章节小结、
    重复引语都会撞车，撞车会让装配时两段译文互相覆盖。
    """
    payload = f"{book}\x00{chapter}\x00{_normalize_for_hash(text)}"
    if occurrence:
        payload += f"\x00#{occurrence}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{book}/{chapter}/§{digest}"


@dataclass(frozen=True)
class Block:
    kind: str  # para | heading | list | quote | table | image | code | toc | rule
    text: str  # 原始 markdown
    chapter: str  # 章节 slug
    level: int = 0  # 标题层级，其余为 0
    id: str = ""  # 稳定主键，仅 translatable 块有意义

    @property
    def translatable(self) -> bool:
        return self.kind in TRANSLATABLE_KINDS

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class Document:
    book_slug: str
    blocks: list[Block] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def source_sha256(self) -> str:
        return str(self.meta.get("source_sha256", ""))

    @property
    def warnings(self) -> list[str]:
        w = self.meta.get("extraction_warnings") or []
        return [str(x) for x in w] if isinstance(w, list) else []

    def chapter_slugs(self) -> list[str]:
        seen: list[str] = []
        for b in self.blocks:
            if b.chapter and b.chapter not in seen:
                seen.append(b.chapter)
        return seen

    def translatable_blocks(self) -> list[Block]:
        return [b for b in self.blocks if b.translatable]

    def word_count(self) -> int:
        return sum(b.word_count for b in self.translatable_blocks())

    def by_id(self) -> dict[str, Block]:
        return {b.id: b for b in self.translatable_blocks()}


# ------------------------------------------------------------ frontmatter


def _split_frontmatter(src: str) -> tuple[dict, str]:
    """book2md 产物开头是 YAML frontmatter。没有 pyyaml 也要能跑，
    所以这里只做够用的浅解析：标量 + 一层列表。"""
    if not src.startswith("---"):
        return {}, src
    end = src.find("\n---", 3)
    if end == -1:
        return {}, src
    raw = src[3:end].strip("\n")
    rest = src[end + 4 :]

    meta: dict = {}
    current_list_key: str | None = None
    for line in raw.split("\n"):
        if not line.strip():
            continue
        if re.match(r"^\s+-\s+", line) and current_list_key:
            meta[current_list_key].append(line.split("-", 1)[1].strip())
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if val:
                meta[key] = val
                current_list_key = None
            else:
                meta[key] = []
                current_list_key = key
        else:
            current_list_key = None
    return meta, rest


# ------------------------------------------------------------ block split


def _raw_blocks(body: str) -> list[list[str]]:
    """按空行切块，但代码围栏内的空行不算边界。"""
    blocks: list[list[str]] = []
    cur: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            cur.append(line)
            if not in_fence:
                blocks.append(cur)
                cur = []
            continue
        if in_fence:
            cur.append(line)
            continue
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def _classify(lines: list[str]) -> tuple[str, int]:
    first = lines[0]
    if _FENCE.match(first):
        return "code", 0
    if _IMAGE.match(first.strip()):
        return "image", 0
    m = _HEADING.match(first.strip())
    if m:
        return "heading", len(m.group(1))
    if _RULE.match(first):
        return "rule", 0
    # 目录：块内绝大多数行都是指向锚点的链接
    link_lines = sum(1 for ln in lines if _TOC_LINE.match(ln))
    if link_lines and link_lines >= max(1, int(len(lines) * 0.8)):
        return "toc", 0
    if _QUOTE.match(first):
        return "quote", 0
    if _LIST.match(first):
        return "list", 0
    if _TABLE.match(first):
        return "table", 0
    return "para", 0


# ------------------------------------------------------------ parse


def parse_markdown(
    src: str,
    book_slug: str,
    chapter_levels: tuple[int, ...] = (1,),
) -> Document:
    """解析 book2md 产物。返回带稳定 ID 的块序列。

    ``chapter_levels`` makes an extraction-specific heading hierarchy explicit
    without rewriting the immutable source. Most books use H1; books whose EPUB
    conversion retained chapters as H2 can opt into ``(1, 2)``.
    """
    levels = frozenset(chapter_levels)
    if not levels or any(level < 1 or level > 6 for level in levels):
        raise ValueError("chapter_levels 必须包含 1—6 之间的标题级别。")
    meta, body = _split_frontmatter(src)
    doc = Document(book_slug=book_slug, meta=meta)
    if not body.strip():
        return doc

    chapter = "front"
    chapter_names: dict[str, int] = {}
    # (chapter, normalized_text) -> 已出现次数，用于同章重复段消歧
    seen: dict[tuple[str, str], int] = {}

    for lines in _raw_blocks(body):
        kind, level = _classify(lines)
        text = "\n".join(lines)

        if kind == "heading" and level in levels:
            base = slugify(_HEADING.match(lines[0].strip()).group(2))
            n = chapter_names.get(base, 0)
            chapter_names[base] = n + 1
            chapter = base if n == 0 else f"{base}-{n + 1}"

        block_id = ""
        if kind in TRANSLATABLE_KINDS:
            key = (chapter, _normalize_for_hash(text))
            occ = seen.get(key, 0)
            seen[key] = occ + 1
            block_id = paragraph_id(book_slug, chapter, text, occurrence=occ)

        doc.blocks.append(Block(kind=kind, text=text, chapter=chapter, level=level, id=block_id))
    return doc


def load(
    path,
    book_slug: str | None = None,
    chapter_levels: tuple[int, ...] = (1,),
) -> Document:
    """从文件读。book_slug 省略时用文件名前 3 个词首字母的缩写。"""
    from pathlib import Path

    p = Path(path)
    if book_slug is None:
        words = re.split(r"[-_\s]+", p.stem)[:3]
        book_slug = "".join(w[0] for w in words if w).lower() or "book"
    return parse_markdown(
        p.read_text(encoding="utf-8"),
        book_slug=book_slug,
        chapter_levels=chapter_levels,
    )
