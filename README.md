# rssfeed

Python RSS-to-IRC gateway.

## Setup (uv)

```bash
uv venv
uv sync
```

Run:

```bash
uv run python ./rssfeed.py --config config.toml --instance slashdot
```

Tests:

```bash
uv sync
uv run pytest
```
