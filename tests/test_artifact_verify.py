from pipeline.artifact_verify import verify_artifact
from pipeline.assemble import assemble_chapter
from pipeline.document import parse_markdown
from pipeline.visuals import VisualAnnotation


def _fixture(translation="普通译文。"):
    doc = parse_markdown("# Chapter One\n\nEnglish prose.\n", book_slug="t")
    translations = {
        b.id: ("第一章" if b.kind == "heading" else translation)
        for b in doc.translatable_blocks()
    }
    chapter = doc.chapter_slugs()[0]
    markdown, _ = assemble_chapter(doc, chapter, translations)
    return doc, chapter, translations, markdown


def test_valid_artifact_passes():
    doc, chapter, translations, markdown = _fixture()
    assert verify_artifact(doc, chapter, translations, markdown).ok


def test_translation_rendered_as_quote_is_caught():
    doc, chapter, translations, markdown = _fixture()
    broken = markdown.replace("\n普通译文。\n", "\n> 普通译文。\n")
    rules = {f.rule for f in verify_artifact(doc, chapter, translations, broken).findings}
    assert "artifact.translation_quote" in rules or "artifact.pairing" in rules


def test_traditional_character_is_caught():
    doc, chapter, translations, markdown = _fixture("毒販出现。")
    rules = {f.rule for f in verify_artifact(doc, chapter, translations, markdown).findings}
    assert "artifact.traditional" in rules


def test_missing_pair_is_caught():
    doc, chapter, translations, markdown = _fixture()
    broken = markdown.replace("普通译文。", "")
    rules = {f.rule for f in verify_artifact(doc, chapter, translations, broken).findings}
    assert "artifact.pairing" in rules


def test_curated_visual_annotation_does_not_break_source_pairing():
    source = "# Chapter One\n\nEnglish prose.\n\n![Figure](images/f1.png)\n"
    doc = parse_markdown(source, book_slug="t")
    translations = {
        b.id: ("第一章" if b.kind == "heading" else "普通译文。")
        for b in doc.translatable_blocks()
    }
    chapter = doc.chapter_slugs()[0]
    annotation = VisualAnnotation(
        path="images/f1.png",
        figure="图 1",
        summary_zh="示意图。",
        labels=(("Job", "任务"),),
    )
    markdown, _ = assemble_chapter(
        doc,
        chapter,
        translations,
        image_annotations={annotation.path: annotation},
    )
    assert verify_artifact(doc, chapter, translations, markdown).ok


def test_inline_heading_image_is_valid_but_broken_literal_is_rejected():
    source = (
        "# Book\n\n"
        "### **PLAY** ![Images](images/arrow1.jpg) **Conduct Interviews**\n"
    )
    doc = parse_markdown(source, book_slug="t")
    translations = {
        block.id: (
            "**实战方法** ![Images](images/arrow1.jpg) **开展访谈**"
            if block.level == 3 else "书名"
        )
        for block in doc.translatable_blocks()
    }
    chapter = "book"
    markdown, _ = assemble_chapter(doc, chapter, translations)
    assert verify_artifact(doc, chapter, translations, markdown).ok

    broken = markdown.replace("![Images](images/arrow1.jpg)", "!Images")
    rules = {
        finding.rule
        for finding in verify_artifact(doc, chapter, translations, broken).findings
    }
    assert "artifact.malformed_image" in rules


def test_raw_svg_wrapper_is_not_duplicated_as_a_translation():
    source = "# Book\n\n<svg viewBox=\"0 0 10 10\">\n\n</svg>\n"
    doc = parse_markdown(source, book_slug="t")
    translations = {
        block.id: ("书名" if block.kind == "heading" else block.text)
        for block in doc.translatable_blocks()
    }
    chapter = "book"
    markdown, _ = assemble_chapter(doc, chapter, translations)
    assert verify_artifact(doc, chapter, translations, markdown).ok
    assert markdown.count("<svg viewBox=\"0 0 10 10\">") == 1
    assert markdown.count("</svg>") == 1
