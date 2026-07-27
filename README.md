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

## Run as systemd user services

Unit files live in `systemd/` and are linked into `~/.config/systemd/user/`:

```bash
systemctl --user link $PWD/systemd/rssfeed@.service $PWD/systemd/rssfeed-brmlab.service
systemctl --user daemon-reload
systemctl --user enable --now rssfeed@slashdot rssfeed@newscz rssfeed@longreads rssfeed-brmlab
loginctl enable-linger $USER   # start at boot without login
```

Logs keep appending to `/a/logs/rss/<instance>.log` and `brmwiki.log`
via `StandardOutput=append:`. After editing units:
`systemctl --user daemon-reload && systemctl --user restart 'rssfeed@*' rssfeed-brmlab`.

### Include description

Set `include_description = true` in the instance config to append an extra `:: <description>` segment to each IRC message (HTML stripped and truncated to fit a single IRC line).

Tests:

```bash
uv sync
uv run pytest
```
