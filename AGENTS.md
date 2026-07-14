# AGENTS.md

Compact guide for OpenCode sessions working in this repo. Read alongside `README.md`; this file only contains the non-obvious bits.

## Toolchain
- Python **>=3.11** (uses `tomllib`, `from typing import ...`). `.python-version` pins 3.11; CI matrix tests 3.11 + 3.12.
- Package manager is **uv** (lockfile: `uv.lock`). Do not use plain `pip` in commands if `uv` is expected.

## Dev commands
- Editable install with extras (needed for full test run): `uv pip install -e ".[all]"`. `[all]` = `anthropic,google,mcp,documents,graph-rag`. CI additionally does `uv pip install --system pytest pytest-cov`.
- Lint (matches CI exactly): `ruff check hyperextract` and `ruff format --check hyperextract`. Auto-fix: `ruff format hyperextract`.
- Tests: `pytest` (with optional `--cov=hyperextract`).
- Single file: `pytest tests/types/test_hypergraph_merge.py`. Single test: `pytest path/to/test_file.py::TestClass::test_name`.
- Integration tests only: `pytest -m integration -v --tb=short` (requires a real `OPENAI_API_KEY`).

## Test-environment gotcha (most likely to bite)
- `tests/conftest.py` auto-loads `.env` at collection time. If your local `.env` has a real `OPENAI_API_KEY`, **all** `pytest` runs switch off the mocks in `tests/mocks.py` and hit the real OpenAI API (slow, costs money, non-deterministic).
- To force deterministic mock tests locally, override the env: `OPENAI_API_KEY="" pytest`. CI's `test.yml` sets `OPENAI_API_KEY: ""` for exactly this reason.
- Integration tests in `tests/integration/` carry `pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"))]`. They run via `integration.yml` nightly using `secrets.OPENAI_API_KEY` under the `integration-tests` environment; they will be skipped without the key.

## Lint scope
- CI runs `ruff` over **`hyperextract` only** — never over `tests/`, `docs_hooks.py`, or `examples/`. Don't be surprised that tests can use slightly different style. `[tool.ruff.lint]` ignores `E731`.

## Architecture (not obvious from filenames)
- Three-layer design:
  - `hyperextract/types/` — Auto-* primitives: `AutoModel`, `AutoList`, `AutoSet`, `AutoGraph`, `AutoHypergraph`, `AutoTemporalGraph`, `AutoSpatialGraph`, `AutoSpatioTemporalGraph`. Start at `types/base.py`.
  - `hyperextract/methods/` — extraction algorithms under `typical/` and `rag/`; registry at `methods/registry.py`.
  - `hyperextract/templates/` — YAML presets in `presets/<domain>/<name>.yaml`, referenced by id `domain/name` (e.g., `general/biography_graph`). Template format spec lives in `templates/DESIGN_GUIDE.md`.
- Package is named **`hyperextract`** (singular) everywhere: distribution, import root, and PyPI project. CLI binary is `he`; MCP server binary is `he-mcp`.
- Entry points (`pyproject.toml [project.scripts]`):
  - `he` → `hyperextract.cli:app` (Typer app; real module is `hyperextract/cli/cli.py`, subcommands in `cli/commands/`).
  - `he-mcp` → `hyperextract.mcp_server:main` (stdio MCP server; needs `pip install 'hyperextract[mcp]'`).
- Public Python API is curated at `hyperextract/__init__.py`: `Template`, `create_client`/`create_llm`/`create_embedder`/`get_client`, and the Auto-* types. Prefer re-exports over deep imports.
- CLI persists user config at `~/.he/config.toml` (created by `he config init -k KEY, -p provider`). Provider presets/env-var aliases live in `hyperextract/cli/config.py` (`PROVIDER_PRESETS`, `PROVIDER_API_KEY_ENV`).

## Environment / providers
- `.env.example` is the source of truth for env vars: `OPENAI_API_KEY`, optional `OPENAI_BASE_URL`, and `ANTHROPIC_API_KEY` (also accepts `CLAUDE_API_KEY` as alias). Anthropic is LLM-only and requires pairing with an OpenAI-compatible embedder.
- Any OpenAI-compatible base URL works for both LLM and embeddings (Bailian, vLLM, etc.). Plan through `create_client(llm="provider:model@base_url", embedder="...", api_key="...")`.

## Docs
- Bilingual docs via `mkdocs-static-i18n` using **folder** structure: English in `docs/`, Chinese in `docs/zh/`. Both `en` and `zh` navs in `mkdocs.yml` must be kept in sync when adding pages, or the build breaks.
- Docstring style for `mkdocstrings[python]` is **Google**, with private members (`_`-prefixed) filtered out.
- `docs_hooks.py` is loaded by mkdocs to silence expected "Multiple primary URLs found" warnings from bilingual auto-refs builds — do not remove it.
- Docs deploy uses **mike**, only on push to `main` when `docs/`, `mkdocs.yml`, `pyproject.toml`, `docs_hooks.py`, or the workflow change. Don't run `mike deploy` locally expecting CI to pick it up; preview with `mkdocs serve`.

## Releases
- PyPI publish is **automated on GitHub release** (`publish.yml`). Manually bump `pyproject.toml [project] version` before tagging — there is no auto-versioning.
- Build: `python -m build` (hatchling backend); only `hyperextract/` is shipped (wheel excludes `docs/`, `tests/`, `.github/`, `*.md`, `mkdocs.yml`, `docs_hooks.py`, `.env*`, `.python-version`, `uv.lock`).

## Workflow
- Branches `main` and `develop` are both protected and trigger CI. PRs use feature branches; commits follow lowercase conventional style (`fix:`, `test:`, `chore:` …) but there's no enforced commit linter.
- Don't commit secrets. `.env` is gitignored; only `.env.example` is tracked.