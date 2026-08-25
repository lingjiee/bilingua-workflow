from __future__ import annotations

import json

import pytest

from pipeline.client import ProviderConfig
from pipeline.review import ReviewImportError, apply_review
from pipeline.state import load_translations
from pipeline.workflow import build_book


CFG = ProviderConfig(
    base_url="https://relay.example",
    api_key="secret",
    model="model-x",
    concurrency=1,
)


class FailingClient:
    def translate_chunk(self, *args, **kwargs):
        raise RuntimeError("upstream down")


class MustNotCall:
    def translate_chunk(self, *args, **kwargs):
        raise AssertionError("审核已补齐的 chunk 不应调用 API")


def _failed_build(tmp_path):
    source = tmp_path / "book.md"
    source.write_text("# One\n\nPlain prose.\n", encoding="utf-8")
    report = build_book(
        source,
        tmp_path / "build",
        CFG,
        "style-v1",
        book_slug="book",
        client=FailingClient(),
        target_words=1000,
    )
    manifest = json.loads(
        (report.build_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    return source, report, manifest


def test_review_can_complete_failed_chunk_and_resume_without_api(tmp_path):
    source, report, manifest = _failed_build(tmp_path)
    translations = {
        block_id: "第一章" if index == 0 else "这里是中文译文。"
        for index, block_id in enumerate(manifest["block_ids"])
    }
    patch = tmp_path / "review.json"
    patch.write_text(
        json.dumps({
            "reviewer": "Codex",
            "chunks": [{
                "chunk_id": manifest["chunk_id"],
                "translations": translations,
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    imported = apply_review(report.build_dir, patch)
    assert imported.chunk_count == 1
    assert imported.revised_block_count == 2
    resumed = build_book(
        source,
        tmp_path / "build",
        CFG,
        "style-v1",
        book_slug="book",
        client=MustNotCall(),
        target_words=1000,
    )
    assert resumed.ok
    _, records = load_translations(report.build_dir / "translations.jsonl")
    assert records[manifest["chunk_id"]]["review"]["reviewer"] == "Codex"


def test_review_rejects_incomplete_failed_chunk(tmp_path):
    _, report, manifest = _failed_build(tmp_path)
    patch = tmp_path / "review.json"
    patch.write_text(
        json.dumps({
            "chunks": [{
                "chunk_id": manifest["chunk_id"],
                "translations": {manifest["block_ids"][0]: "第一章"},
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ReviewImportError, match="仍缺少段落"):
        apply_review(report.build_dir, patch)
