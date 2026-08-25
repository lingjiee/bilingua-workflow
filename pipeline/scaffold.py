"""Project scaffolding and local, zero-cost environment diagnostics."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DoctorCheck", "DoctorReport", "init_project", "inspect_project"]


ENV_EXAMPLE = """# Copy this file to .env. Never commit the real .env file.
TRANSLATE_BASE_URL=https://api.example.com
TRANSLATE_API_KEY=replace-with-your-secret-key
TRANSLATE_MODEL=replace-with-a-model-id
TRANSLATE_PROTOCOL=anthropic
TRANSLATE_AUTH=bearer
TRANSLATE_PATH_PREFIX=/v1
"""

PROJECT_GITIGNORE = """.env
.env.*
!.env.example
.venv/
.pytest_cache/
__pycache__/
*.py[cod]
build/
source/*
!source/.gitkeep
dist/
*.egg-info/
"""

STYLE_CARD = """# Translation style card

- Accuracy comes before elegance: do not add, omit, or embellish.
- Use natural Simplified Chinese while preserving the author's reasoning and tone.
- Follow the frozen glossary snapshot bound to the build.
- Preserve numbers, links, footnotes, and Markdown structure.
- Keep names and established product or organization names consistent.
- Return only the requested paragraph IDs and Chinese translations.
"""

PROJECT_README = """# Bilingua translation project

This directory contains local inputs and build state for one or more books.
The source material, translations, API key, and build output are intentionally
excluded from Git by default.

Start here:

1. Copy `.env.example` to `.env` and fill in your provider configuration.
2. Put a legally obtained Markdown source in `source/`.
3. Run `bilingua doctor` and `bilingua inspect source/<book>.md`.
4. Review glossary evidence, freeze a snapshot, and build one acceptance chapter.
5. Only after acceptance, run the full resumable build and publish the verified result.

See the main Bilingua repository documentation for the complete workflow.
"""

EMPTY_GLOSSARY = """# Only approved senses enter a frozen snapshot.
senses: []
"""

DOMAIN_EXAMPLE = """senses:
  - id: example.customer
    surface: customer
    zh: 客户
    parent: example.person
    definition_zh: 购买或使用产品与服务的人或组织。
    evidence:
      - sample/chapter-1/<replace-with-real-block-id>
    decision: 示例义项；正式项目必须以原书证据复核。
    status: candidate
"""

BOOK_EXAMPLE = """# Book-specific senses override neither evidence nor review.
senses: []
"""

REVIEW_EXAMPLE = """{
  "reviewer": "name or review process",
  "chunks": [
    {
      "chunk_id": "chapter-1/c01",
      "translations": {
        "sample/chapter-1/<block-id>": "经人工复核的中文译文。"
      }
    }
  ]
}
"""

VISUAL_EXAMPLE = """annotations:
  images/example.png:
    figure: Figure 1
    summary_zh: 说明图表表达的核心关系。
    confidence: 高
    labels:
      - en: Input
        zh: 输入
      - en: Output
        zh: 输出
"""


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _project_files() -> dict[str, str]:
    return {
        ".env.example": ENV_EXAMPLE,
        ".gitignore": PROJECT_GITIGNORE,
        "README.md": PROJECT_README,
        "style-card.md": STYLE_CARD,
        "source/.gitkeep": "",
        "glossary/global.yaml": EMPTY_GLOSSARY,
        "glossary/candidates.yaml": EMPTY_GLOSSARY,
        "glossary/domains/general.yaml": DOMAIN_EXAMPLE,
        "glossary/books/sample.yaml": BOOK_EXAMPLE,
        "reviews/review.example.json": REVIEW_EXAMPLE,
        "visuals/annotations.example.yaml": VISUAL_EXAMPLE,
    }


def init_project(destination: str | Path) -> tuple[Path, ...]:
    """Create a safe local project skeleton; never overwrite existing files."""
    root = Path(destination).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise FileExistsError(f"project destination is not a directory: {root}")
    conflicts = [root / relative for relative in _project_files() if (root / relative).exists()]
    if conflicts:
        rendered = ", ".join(str(path) for path in conflicts[:5])
        raise FileExistsError(f"project initialization would overwrite: {rendered}")
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for relative, content in _project_files().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    (root / "build").mkdir(exist_ok=True)
    return tuple(written)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def inspect_project(root: str | Path, env_file: str | Path = ".env") -> DoctorReport:
    """Inspect local prerequisites without making network or paid API calls."""
    project = Path(root).expanduser().resolve()
    env_path = Path(env_file).expanduser()
    if not env_path.is_absolute():
        env_path = project / env_path
    values = _read_env(env_path)
    checks = [
        DoctorCheck(
            "python",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ),
        DoctorCheck("project", project.is_dir(), str(project)),
        DoctorCheck("environment file", env_path.is_file(), str(env_path)),
    ]
    for key in ("TRANSLATE_BASE_URL", "TRANSLATE_API_KEY", "TRANSLATE_MODEL"):
        value = values.get(key, "").strip()
        detail = "configured" if value else "missing"
        if key == "TRANSLATE_API_KEY" and value:
            detail = "configured (redacted)"
        checks.append(DoctorCheck(key, bool(value), detail))
    protocol = values.get("TRANSLATE_PROTOCOL", "anthropic")
    auth = values.get("TRANSLATE_AUTH", "bearer")
    checks.extend(
        [
            DoctorCheck("protocol", protocol in {"anthropic", "openai"}, protocol),
            DoctorCheck("auth", auth in {"bearer", "x-api-key"}, auth),
            DoctorCheck("source directory", (project / "source").is_dir(), str(project / "source")),
            DoctorCheck(
                "glossary directory", (project / "glossary").is_dir(), str(project / "glossary")
            ),
            DoctorCheck(
                "style card", (project / "style-card.md").is_file(), str(project / "style-card.md")
            ),
        ]
    )
    gitignore = project / ".gitignore"
    ignored = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    checks.append(DoctorCheck("secret ignore rule", ".env" in ignored, str(gitignore)))
    return DoctorReport(checks=tuple(checks))
