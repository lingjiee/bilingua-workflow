"""把已通过校验的整本书一次性发布到 Obsidian vault。"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .verify import VerificationReport

__all__ = [
    "PublicationBlockedError",
    "PublicationReport",
    "publish_book",
    "render_index",
]


class PublicationBlockedError(RuntimeError):
    """校验未通过或目标不安全，禁止写入 vault。"""


@dataclass(frozen=True)
class PublicationReport:
    destination: Path
    chapter_files: tuple[str, ...]


_WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _safe_component(value: str, fallback: str) -> str:
    clean = _WINDOWS_FORBIDDEN.sub("-", value).strip().rstrip(". ")
    clean = re.sub(r"\s+", " ", clean)
    clean = re.sub(r"\.{2,}", "-", clean).lstrip(". ")
    if clean in {"", ".", ".."}:
        clean = fallback
    return clean[:120].rstrip(". ") or fallback


def _chapter_title(slug: str, markdown: str) -> str:
    match = _HEADING.search(markdown)
    if not match:
        return _safe_component(slug.replace("-", " ").title(), "章节")
    title = match.group(1).split(" · ", 1)[0].strip()
    return _safe_component(title, _safe_component(slug, "章节"))


def render_index(
    book_title: str,
    chapters: list[tuple[str, str]],
    metadata: dict[str, str] | None = None,
) -> str:
    lines = [f"# {book_title}", "", "## 章节", ""]
    for filename, title in chapters:
        stem = Path(filename).stem
        lines.append(f"- [[{stem}|{title}]]")
    if metadata:
        lines.extend(["", "## 构建信息", ""])
        for key, value in metadata.items():
            lines.append(f"- **{key}**：{value}")
    return "\n".join(lines).rstrip() + "\n"


def publish_book(
    vault_root,
    folder_name: str,
    book_title: str,
    chapters: list[tuple[str, str]],
    verification_reports: list[VerificationReport] | tuple[VerificationReport, ...],
    metadata: dict[str, str] | None = None,
    images_dir=None,
) -> PublicationReport:
    """发布新目录。目标已存在时拒绝覆盖，避免静默破坏现有笔记。"""
    failed = [report.chapter for report in verification_reports if not report.ok]
    if failed:
        raise PublicationBlockedError("以下章节校验未通过，禁止发布：" + ", ".join(failed))
    if not chapters:
        raise PublicationBlockedError("没有可发布的章节。")

    root = Path(vault_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_folder = _safe_component(folder_name, "译本")
    destination = root / safe_folder
    if destination.exists():
        raise FileExistsError(f"发布目标已存在：{destination}。为保护现有笔记，本版本不自动覆盖。")

    staging = root / f".bilingua-{safe_folder}-{uuid.uuid4().hex}.tmp"
    chapter_links: list[tuple[str, str]] = []
    filenames: list[str] = []
    try:
        staging.mkdir()
        for index, (slug, markdown) in enumerate(chapters, start=1):
            title = _chapter_title(slug, markdown)
            filename = f"{index:02d} {title}.md"
            (staging / filename).write_text(markdown, encoding="utf-8", newline="\n")
            chapter_links.append((filename, title))
            filenames.append(filename)

        index_text = render_index(book_title, chapter_links, metadata=metadata)
        (staging / "00 索引.md").write_text(index_text, encoding="utf-8", newline="\n")

        if images_dir is not None:
            source_images = Path(images_dir)
            if source_images.exists():
                shutil.copytree(source_images, staging / "images")

        os.replace(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return PublicationReport(destination=destination, chapter_files=tuple(filenames))
