"""把人工或 Codex 复核结果安全地并入追加式译文日志。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .document import Block, Document
from .glossary import Sense
from .state import (
    append_translation_batch,
    load_state,
    load_translations,
    save_state,
)
from .verify import (
    Finding,
    Severity,
    VerificationReport,
    verify_block,
    verify_corpus_consistency,
)

__all__ = [
    "ReviewImportError",
    "ReviewImportReport",
    "apply_review",
    "write_review_context",
]

_CONTEXT_SCHEMA_VERSION = 1


class ReviewImportError(ValueError):
    """审核补丁与当前构建或质量门禁不一致。"""


@dataclass(frozen=True)
class ReviewImportReport:
    chunk_count: int
    revised_block_count: int
    warning_count: int = 0
    verification_path: Path | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_review_context(
    build_dir: str | Path,
    doc: Document,
    senses: list[Sense] | tuple[Sense, ...],
    *,
    source_sha256: str,
    glossary_version: str,
) -> Path:
    """Persist the immutable source/glossary context needed to validate reviews."""
    path = Path(build_dir) / "review-context.json"
    payload = {
        "schema_version": _CONTEXT_SCHEMA_VERSION,
        "book_slug": doc.book_slug,
        "source_sha256": source_sha256,
        "glossary_version": glossary_version,
        "blocks": [
            {
                "id": block.id,
                "kind": block.kind,
                "text": block.text,
                "chapter": block.chapter,
                "level": block.level,
            }
            for block in doc.blocks
            if block.translatable
        ],
        "senses": [sense.to_dict() for sense in senses],
    }
    _atomic_json(path, payload)
    return path


def _load_review_context(
    build: Path,
    *,
    book_slug: str,
    source_sha256: str,
    glossary_version: str,
) -> tuple[Document, list[Sense]]:
    path = build / "review-context.json"
    if not path.exists():
        raise ReviewImportError(
            "构建目录缺少 review-context.json；请先用原 build 命令离线重跑一次，"
            "生成与源文和冻结术语绑定的审核校验上下文。"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != _CONTEXT_SCHEMA_VERSION:
        raise ReviewImportError("review-context.json 版本不兼容，请重新运行 build。")
    expected = {
        "book_slug": book_slug,
        "source_sha256": source_sha256,
        "glossary_version": glossary_version,
    }
    changed = [name for name, value in expected.items() if payload.get(name) != value]
    if changed:
        raise ReviewImportError("审核校验上下文与当前构建身份不一致：" + ", ".join(changed))
    blocks = [
        Block(
            id=str(item["id"]),
            kind=str(item["kind"]),
            text=str(item["text"]),
            chapter=str(item["chapter"]),
            level=int(item.get("level", 0)),
        )
        for item in payload.get("blocks", [])
    ]
    senses = [Sense.from_dict(item) for item in payload.get("senses", [])]
    return Document(book_slug=book_slug, blocks=blocks), senses


def _manifest(path: Path) -> dict[str, tuple[str, ...]]:
    chunks: dict[str, tuple[str, ...]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewImportError(f"{path}:{number} 不是完整 JSONL：{exc}") from exc
        chunk_id = str(record.get("chunk_id", ""))
        block_ids = tuple(str(value) for value in record.get("block_ids") or ())
        if not chunk_id or not block_ids:
            raise ReviewImportError(f"{path}:{number} 缺少 chunk_id 或 block_ids")
        chunks[chunk_id] = block_ids
    return chunks


def _write_verification(path: Path, report: VerificationReport) -> None:
    path.write_text(report.to_markdown(), encoding="utf-8", newline="\n")


def apply_review(build_dir, input_path) -> ReviewImportReport:
    """Validate an entire review patch, then append it as one atomic transaction."""
    build = Path(build_dir)
    source = Path(input_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    items = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ReviewImportError("审核文件必须包含非空 chunks 列表。")

    manifest = _manifest(build / "chunks.jsonl")
    state = load_state(build / "state.json")
    current_translations, records = load_translations(build / "translations.jsonl")
    doc, senses = _load_review_context(
        build,
        book_slug=state.identity.book_slug,
        source_sha256=state.identity.source_sha256,
        glossary_version=state.identity.glossary_version,
    )
    blocks_by_id = {block.id: block for block in doc.blocks}
    reviewer = str(payload.get("reviewer") or "human")
    seen_chunks: set[str] = set()
    revised_block_ids: set[str] = set()
    prepared: list[tuple[str, dict[str, str], dict, int, list[str]]] = []
    prospective = dict(current_translations)

    for item in items:
        if not isinstance(item, dict):
            raise ReviewImportError("chunks 中每一项都必须是对象。")
        chunk_id = str(item.get("chunk_id") or "")
        if chunk_id in seen_chunks:
            raise ReviewImportError(f"审核文件重复出现 chunk：{chunk_id}")
        seen_chunks.add(chunk_id)
        expected = set(manifest.get(chunk_id) or ())
        if not expected or chunk_id not in state.chunks:
            raise ReviewImportError(f"审核文件包含未知 chunk：{chunk_id}")
        patch = item.get("translations")
        if not isinstance(patch, dict) or not patch:
            raise ReviewImportError(f"{chunk_id} 没有译文修订。")
        patch = {str(block_id): str(zh) for block_id, zh in patch.items()}
        extra = set(patch) - expected
        if extra:
            raise ReviewImportError(f"{chunk_id} 含计划外段落：" + ", ".join(sorted(extra)))

        previous = records.get(chunk_id) or {}
        merged = {
            str(block_id): str(zh) for block_id, zh in (previous.get("translations") or {}).items()
        }
        merged.update(patch)
        missing = expected - set(merged)
        if missing:
            raise ReviewImportError(f"{chunk_id} 仍缺少段落：" + ", ".join(sorted(missing)))
        ordered = {block_id: merged[block_id] for block_id in manifest[chunk_id]}
        usage = dict(previous.get("usage") or {})
        attempts = int(previous.get("attempts") or 0)
        prepared.append((chunk_id, ordered, usage, attempts, list(patch)))
        prospective.update(patch)
        revised_block_ids.update(patch)

    findings: list[Finding] = []
    for block_id in sorted(revised_block_ids):
        block = blocks_by_id.get(block_id)
        if block is None:
            raise ReviewImportError(f"审核校验上下文缺少段落：{block_id}")
        findings.extend(verify_block(block, prospective[block_id], senses=senses))

    consistency = verify_corpus_consistency(doc, prospective, severity=Severity.ERROR)
    findings.extend(consistency.findings)
    verification = VerificationReport("review-import", tuple(findings))
    verification_path = build / "review-verify-report.md"
    _write_verification(verification_path, verification)
    if not verification.ok:
        raise ReviewImportError(
            f"审核补丁未通过质量门禁：{verification.error_count} 个错误；"
            f"详见 {verification_path}。译文日志未修改。"
        )

    recorded_at = _now()
    journal_records = [
        {
            "chunk_id": chunk_id,
            "translations": ordered,
            "usage": usage,
            "attempts": attempts,
            "recorded_at": recorded_at,
            "review": {
                "reviewer": reviewer,
                "source": source.name,
                "revised_block_ids": patch_ids,
            },
        }
        for chunk_id, ordered, usage, attempts, patch_ids in prepared
    ]
    append_translation_batch(build / "translations.jsonl", journal_records)
    for chunk_id, _, usage, attempts, _ in prepared:
        state.mark_done(chunk_id, attempts, usage)
    save_state(state, build / "state.json")
    return ReviewImportReport(
        chunk_count=len(seen_chunks),
        revised_block_count=len(revised_block_ids),
        warning_count=verification.warning_count,
        verification_path=verification_path,
    )
