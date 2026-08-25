from __future__ import annotations

import json

import pytest

from pipeline.client import ChunkResult, ProviderConfig
from pipeline.state import StaleBuildError
from pipeline.workflow import (
    BuildAlreadyRunning,
    _exclusive_build_lock,
    build_book,
)

CFG = ProviderConfig(
    base_url="https://relay.example",
    api_key="secret",
    model="model-x",
    concurrency=2,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def translate_chunk(self, chunk, style_card, chapter_card, senses):
        self.calls.append(chunk.id)
        translations = {}
        for block in chunk.blocks:
            translations[block.id] = "第一章" if block.kind == "heading" else "这里是中文译文。"
        return ChunkResult(chunk.id, translations, {"cost": 0.01})


class MustNotCall:
    def translate_chunk(self, *args, **kwargs):
        raise AssertionError("已完成构建不应再次请求 API")


def source_file(tmp_path):
    path = tmp_path / "book.md"
    path.write_text("# Chapter One\n\nPlain prose here.\n", encoding="utf-8")
    return path


def test_build_runs_translate_verify_assemble_and_records_state(tmp_path):
    client = FakeClient()
    report = build_book(
        source_file(tmp_path),
        tmp_path / "build",
        CFG,
        "style-v1",
        book_slug="book",
        client=client,
        target_words=1000,
    )
    assert report.ok
    assert report.completed_chunks == report.chunk_count == 1
    assert client.calls == ["chapter-one/c01"]
    assert (report.build_dir / "state.json").exists()
    assert (report.build_dir / "translations.jsonl").exists()
    assert (report.build_dir / "verify-report.md").exists()
    assert (report.build_dir / "artifact-verify-report.md").exists()
    assert (report.build_dir / "chapters" / "chapter-one.md").exists()


def test_completed_build_resumes_without_calling_api(tmp_path):
    source = source_file(tmp_path)
    build_root = tmp_path / "build"
    build_book(
        source,
        build_root,
        CFG,
        "style-v1",
        book_slug="book",
        client=FakeClient(),
        target_words=1000,
    )
    report = build_book(
        source,
        build_root,
        CFG,
        "style-v1",
        book_slug="book",
        client=MustNotCall(),
        target_words=1000,
    )
    assert report.ok


def test_complete_journal_repairs_pending_state_without_duplicate_api_call(tmp_path):
    source = source_file(tmp_path)
    build_root = tmp_path / "build"
    first = build_book(
        source,
        build_root,
        CFG,
        "style-v1",
        book_slug="book",
        client=FakeClient(),
        target_words=1000,
    )
    state_path = first.build_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["chunks"]["chapter-one/c01"].update(
        {"status": "pending", "attempts": 0, "usage": {}, "error": ""}
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    resumed = build_book(
        source,
        build_root,
        CFG,
        "style-v1",
        book_slug="book",
        client=MustNotCall(),
        target_words=1000,
    )

    assert resumed.ok
    assert resumed.completed_chunks == 1
    healed = json.loads(state_path.read_text(encoding="utf-8"))
    assert healed["chunks"]["chapter-one/c01"]["status"] == "done"


def test_second_process_is_refused_while_build_root_is_locked(tmp_path):
    build_root = tmp_path / "build"
    with _exclusive_build_lock(build_root / ".bilingua-build.lock"):
        with pytest.raises(BuildAlreadyRunning, match="已有构建进程"):
            build_book(
                source_file(tmp_path),
                build_root,
                CFG,
                "style-v1",
                book_slug="book",
                client=FakeClient(),
                target_words=1000,
            )


def test_changed_style_refuses_to_mix_with_old_results(tmp_path):
    source = source_file(tmp_path)
    build_root = tmp_path / "build"
    build_book(
        source,
        build_root,
        CFG,
        "style-v1",
        book_slug="book",
        client=FakeClient(),
        target_words=1000,
    )
    with pytest.raises(StaleBuildError, match="style_version"):
        build_book(
            source,
            build_root,
            CFG,
            "style-v2",
            book_slug="book",
            client=FakeClient(),
            target_words=1000,
        )


def test_failed_chunk_blocks_assembly_and_remains_retryable(tmp_path):
    class FailingClient:
        def translate_chunk(self, *args, **kwargs):
            raise RuntimeError("upstream down")

    report = build_book(
        source_file(tmp_path),
        tmp_path / "build",
        CFG,
        "style-v1",
        book_slug="book",
        client=FailingClient(),
        target_words=1000,
    )
    assert not report.ok
    assert report.completed_chunks == 0
    assert not (report.build_dir / "chapters").exists()


def test_build_can_select_one_acceptance_chapter(tmp_path):
    source = tmp_path / "book.md"
    source.write_text(
        "# Chapter One\n\nFirst chapter prose.\n\n# Chapter Two\n\nSecond chapter prose.\n",
        encoding="utf-8",
    )
    client = FakeClient()
    report = build_book(
        source,
        tmp_path / "build",
        CFG,
        "style-v1",
        book_slug="book",
        client=client,
        target_words=1000,
        chapters=("chapter-two",),
    )
    assert report.ok
    assert client.calls == ["chapter-two/c01"]
    assert not (report.build_dir / "chapters" / "chapter-one.md").exists()
    assert (report.build_dir / "chapters" / "chapter-two.md").exists()
