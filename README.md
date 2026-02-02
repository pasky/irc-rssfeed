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

### Include description

Set `include_description = true` in the instance config to append an extra `:: <description>` segment to each IRC message (HTML stripped and truncated to fit a single IRC line).

Tests:

```bash
uv sync
uv run pytest
```
