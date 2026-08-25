"""bilingua 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .chunker import chunk_document, summarize
from .client import ProviderConfig
from .document import load
from .glossary import freeze, load_layers, load_snapshot
from .publish import PublicationBlockedError, publish_book
from .review import ReviewImportError, apply_review
from .scaffold import init_project, inspect_project
from .state import StaleBuildError
from .visuals import load_visual_annotations
from .workflow import build_book


class CLIError(RuntimeError):
    pass


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise CLIError(f"找不到环境配置：{path}")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _need(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise CLIError(f"环境配置缺少 {name}")
    return value


def _provider(args, env: dict[str, str]) -> ProviderConfig:
    return ProviderConfig(
        base_url=_need(env, "TRANSLATE_BASE_URL"),
        api_key=_need(env, "TRANSLATE_API_KEY"),
        protocol=env.get("TRANSLATE_PROTOCOL", "anthropic"),
        auth=env.get("TRANSLATE_AUTH", "bearer"),
        path_prefix=env.get("TRANSLATE_PATH_PREFIX", "/v1"),
        model=_need(env, "TRANSLATE_MODEL"),
        max_output_tokens=args.max_output_tokens,
        concurrency=args.concurrency,
        timeout=args.timeout,
        max_retries=args.max_retries,
    )


def _chapter_cards(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_dir():
        raise CLIError(f"章节卡目录不存在：{path}")
    return {
        item.stem: item.read_text(encoding="utf-8")
        for item in sorted(path.glob("*.md"))
    }


def _image_annotations(paths: list[Path] | None):
    merged = {}
    for path in paths or ():
        current = load_visual_annotations(path)
        duplicate = set(merged) & set(current)
        if duplicate:
            raise CLIError(
                f"视觉旁注图片路径重复（{path}）：" + ", ".join(sorted(duplicate))
            )
        merged.update(current)
    return merged or None


def _inspect(args) -> int:
    doc = load(
        args.source,
        book_slug=args.book_slug,
        chapter_levels=tuple(args.chapter_level or (1,)),
    )
    chunks = chunk_document(
        doc,
        target_words=args.target_words,
        context_blocks=args.context_blocks,
        max_output_tokens=args.max_output_tokens,
    )
    print(f"书籍：{doc.book_slug}")
    print(f"章节：{len(doc.chapter_slugs())}")
    print(f"段落：{len(doc.translatable_blocks())}")
    print(summarize(chunks))
    for warning in doc.warnings:
        print(f"警告：{warning}")
    return 0


def _build(args) -> int:
    env = _load_env(args.env_file)
    if args.snapshot is None and not args.allow_empty_glossary:
        raise CLIError(
            "正式构建必须用 --snapshot 绑定冻结术语快照；"
            "仅做技术测试时可显式传 --allow-empty-glossary。"
        )
    cfg = _provider(args, env)
    snapshot = load_snapshot(args.snapshot) if args.snapshot else None
    style_card = args.style_card.read_text(encoding="utf-8")
    report = build_book(
        source_path=args.source,
        build_root=args.build_root,
        cfg=cfg,
        style_card=style_card,
        snapshot=snapshot,
        book_slug=args.book_slug,
        chapter_cards=_chapter_cards(args.chapter_cards),
        target_words=args.target_words,
        context_blocks=args.context_blocks,
        chapters=tuple(args.chapter) if args.chapter else None,
        chapter_levels=tuple(args.chapter_level or (1,)),
        image_annotations=_image_annotations(args.image_annotations),
    )
    print(
        f"{report.book_slug}: chunks {report.completed_chunks}/{report.chunk_count} · "
        f"cost ${report.total_cost:.6f} · {'通过' if report.ok else '未通过'}"
    )
    print(f"构建目录：{report.build_dir}")

    if args.publish_to:
        if not report.ok:
            raise PublicationBlockedError("构建或校验未通过，不能发布。")
        chapter_paths = sorted((report.build_dir / "chapters").glob("*.md"))
        chapters = [
            (path.stem, path.read_text(encoding="utf-8"))
            for path in chapter_paths
        ]
        title = args.book_title or args.folder_name or report.book_slug
        folder = args.folder_name or report.book_slug
        images_dir = args.images_dir
        if images_dir is None:
            candidate = args.source.with_name(args.source.stem + "-images")
            images_dir = candidate if candidate.exists() else None
        publication = publish_book(
            args.publish_to,
            folder_name=folder,
            book_title=title,
            chapters=chapters,
            verification_reports=report.verification,
            metadata={
                "模型": cfg.model,
                "术语快照": snapshot.version if snapshot else "none",
                "构建目录": str(report.build_dir),
                "视觉旁注": (
                    ", ".join(str(path) for path in args.image_annotations)
                    if args.image_annotations else "none"
                ),
            },
            images_dir=images_dir,
        )
        print(f"已发布：{publication.destination}")
    return 0 if report.ok else 2


def _freeze_glossary(args) -> int:
    glossary = load_layers(args.root, domain=args.domain, book=args.book)
    snapshot = freeze(glossary, domain=args.domain, authors=args.author)
    snapshot.save(args.output)
    print(
        f"已冻结：{args.output} · version {snapshot.version} · "
        f"approved {len(snapshot.senses)}"
    )
    return 0


def _apply_review(args) -> int:
    report = apply_review(args.build_dir, args.input)
    print(
        f"已导入审核：{report.chunk_count} chunks · "
        f"{report.revised_block_count} 个段落"
    )
    return 0


def _init_project(args) -> int:
    written = init_project(args.destination)
    root = Path(args.destination).expanduser().resolve()
    print(f"已初始化项目：{root}")
    print(f"已创建：{len(written)} 个模板文件；未写入 API 密钥。")
    print("下一步：复制 .env.example 为 .env，填写配置后运行 bilingua doctor。")
    return 0


def _doctor(args) -> int:
    report = inspect_project(args.project_root, args.env_file)
    for check in report.checks:
        marker = "OK" if check.ok else "FAIL"
        print(f"[{marker}] {check.name}: {check.detail}")
    print("诊断不访问网络，也不会调用付费 API。")
    return 0 if report.ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bilingua",
        description="把 book2md Markdown 构建成段落级中英双语对照译本。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化一个不会覆盖已有文件的项目骨架")
    init.add_argument("destination", nargs="?", default=".", type=Path)
    init.set_defaults(handler=_init_project)

    doctor = sub.add_parser("doctor", help="零网络检查本地环境、配置与安全边界")
    doctor.add_argument("--project-root", type=Path, default=Path("."))
    doctor.add_argument("--env-file", type=Path, default=Path(".env"))
    doctor.set_defaults(handler=_doctor)

    inspect = sub.add_parser("inspect", help="只解析和分块，不调用 API")
    inspect.add_argument("source", type=Path)
    inspect.add_argument("--book-slug")
    inspect.add_argument(
        "--chapter-level",
        action="append",
        type=int,
        choices=range(1, 7),
        help="作为章节边界的 Markdown 标题级别；可重复传入，默认只用 H1。",
    )
    inspect.add_argument("--target-words", type=int, default=1500)
    inspect.add_argument("--context-blocks", type=int, default=2)
    inspect.add_argument("--max-output-tokens", type=int, default=8192)
    inspect.set_defaults(handler=_inspect)

    freeze_cmd = sub.add_parser("freeze-glossary", help="合并三层术语并冻结快照")
    freeze_cmd.add_argument("--root", type=Path, default=Path("glossary"))
    freeze_cmd.add_argument("--domain", required=True)
    freeze_cmd.add_argument("--book", required=True)
    freeze_cmd.add_argument(
        "--author",
        action="append",
        help="只纳入该作者及 author 为空的通用批准义项；可重复传入。",
    )
    freeze_cmd.add_argument("--output", type=Path, required=True)
    freeze_cmd.set_defaults(handler=_freeze_glossary)

    build = sub.add_parser("build", help="可恢复地翻译、校验和装配一本书")
    build.add_argument("source", type=Path)
    build.add_argument("--book-slug")
    build.add_argument(
        "--chapter-level",
        action="append",
        type=int,
        choices=range(1, 7),
        help="作为章节边界的 Markdown 标题级别；可重复传入，默认只用 H1。",
    )
    build.add_argument("--env-file", type=Path, default=Path(".env"))
    build.add_argument("--build-root", type=Path, default=Path("build"))
    build.add_argument(
        "--style-card",
        type=Path,
        default=Path(__file__).with_name("style-card.md"),
    )
    build.add_argument("--snapshot", type=Path)
    build.add_argument("--allow-empty-glossary", action="store_true")
    build.add_argument("--chapter-cards", type=Path)
    build.add_argument(
        "--chapter",
        action="append",
        help="只构建指定章节 slug；可重复传入。适合验收章节，默认构建全书。",
    )
    build.add_argument("--target-words", type=int, default=1500)
    build.add_argument("--context-blocks", type=int, default=2)
    build.add_argument("--max-output-tokens", type=int, default=8192)
    build.add_argument("--concurrency", type=int, default=2)
    build.add_argument("--timeout", type=int, default=180)
    build.add_argument("--max-retries", type=int, default=4)
    build.add_argument("--publish-to", type=Path)
    build.add_argument("--folder-name")
    build.add_argument("--book-title")
    build.add_argument("--images-dir", type=Path)
    build.add_argument(
        "--image-annotations",
        type=Path,
        action="append",
        help="可选 YAML 旁注；保留原图，并在匹配图片后插入可检索的中文标签对照。",
    )
    build.set_defaults(handler=_build)

    review = sub.add_parser("apply-review", help="把人工/Codex 修订安全并入构建日志")
    review.add_argument("--build-dir", type=Path, required=True)
    review.add_argument("--input", type=Path, required=True)
    review.set_defaults(handler=_apply_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        CLIError,
        StaleBuildError,
        ReviewImportError,
        PublicationBlockedError,
        FileExistsError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
