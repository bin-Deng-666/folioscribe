# Contributing to FolioScribe

Use Python 3.11+ and install from the repository root:

```bash
python -m pip install -e '.[dev,annotation]'
python -m pytest -q --ignore=tests/browser
ruff check .
node --check components/annotation/viewer/static/app.js
```

For viewer behavior changes, install `.[browser]`, run `python -m playwright install chromium`, then `python -m pytest -q tests/browser`. You can use an existing Chromium browser by setting `FOLIOSCRIBE_BROWSER` to its executable path.

With uv, use `uv sync --extra dev --extra annotation --locked` to install from the root `uv.lock`. The lock covers project and client dependencies; GPU services use separate environments described in `docs/deployment/`.

## Change boundaries

- Name modules by responsibility. Keep protocol versions in constants, schemas and manifests; do not put release suffixes on the current implementation file.
- Keep scripts thin; put reusable behavior in the installed packages. Prefer explicit input/output paths over machine-specific defaults.
- Preserve document boundaries. Split training and evaluation by document, never by page.
- Do not place current-page targets in the current-page prompt. Training and inference must use the same format and document-state implementation.
- Add regression coverage for changed behavior, especially content loss, geometry, continuation, data provenance and persistence. Use synthetic documents where possible.
- Establish correctness before reporting performance gains. State the workload, hardware and measurement method; do not infer accuracy from unit tests.
- Avoid mixing mass formatting with algorithm changes. Ruff initially gates correctness errors; broader style cleanup can proceed module by module.
- Document incompatible data changes and bump the appropriate format/prompt/metric version. File renames alone do not change stored annotation schemas.

## Review expectations

Describe the concrete failure, resulting behavior, validation and remaining limitations. Include before/after screenshots for visible viewer changes, using synthetic or explicitly shareable data. Do not add keys, business documents, model weights, generated datasets or experiment outputs to Git.

Review the [roadmap](ROADMAP.md) for open work and [third-party notices](THIRD_PARTY_NOTICES.md) before introducing or distributing external code, dependencies or models.
