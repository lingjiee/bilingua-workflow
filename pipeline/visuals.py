"""Curated visual transcriptions for image-heavy source books.

The raster image remains immutable.  A sidecar supplies searchable Chinese
labels that assembly inserts immediately after the matching original image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

__all__ = [
    "VisualAnnotation",
    "image_target",
    "load_visual_annotations",
]

_IMAGE = re.compile(
    r"!\[[^\]]*\]\(\s*<?([^\s)>]+)>?"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)


def _cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def _normalise_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"视觉旁注图片路径必须是安全的相对路径：{value}")
    normalised = path.as_posix().lstrip("./")
    if not normalised:
        raise ValueError("视觉旁注图片路径不能为空。")
    return normalised


@dataclass(frozen=True)
class VisualAnnotation:
    path: str
    figure: str
    summary_zh: str
    labels: tuple[tuple[str, str], ...]
    confidence: str = "高"
    note_zh: str = ""

    def to_markdown(self) -> str:
        lines = [
            f"> [!note] 图中文字中英对照 · {_cell(self.figure)}",
            f"> 人工视觉转写 · 置信度：{_cell(self.confidence)} · 原始图片保持不变",
            ">",
            f"> **图意：** {_cell(self.summary_zh)}",
        ]
        if self.note_zh:
            lines.extend([">", f"> **读图说明：** {_cell(self.note_zh)}"])
        lines.extend([
            "",
            "| English label | 中文对照 |",
            "|---|---|",
        ])
        lines.extend(f"| {_cell(en)} | {_cell(zh)} |" for en, zh in self.labels)
        return "\n".join(lines)


def image_target(markdown: str) -> str | None:
    match = _IMAGE.search(markdown)
    return _normalise_path(match.group(1)) if match else None


def load_visual_annotations(path: str | Path) -> dict[str, VisualAnnotation]:
    source = Path(path)
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    raw = payload.get("annotations")
    if not isinstance(raw, Mapping):
        raise ValueError(f"视觉旁注缺少 annotations 映射：{source}")

    annotations: dict[str, VisualAnnotation] = {}
    for raw_path, item in raw.items():
        image_path = _normalise_path(str(raw_path))
        if not isinstance(item, Mapping):
            raise ValueError(f"视觉旁注条目必须是映射：{image_path}")
        figure = str(item.get("figure", "")).strip()
        summary = str(item.get("summary_zh", "")).strip()
        confidence = str(item.get("confidence", "高")).strip()
        note = str(item.get("note_zh", "")).strip()
        raw_labels = item.get("labels")
        if not figure or not summary or not isinstance(raw_labels, list) or not raw_labels:
            raise ValueError(
                f"视觉旁注必须包含 figure、summary_zh 和非空 labels：{image_path}"
            )
        if confidence not in {"高", "中", "低"}:
            raise ValueError(f"视觉旁注置信度只能是高/中/低：{image_path}")

        labels: list[tuple[str, str]] = []
        for index, label in enumerate(raw_labels, start=1):
            if not isinstance(label, Mapping):
                raise ValueError(f"视觉标签第 {index} 项不是映射：{image_path}")
            en = str(label.get("en", "")).strip()
            zh = str(label.get("zh", "")).strip()
            if not en or not zh:
                raise ValueError(f"视觉标签第 {index} 项缺少 en/zh：{image_path}")
            labels.append((en, zh))
        if image_path in annotations:
            raise ValueError(f"视觉旁注图片路径重复：{image_path}")
        annotations[image_path] = VisualAnnotation(
            path=image_path,
            figure=figure,
            summary_zh=summary,
            labels=tuple(labels),
            confidence=confidence,
            note_zh=note,
        )
    return annotations
