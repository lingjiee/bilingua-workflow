from pathlib import Path

import pytest

from pipeline.visuals import image_target, load_visual_annotations


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_and_render_visual_annotation(tmp_path):
    path = _write(
        tmp_path / "visuals.yaml",
        """
annotations:
  images/figure.png:
    figure: 图 2.2
    summary_zh: JTBD 生态系统。
    confidence: 高
    note_zh: 从左向右阅读。
    labels:
      - en: Job Performer
        zh: 任务执行者
      - en: When/Where
        zh: 何时/何地
""".lstrip(),
    )
    annotations = load_visual_annotations(path)
    item = annotations["images/figure.png"]
    rendered = item.to_markdown()
    assert item.labels == (("Job Performer", "任务执行者"), ("When/Where", "何时/何地"))
    assert "人工视觉转写 · 置信度：高" in rendered
    assert "| Job Performer | 任务执行者 |" in rendered
    assert "从左向右阅读" in rendered


def test_image_target_normalises_relative_path():
    assert image_target("![Figure](./images/figure.png)") == "images/figure.png"
    assert image_target("ordinary text") is None


@pytest.mark.parametrize("unsafe", ["../secret.png", "/absolute.png"])
def test_visual_annotation_rejects_unsafe_path(tmp_path, unsafe):
    path = _write(
        tmp_path / "visuals.yaml",
        f"""
annotations:
  {unsafe}:
    figure: 图 X
    summary_zh: 不安全路径。
    labels:
      - en: A
        zh: 甲
""".lstrip(),
    )
    with pytest.raises(ValueError, match="安全的相对路径"):
        load_visual_annotations(path)


def test_visual_annotation_requires_nonempty_labels(tmp_path):
    path = _write(
        tmp_path / "visuals.yaml",
        """
annotations:
  images/figure.png:
    figure: 图 X
    summary_zh: 空标签。
    labels: []
""".lstrip(),
    )
    with pytest.raises(ValueError, match="非空 labels"):
        load_visual_annotations(path)
