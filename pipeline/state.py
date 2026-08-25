"""可恢复构建状态与追加式译文日志。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "BuildIdentity",
    "ChunkProgress",
    "BuildState",
    "StaleBuildError",
    "open_state", "load_state",
    "save_state",
    "append_translation",
    "load_translations",
]

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StaleBuildError(RuntimeError):
    """现有缓存属于另一份源文件、术语快照、文风或模型。"""


@dataclass(frozen=True)
class BuildIdentity:
    book_slug: str
    source_sha256: str
    glossary_version: str
    style_version: str
    provider: str
    model: str
    chunk_plan: str
    prompt_version: str = ""


@dataclass
class ChunkProgress:
    block_ids: tuple[str, ...]
    status: str = "pending"  # pending | done | failed
    attempts: int = 0
    usage: dict = field(default_factory=dict)
    error: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "block_ids": list(self.block_ids),
            "status": self.status,
            "attempts": self.attempts,
            "usage": self.usage,
            "error": self.error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChunkProgress":
        return cls(
            block_ids=tuple(str(x) for x in data.get("block_ids", [])),
            status=str(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            usage=dict(data.get("usage") or {}),
            error=str(data.get("error", "")),
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass
class BuildState:
    identity: BuildIdentity
    chunks: dict[str, ChunkProgress]
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def pending_chunk_ids(self) -> tuple[str, ...]:
        return tuple(cid for cid, item in self.chunks.items() if item.status != "done")

    @property
    def done_count(self) -> int:
        return sum(item.status == "done" for item in self.chunks.values())

    def mark_done(self, chunk_id: str, attempts: int, usage: dict) -> None:
        item = self.chunks[chunk_id]
        item.status = "done"
        item.attempts = attempts
        item.usage = dict(usage or {})
        item.error = ""
        item.updated_at = _now()
        self.updated_at = item.updated_at

    def mark_failed(self, chunk_id: str, error: str, attempts: int = 0) -> None:
        item = self.chunks[chunk_id]
        item.status = "failed"
        item.attempts = attempts
        item.error = str(error)[:2000]
        item.updated_at = _now()
        self.updated_at = item.updated_at

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": asdict(self.identity),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "chunks": {cid: item.to_dict() for cid, item in self.chunks.items()},
        }


def _load(path: Path) -> BuildState:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if int(raw.get("schema_version", 0)) != SCHEMA_VERSION:
        raise StaleBuildError(
            f"{path} 的状态格式版本不兼容；请使用新的 build 目录。"
        )
    return BuildState(
        identity=BuildIdentity(**raw["identity"]),
        chunks={
            str(cid): ChunkProgress.from_dict(item)
            for cid, item in raw.get("chunks", {}).items()
        },
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
    )


def load_state(path) -> BuildState:
    """读取现有构建状态，供审核导入等构建后工具使用。"""
    return _load(Path(path))


def open_state(
    path,
    identity: BuildIdentity,
    chunk_blocks: dict[str, tuple[str, ...]],
) -> BuildState:
    """打开兼容状态；身份不一致时硬失败，绝不静默复用旧译文。"""
    p = Path(path)
    if p.exists():
        state = _load(p)
        if state.identity != identity:
            changed = [
                name
                for name in asdict(identity)
                if getattr(state.identity, name) != getattr(identity, name)
            ]
            raise StaleBuildError(
                "构建身份已变化，不能复用旧状态：" + ", ".join(changed)
            )
        expected = {cid: tuple(ids) for cid, ids in chunk_blocks.items()}
        actual = {cid: item.block_ids for cid, item in state.chunks.items()}
        if actual != expected:
            raise StaleBuildError("chunk 计划与 state.json 不一致。")
        return state

    state = BuildState(
        identity=identity,
        chunks={
            cid: ChunkProgress(block_ids=tuple(ids))
            for cid, ids in chunk_blocks.items()
        },
    )
    save_state(state, p)
    return state


def save_state(state: BuildState, path) -> None:
    """同目录原子替换，进程中断不会留下半截 JSON。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, p)


def append_translation(
    path,
    chunk_id: str,
    translations: dict[str, str],
    usage: dict,
    attempts: int,
    metadata: dict | None = None,
) -> None:
    """每个完成 chunk 追加一行；同 chunk 后写的记录覆盖先前记录。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "chunk_id": chunk_id,
        "translations": translations,
        "usage": usage or {},
        "attempts": attempts,
        "recorded_at": _now(),
    }
    if metadata:
        record["review"] = dict(metadata)
    with p.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_translations(path) -> tuple[dict[str, str], dict[str, dict]]:
    """读取追加日志，返回合并译文和每个 chunk 的最后一条记录。"""
    p = Path(path)
    if not p.exists():
        return {}, {}
    records: dict[str, dict] = {}
    for number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p}:{number} 不是完整 JSONL：{exc}") from exc
        chunk_id = str(record.get("chunk_id", ""))
        if not chunk_id:
            raise ValueError(f"{p}:{number} 缺少 chunk_id")
        records[chunk_id] = record
    translations: dict[str, str] = {}
    for record in records.values():
        translations.update({
            str(block_id): str(zh)
            for block_id, zh in (record.get("translations") or {}).items()
        })
    return translations, records
