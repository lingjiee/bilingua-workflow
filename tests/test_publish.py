from __future__ import annotations

import pytest

from pipeline.publish import PublicationBlockedError, publish_book
from pipeline.verify import Finding, Severity, VerificationReport


def clean_report(chapter="one"):
    return VerificationReport(chapter=chapter)


def test_publishes_index_and_chapters_atomically(tmp_path):
    chapters = [
        ("one", "# 第一章 · Chapter One\n\n> English\n\n中文。\n"),
        ("two", "# 第二章 · Chapter Two\n\n> More\n\n更多。\n"),
    ]
    report = publish_book(
        tmp_path,
        folder_name="测试译本",
        book_title="测试译本",
        chapters=chapters,
        verification_reports=[clean_report("one"), clean_report("two")],
        metadata={"模型": "model-x"},
    )
    assert report.destination == tmp_path / "测试译本"
    assert (report.destination / "00 索引.md").exists()
    assert (report.destination / "01 第一章.md").exists()
    index = (report.destination / "00 索引.md").read_text(encoding="utf-8")
    assert "[[01 第一章|第一章]]" in index
    assert "model-x" in index
    assert not list(tmp_path.glob(".bilingua-*.tmp"))


def test_failed_verification_never_touches_vault(tmp_path):
    failed = VerificationReport(
        chapter="one",
        findings=(Finding("p1", "empty", "empty", Severity.ERROR),),
    )
    with pytest.raises(PublicationBlockedError):
        publish_book(tmp_path, "book", "Book", [("one", "# One\n")], [failed])
    assert list(tmp_path.iterdir()) == []


def test_existing_book_is_never_overwritten(tmp_path):
    existing = tmp_path / "book"
    existing.mkdir()
    marker = existing / "my-note.md"
    marker.write_text("keep me", encoding="utf-8")
    with pytest.raises(FileExistsError):
        publish_book(tmp_path, "book", "Book", [("one", "# One\n")], [clean_report()])
    assert marker.read_text(encoding="utf-8") == "keep me"


def test_folder_and_chapter_names_cannot_escape_vault(tmp_path):
    report = publish_book(
        tmp_path,
        "../bad/name",
        "Book",
        [("one", "# ../Bad:Title?\n")],
        [clean_report()],
    )
    assert report.destination.parent == tmp_path.resolve()
    assert ".." not in report.destination.name
    assert all("/" not in name and ":" not in name for name in report.chapter_files)


def test_optional_images_are_copied(tmp_path):
    images = tmp_path / "source-images"
    images.mkdir()
    (images / "figure.jpg").write_bytes(b"jpeg")
    vault = tmp_path / "vault"
    report = publish_book(
        vault,
        "book",
        "Book",
        [("one", "# One\n")],
        [clean_report()],
        images_dir=images,
    )
    assert (report.destination / "images" / "figure.jpg").read_bytes() == b"jpeg"
