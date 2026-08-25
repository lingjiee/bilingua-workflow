from __future__ import annotations

import pytest

from pipeline.cli import CLIError, _image_annotations, _load_env, main
from pipeline.scaffold import init_project, inspect_project


def test_init_creates_safe_reusable_project(tmp_path, capsys):
    project = tmp_path / "translation-project"
    code = main(["init", str(project)])
    assert code == 0
    assert (project / ".env.example").is_file()
    assert not (project / ".env").exists()
    assert (project / "source/.gitkeep").is_file()
    assert (project / "glossary/domains/general.yaml").is_file()
    assert (project / "reviews/review.example.json").is_file()
    assert "未写入 API 密钥" in capsys.readouterr().out


def test_init_refuses_to_overwrite_existing_templates(tmp_path):
    project = tmp_path / "translation-project"
    init_project(project)
    with pytest.raises(FileExistsError, match="would overwrite"):
        init_project(project)


def test_doctor_is_local_and_redacts_api_key(tmp_path, capsys):
    project = tmp_path / "translation-project"
    init_project(project)
    (project / ".env").write_text(
        "TRANSLATE_BASE_URL=https://relay.example\n"
        "TRANSLATE_API_KEY=super-secret-value\n"
        "TRANSLATE_MODEL=example-model\n"
        "TRANSLATE_PROTOCOL=anthropic\n"
        "TRANSLATE_AUTH=bearer\n",
        encoding="utf-8",
    )
    report = inspect_project(project)
    assert report.ok
    code = main(["doctor", "--project-root", str(project)])
    output = capsys.readouterr().out
    assert code == 0
    assert "configured (redacted)" in output
    assert "super-secret-value" not in output
    assert "不会调用付费 API" in output


def test_doctor_reports_missing_required_configuration(tmp_path):
    project = tmp_path / "translation-project"
    init_project(project)
    report = inspect_project(project)
    assert not report.ok
    assert any(check.name == "TRANSLATE_API_KEY" and not check.ok for check in report.checks)


def test_inspect_never_needs_api_key(tmp_path, capsys):
    source = tmp_path / "book.md"
    source.write_text("# One\n\nPlain prose.\n", encoding="utf-8")
    code = main(["inspect", str(source), "--book-slug", "book"])
    output = capsys.readouterr().out
    assert code == 0
    assert "1 chunks" in output
    assert "段落：2" in output


def test_env_loader_keeps_key_available_without_printing_it(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRANSLATE_BASE_URL=https://relay.example\n"
        "TRANSLATE_API_KEY=super-secret\n",
        encoding="utf-8",
    )
    values = _load_env(env_file)
    assert values["TRANSLATE_API_KEY"] == "super-secret"
    assert "super-secret" not in capsys.readouterr().out


def test_build_requires_frozen_snapshot_unless_explicitly_overridden(tmp_path, capsys):
    source = tmp_path / "book.md"
    source.write_text("# One\n\nPlain prose.\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRANSLATE_BASE_URL=https://relay.example\n"
        "TRANSLATE_API_KEY=secret\n"
        "TRANSLATE_MODEL=test-model\n",
        encoding="utf-8",
    )
    code = main([
        "build", str(source), "--env-file", str(env_file),
        "--build-root", str(tmp_path / "build"),
    ])
    assert code == 2
    assert "冻结术语快照" in capsys.readouterr().err


def test_freeze_glossary_creates_versioned_snapshot(tmp_path, capsys):
    root = tmp_path / "glossary"
    (root / "domains").mkdir(parents=True)
    (root / "domains" / "jtbd.yaml").write_text(
        "senses:\n"
        "  - id: jtbd.job\n"
        "    surface: job\n"
        "    zh: 任务\n"
        "    status: approved\n",
        encoding="utf-8",
    )
    output = root / "snapshots" / "jtbd.lock"
    code = main([
        "freeze-glossary", "--root", str(root), "--domain", "jtbd",
        "--book", "cal", "--output", str(output),
    ])
    assert code == 0
    assert output.exists()
    assert "approved 1" in capsys.readouterr().out


def test_freeze_glossary_accepts_author_filter(tmp_path, capsys):
    root = tmp_path / "glossary"
    (root / "domains").mkdir(parents=True)
    (root / "domains" / "jtbd.yaml").write_text(
        "senses:\n"
        "  - id: klement\n"
        "    surface: job\n"
        "    zh: 待办任务\n"
        "    author: Author One\n"
        "    status: approved\n"
        "  - id: christensen\n"
        "    surface: job\n"
        "    zh: 任务\n"
        "    author: Author Two\n"
        "    status: approved\n",
        encoding="utf-8",
    )
    output = root / "snapshots" / "klement.lock"
    code = main([
        "freeze-glossary", "--root", str(root), "--domain", "jtbd",
        "--book", "sample", "--author", "Author One", "--output", str(output),
    ])
    assert code == 0
    from pipeline.glossary import load_snapshot
    snap = load_snapshot(output)
    assert [sense.id for sense in snap.senses] == ["klement"]
    assert snap.authors == ("Author One",)


def test_build_parser_accepts_repeatable_chapter_filter(monkeypatch, tmp_path):
    source = tmp_path / "book.md"
    source.write_text("# One\n\nPlain prose.\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRANSLATE_BASE_URL=https://relay.example\n"
        "TRANSLATE_API_KEY=secret\n"
        "TRANSLATE_MODEL=test-model\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_build_book(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after argument capture")

    monkeypatch.setattr("pipeline.cli.build_book", fake_build_book)
    try:
        main([
            "build", str(source), "--env-file", str(env_file),
            "--allow-empty-glossary", "--chapter", "one",
            "--chapter", "two", "--chapter-level", "1", "--chapter-level", "2",
        ])
    except RuntimeError as exc:
        assert str(exc) == "stop after argument capture"
    assert captured["chapters"] == ("one", "two")
    assert captured["chapter_levels"] == (1, 2)


def _visual_sidecar(path, image):
    path.write_text(
        f"""
annotations:
  {image}:
    figure: 图 1
    summary_zh: 示例。
    labels:
      - en: Job
        zh: 任务
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_repeatable_visual_sidecars_are_merged(tmp_path):
    first = _visual_sidecar(tmp_path / "first.yaml", "images/one.jpg")
    second = _visual_sidecar(tmp_path / "second.yaml", "images/two.jpg")
    merged = _image_annotations([first, second])
    assert set(merged) == {"images/one.jpg", "images/two.jpg"}


def test_repeatable_visual_sidecars_reject_duplicate_paths(tmp_path):
    first = _visual_sidecar(tmp_path / "first.yaml", "images/one.jpg")
    second = _visual_sidecar(tmp_path / "second.yaml", "images/one.jpg")
    with pytest.raises(CLIError, match="路径重复"):
        _image_annotations([first, second])
