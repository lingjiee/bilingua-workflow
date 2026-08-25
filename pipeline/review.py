"""把人工或 Codex 复核结果安全地并入追加式译文日志。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .state import append_translation, load_state, load_translations, save_state

__all__ = ["ReviewImportError", "ReviewImportReport", "apply_review"]


class ReviewImportError(ValueError):
    """审核补丁与当前 chunk 计划不一致。"""


@dataclass(frozen=True)
class ReviewImportReport:
    chunk_count: int
    revised_block_count: int


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


def apply_review(build_dir, input_path) -> ReviewImportReport:
    """导入审核补丁。

    输入是 ``{"reviewer": "...", "chunks": [{"chunk_id": "...",
    "translations": {"block-id": "修订译文"}}]}``。已有 chunk 可只给改动
    段落；失败/缺失 chunk 必须补齐全部段落。写入前严格核对 ID 集合。
    """
    build = Path(build_dir)
    source = Path(input_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    items = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ReviewImportError("审核文件必须包含非空 chunks 列表。")

    manifest = _manifest(build / "chunks.jsonl")
    state = load_state(build / "state.json")
    _, records = load_translations(build / "translations.jsonl")
    reviewer = str(payload.get("reviewer") or "human")
    seen_chunks: set[str] = set()
    revised_blocks = 0

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
            raise ReviewImportError(
                f"{chunk_id} 含计划外段落：" + ", ".join(sorted(extra))
            )

        previous = records.get(chunk_id) or {}
        merged = {
            str(block_id): str(zh)
            for block_id, zh in (previous.get("translations") or {}).items()
        }
        merged.update(patch)
        missing = expected - set(merged)
        if missing:
            raise ReviewImportError(
                f"{chunk_id} 仍缺少段落：" + ", ".join(sorted(missing))
            )
        ordered = {block_id: merged[block_id] for block_id in manifest[chunk_id]}
        usage = dict(previous.get("usage") or {})
        attempts = int(previous.get("attempts") or 0)
        append_translation(
            build / "translations.jsonl",
            chunk_id,
            ordered,
            usage,
            attempts,
            metadata={
                "reviewer": reviewer,
                "source": source.name,
                "revised_block_ids": list(patch),
            },
        )
        state.mark_done(chunk_id, attempts, usage)
        records[chunk_id] = {
            "translations": ordered,
            "usage": usage,
            "attempts": attempts,
        }
        revised_blocks += len(patch)

    save_state(state, build / "state.json")
    return ReviewImportReport(len(seen_chunks), revised_blocks)
