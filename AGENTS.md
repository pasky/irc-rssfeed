# Instructions for coding agents

This repo uses **uv** to manage the Python environment and dev dependencies.

## Always run tests in the uv environment

Do **not** run `pytest` directly.

Use:

```bash
uv sync
uv run pytest
```

Rationale:
- Dev dependencies (e.g. `pytest-cov`) are installed via uv.
- Runtime deps (e.g. `irc`) are installed via uv.
- Running system `pytest` can pick a different Python (pyenv/system) and fail with missing plugins/deps.

## Do not “fix” missing dependencies by stubbing/shimming

If you see `ModuleNotFoundError` for a declared dependency (e.g. `irc`), that usually means tests were run outside uv.

Action items:
- Re-run via `uv run ...`.
- If still missing, update dependency declarations (`pyproject.toml`) rather than adding local fake modules.

## Keep coverage enforcement

`pytest` is configured with coverage requirements in `pyproject.toml`.
Do not remove/relax these settings to get tests to pass; fix the code/tests instead.

## Typical workflow

```bash
uv sync
uv run pytest
uv run python rssfeed.py --config config.toml --instance <name>
```
