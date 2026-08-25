"""可恢复的单书构建工作流：翻译、校验、装配，产物只写 build/。"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from .assemble import AssemblyReport, assemble_book
from .artifact_verify import verify_artifact
from .chunker import Chunk, chunk_document, summarize
from .client import ChunkResult, ProviderConfig, TRANSLATE_SYSTEM, TranslationClient
from .document import Document, load
from .glossary import Sense, Snapshot
from .state import (
    BuildIdentity,
    StaleBuildError,
    append_translation,
    load_translations,
    open_state,
    save_state,
)
from .verify import VerificationReport, verify_chapter
from .visuals import VisualAnnotation


class BuildAlreadyRunning(RuntimeError):
    """The same build root is already owned by another live process."""


@contextmanager
def _exclusive_build_lock(path: Path):
    """Non-blocking cross-platform process lock; the OS releases it on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BuildAlreadyRunning(
                    f"已有构建进程占用 {path.parent}；请等待其结束后再重试。"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BuildAlreadyRunning(
                    f"已有构建进程占用 {path.parent}；请等待其结束后再重试。"
                ) from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _single_process_build(func):
    @wraps(func)
    def wrapped(source_path, build_root, *args, **kwargs):
        lock_path = Path(build_root) / ".bilingua-build.lock"
        with _exclusive_build_lock(lock_path):
            return func(source_path, build_root, *args, **kwargs)

    return wrapped

__all__ = ["BuildReport", "build_book", "write_chunk_manifest"]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))[:16]


def _chunk_plan(chunks: list[Chunk]) -> str:
    payload = [
        {"id": chunk.id, "block_ids": list(chunk.block_ids)}
        for chunk in chunks
    ]
    return _sha256_text(json.dumps(payload, sort_keys=True))


@dataclass(frozen=True)
class BuildReport:
    book_slug: str
    build_dir: Path
    chunk_count: int
    completed_chunks: int
    verification: tuple[VerificationReport, ...]
    assembly: tuple[AssemblyReport, ...]
    artifacts: tuple[VerificationReport, ...] = ()
    total_cost: float = 0.0

    @property
    def ok(self) -> bool:
        return (
            self.completed_chunks == self.chunk_count
            and all(report.ok for report in self.verification)
            and all(report.ok for report in self.assembly)
            and all(report.ok for report in self.artifacts)
        )


