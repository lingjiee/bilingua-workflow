# Changelog

All notable changes are documented here. Versions follow Semantic Versioning while the project remains pre-1.0.

## [0.3.1] - 2026-08-30

### Added

- Apache-2.0 `LICENSE` and SPDX license metadata in `pyproject.toml`; the license file now ships inside the built distributions.
- `.mailmap` normalising the author identity across a GitHub username change (display only; no history rewrite, so commit SHAs, tags and releases are unaffected).

### Changed

- First public release. `docs/REPOSITORY_BOUNDARIES.zh-CN.md` now states the repository's own public status and scopes the private-by-default rule to derived translation projects.
- README maturity note updated from the stale `0.2.0` to `0.3.1`.
- Test docstrings no longer name the specific real books used during development.

## [0.3.0] - 2026-08-25

### Added

- Identity-bound `review-context.json` for validating human and AI review patches.
- Atomic multi-chunk review imports with a review verification report.
- English `surface_aliases` and explicit `forbidden_zh` glossary fields.
- Detection of Chinese substitutions embedded inside preserved English terms.
- Duplicate-source translation consistency and corpus translation-split reports.
- Ruff lint/format checks and visible test coverage in CI.

### Changed

- Frozen terms are included in translation-split analysis; single outliers and up to five variants are retained for human review.
- CI now cancels superseded runs, times out after ten minutes, and builds distributions once instead of on every matrix leg.
- Development test count increased from 316 to 328.

### Fixed

- Review patches can no longer write translations that fail block-level quality gates.
- A real review regression that changed `Customer Job Theory` to a mixed-language term was corrected through an append-only review record.

## [0.2.0] - 2026-08-25

### Added

- First Git-managed, reusable repository release.
- `bilingua init` safe project scaffolding command.
- `bilingua doctor` zero-network local diagnostics with key redaction.
- Professional workflow, architecture, repository-boundary, security, and contribution documentation.
- Synthetic example source, glossary, review, and visual sidecar.
- GitHub Actions CI and Dependabot configuration.
- Final-artifact checks for malformed inline image syntax and raw SVG preservation.

### Changed

- Provider model ID is now explicit configuration instead of a relay-specific default.
- Repository boundaries exclude books, translations, build caches, reviews, reports, and project-specific automation.

## [0.1.0] - 2026-08-24

- Initial local pipeline: parsing, chunking, glossary freezing, provider client, resumable state, verification, assembly, reviews, visuals, and guarded publishing.
