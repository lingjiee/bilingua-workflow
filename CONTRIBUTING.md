# Contributing

## Development setup

```bash
git clone https://github.com/lingjiee/bilingua-workflow.git
cd bilingua-workflow
uv sync --locked --extra dev
uv run pytest
```

Tests must not access a real API. Use injected fake transports for client and workflow tests.

## Change workflow

1. Create a focused branch: `feat/...`, `fix/...`, `docs/...`, or `test/...`.
2. Add or update tests before changing behavior.
3. Run the complete suite and `uv build`.
4. Inspect the staged file list for secrets, copyrighted source text, translations, local paths, and build output.
5. Use a concise imperative commit message, preferably Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
6. Open a pull request describing behavior, evidence, migration impact, and verification.

## Compatibility rules

- Python 3.11 and 3.12 are CI-supported.
- Build identity fields are persistence contracts. Changes require migration analysis and regression tests.
- Never weaken a verification rule only to silence one project. Add a narrow, evidence-backed exception.
- Review logs are append-only. Do not design features that silently rewrite history.
- Provider-specific behavior belongs in configuration or a small adapter, not in the core workflow.

## Pull request checklist

- [ ] Scope is generic and contains no book-specific copyrighted content.
- [ ] Tests cover success and failure behavior.
- [ ] `uv run pytest` passes.
- [ ] `uv build` succeeds.
- [ ] Documentation and changelog are updated when user behavior changes.
- [ ] No secrets, real `.env`, source books, translations, or local absolute paths are staged.