def write_chunk_manifest(path, chunks: list[Chunk], source_sha256: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in chunks:
            record = {
                "chunk_id": chunk.id,
                "chapter": chunk.chapter,
                "block_ids": list(chunk.block_ids),
                "word_count": chunk.word_count,
                "estimated_output_tokens": chunk.estimated_output_tokens,
                "oversized": chunk.oversized,
                "warnings": list(chunk.warnings),
                "source_sha256": source_sha256,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _senses_for_chunk(snapshot: Snapshot | None, chunk: Chunk) -> list[Sense]:
    if snapshot is None:
        return []
    text = "\n".join(block.text for block in chunk.blocks)
    return snapshot.hits(text)


def _total_cost(records: dict[str, dict]) -> float:
    total = 0.0
    for record in records.values():
        value = (record.get("usage") or {}).get("cost", 0)
        try:
            total += float(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def _write_verification(path: Path, reports: list[VerificationReport]) -> None:
    text = "\n".join(report.to_markdown().rstrip() for report in reports).rstrip() + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


@_single_process_build
def build_book(
    source_path,
    build_root,
    cfg: ProviderConfig,
    style_card: str,
    snapshot: Snapshot | None = None,
    book_slug: str | None = None,
    chapter_cards: dict[str, str] | None = None,
    target_words: int = 1500,
    context_blocks: int = 2,
    client: TranslationClient | None = None,
    chapters: tuple[str, ...] | list[str] | None = None,
    chapter_levels: tuple[int, ...] = (1,),
    image_annotations: dict[str, VisualAnnotation] | None = None,
) -> BuildReport:
    """构建一本书。中断后再次调用会只提交未完成 chunk。"""
    source = Path(source_path)
    source_bytes = source.read_bytes()
    source_sha256 = _sha256_bytes(source_bytes)
    doc: Document = load(
        source,
        book_slug=book_slug,
        chapter_levels=chapter_levels,
    )
    if chapters:
        requested = tuple(dict.fromkeys(chapters))
        available = set(doc.chapter_slugs())
        missing = [chapter for chapter in requested if chapter not in available]
        if missing:
            raise ValueError(
                "找不到指定章节：" + ", ".join(missing)
                + "；可用章节：" + ", ".join(doc.chapter_slugs())
            )
        selected = set(requested)
        doc = Document(
            book_slug=doc.book_slug,
            blocks=[block for block in doc.blocks if block.chapter in selected],
            meta=dict(doc.meta),
        )
    chunks = chunk_document(
        doc,
        target_words=target_words,
        context_blocks=context_blocks,
        max_output_tokens=cfg.max_output_tokens,
    )
    if not chunks:
        raise ValueError(f"{source} 没有可翻译块。")

    build_dir = Path(build_root) / doc.book_slug
    build_dir.mkdir(parents=True, exist_ok=True)
    state_path = build_dir / "state.json"
    journal_path = build_dir / "translations.jsonl"
    write_chunk_manifest(build_dir / "chunks.jsonl", chunks, source_sha256)

    identity = BuildIdentity(
        book_slug=doc.book_slug,
        source_sha256=source_sha256,
        glossary_version=snapshot.version if snapshot else "none",
        style_version=_sha256_text(style_card),
        provider=f"{cfg.protocol}:{cfg.auth}:{cfg.endpoint()}",
        model=cfg.model,
        chunk_plan=_chunk_plan(chunks),
        prompt_version=_sha256_text(TRANSLATE_SYSTEM),
    )
    by_chunk = {chunk.id: chunk for chunk in chunks}
    state = open_state(
        state_path,
        identity,
        {chunk.id: chunk.block_ids for chunk in chunks},
    )
    translations, records = load_translations(journal_path)

    unknown_records = set(records) - set(by_chunk)
    if unknown_records:
        raise StaleBuildError(
            "translations.jsonl 含有当前计划之外的 chunk："
            + ", ".join(sorted(unknown_records))
        )
    # 日志是结果真相。崩溃可能发生在 append_translation 与 save_state 之间：
    # 完整日志必须能把 pending/failed 状态自愈为 done，避免付费重复请求；
    # 反过来，状态声称 done 但日志不完整时仍必须重跑。
    for chunk_id, progress in state.chunks.items():
        record = records.get(chunk_id) or {}
        got = set((record.get("translations") or {}).keys())
        expected = set(progress.block_ids)
        if got == expected:
            if progress.status != "done":
                state.mark_done(
                    chunk_id,
                    int(record.get("attempts") or 0),
                    dict(record.get("usage") or {}),
                )
        elif progress.status == "done":
            state.mark_failed(chunk_id, "完成状态与译文日志不一致，已安排重跑。")
    save_state(state, state_path)

    translation_client = client or TranslationClient(cfg)
    pending = [by_chunk[chunk_id] for chunk_id in state.pending_chunk_ids]
    cards = chapter_cards or {}

    def translate(chunk: Chunk) -> ChunkResult:
        return translation_client.translate_chunk(
            chunk,
            style_card=style_card,
            chapter_card=cards.get(chunk.chapter, ""),
            senses=_senses_for_chunk(snapshot, chunk),
        )

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as executor:
            futures = {executor.submit(translate, chunk): chunk for chunk in pending}
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    result = future.result()
                    if not result.is_complete:
                        raise RuntimeError(
                            f"ID 集合不一致；missing={result.missing_ids}, "
                            f"extra={result.extra_ids}"
                        )
                    append_translation(
                        journal_path,
                        chunk.id,
                        result.translations,
                        result.usage,
                        result.attempts,
                    )
                    state.mark_done(chunk.id, result.attempts, result.usage)
                except Exception as exc:  # noqa: BLE001
                    state.mark_failed(chunk.id, f"{type(exc).__name__}: {exc}")
                save_state(state, state_path)

    translations, records = load_translations(journal_path)
    senses = list(snapshot.senses) if snapshot else []
    verification = [
        verify_chapter(doc, chapter, translations, senses=senses)
        for chapter in doc.chapter_slugs()
    ]
    _write_verification(build_dir / "verify-report.md", verification)

    assembly_reports: list[AssemblyReport] = []
    artifact_reports: list[VerificationReport] = []
    if state.done_count == len(chunks) and all(report.ok for report in verification):
        chapters_dir = build_dir / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        for chapter, markdown, report in assemble_book(
            doc,
            translations,
            image_annotations=image_annotations,
        ):
            assembly_reports.append(report)
            artifact_report = verify_artifact(doc, chapter, translations, markdown)
            artifact_reports.append(artifact_report)
            if report.ok and artifact_report.ok:
                (chapters_dir / f"{chapter}.md").write_text(
                    markdown, encoding="utf-8", newline="\n"
                )
        _write_verification(build_dir / "artifact-verify-report.md", artifact_reports)

    report = BuildReport(
        book_slug=doc.book_slug,
        build_dir=build_dir,
        chunk_count=len(chunks),
        completed_chunks=state.done_count,
        verification=tuple(verification),
        assembly=tuple(assembly_reports),
        artifacts=tuple(artifact_reports),
        total_cost=_total_cost(records),
    )
    (build_dir / "summary.txt").write_text(
        summarize(chunks)
        + f"\n完成 {report.completed_chunks}/{report.chunk_count}"
        + f"\n费用 ${report.total_cost:.6f}"
        + f"\n状态 {'ok' if report.ok else 'blocked'}\n",
        encoding="utf-8",
        newline="\n",
    )
    return report
