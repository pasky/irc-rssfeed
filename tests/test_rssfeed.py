from __future__ import annotations

from dataclasses import dataclass

import pytest

from rssfeed import (
    IRC_SAFE_MESSAGE_LEN,
    Feed,
    InstanceConfig,
    State,
    delta_items,
    extract_url,
    fetch_feed,
    format_message,
    load_config,
    make_handlers,
    parse_opml,
    run_instance,
)


class InlineExecutor:
    """Executor that runs tasks immediately in the caller thread (for tests)."""

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures

        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut


@dataclass
class FakeSource:
    nick: str


@dataclass
class FakeEvent:
    arguments: list[str]
    source: FakeSource


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []
        self.delayed: list[tuple[int, object]] = []

    def execute_every(self, interval: int, func) -> None:
        self.calls.append((interval, func))

    def execute_after(self, delay: int, func) -> None:
        self.delayed.append((delay, func))


class FakeConnection:
    def __init__(self) -> None:
        self.joined: list[str] = []
        self.privmsgs: list[tuple[str, str]] = []
        self.ctcps: list[tuple[str, str]] = []
        self._connected: bool = True

    def join(self, channel: str) -> None:
        self.joined.append(channel)

    def privmsg(self, target: str, message: str) -> None:
        self.privmsgs.append((target, message))

    def ctcp_reply(self, target: str, message: str) -> None:
        self.ctcps.append((target, message))

    def is_connected(self) -> bool:
        return self._connected


class FakeReactor:
    def __init__(self) -> None:
        self.scheduler = FakeScheduler()
        self.handlers: dict[str, object] = {}
        self.connected: list[tuple] = []
        self.processed = False
        self.connection = FakeConnection()

    def add_global_handler(self, name: str, callback) -> None:
        self.handlers[name] = callback

    def server(self):
        return self

    def connect(self, server: str, port: int, nick: str, ircname: str) -> FakeConnection:
        self.connected.append((server, port, nick, ircname))
        return self.connection

    def process_forever(self) -> None:
        self.processed = True


def test_parse_opml(tmp_path):
    content = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<opml version=\"2.0\"><body>
  <outline text=\"Example\" xmlUrl=\"http://example.com/rss\" />
  <outline xmlUrl=\"http://example.com/alt\" />
  <outline text=\"MissingUrl\" />
</body></opml>"""
    path = tmp_path / "feeds.opml"
    path.write_text(content, encoding="utf-8")

    feeds = parse_opml(str(path))

    assert feeds[0].url == "http://example.com/rss"
    assert feeds[0].name == "Example"
    assert feeds[1].url == "http://example.com/alt"
    assert feeds[1].name is None


def test_fetch_feed(monkeypatch):
    class DummyResponse:
        def __init__(self) -> None:
            self.content = b"<rss />"

        def raise_for_status(self) -> None:
            return None

    class DummyParsed:
        def __init__(self) -> None:
            self.entries = [{"title": "Item"}]

    def fake_get(_url: str, timeout: int, headers: dict[str, str]):
        assert timeout == 30
        assert "User-Agent" in headers
        return DummyResponse()

    def fake_parse(_content: bytes):
        return DummyParsed()

    monkeypatch.setattr("rssfeed.requests.get", fake_get)
    monkeypatch.setattr("rssfeed.feedparser.parse", fake_parse)

    items = fetch_feed("http://example.com/rss")
    assert items == [{"title": "Item"}]


def test_delta_items_first_run_empty():
    seen = {}
    delta = delta_items(seen, [{"title": "Item 1"}])
    assert delta == []
    assert "Item 1" in seen


def test_delta_items_subsequent():
    seen = {"Item 1": True}
    delta = delta_items(seen, [{"title": "Item 1"}, {"title": "Item 2"}])
    assert [item["title"] for item in delta] == ["Item 2"]


def test_extract_url_variants():
    link = "https://example.com/?id=1&url=https%3A%2F%2Ftarget.test%2Fpath"
    assert extract_url(link) == "https://target.test/path"
    assert extract_url("https://example.com/") == "https://example.com/"
    assert extract_url("https://example.com/?url=//target.test/path") == "http://target.test/path"
    assert extract_url("https://example.com/?url=/target/path") == "http:/target/path"


def test_format_message_description_truncation_paths():
    # No description -> unchanged
    msg = format_message("T", "https://x", "", None)
    assert "https://x" in msg
    assert msg.count("\x0314::\x03") == 1

    # remaining <= 0: base already fills (or exceeds) the safe message length
    very_long_link = "http://" + ("x" * (IRC_SAFE_MESSAGE_LEN + 50))
    base = format_message("T", very_long_link, "")
    with_desc = format_message("T", very_long_link, "", "abc")
    assert with_desc == base

    # remaining >= 3: ellipsis branch
    title = "T"
    link = "https://x"
    base = format_message(title, link, "")
    # make description longer than the remaining budget
    long_desc = "a" * 1000
    truncated = format_message(title, link, "", long_desc)
    assert truncated.startswith(base + " \x0314::\x03 ")
    assert truncated.endswith("...")

    # remaining < 3: non-ellipsis truncation branch (exercise the else)
    sep = " \x0314::\x03 "
    # Choose a base length so remaining becomes 2.
    # remaining = IRC_SAFE_MESSAGE_LEN - len(base) - len(sep)
    target_remaining = 2
    # build a link that makes base hit the desired length
    # base = "\x02{title}\x02 \x0314::\x03 {link}"
    fixed_overhead = len("\x02") + len("\x02 ") + len("\x0314::\x03 ")
    # But easiest: construct iteratively.
    title = "T"
    prefix = ""
    # find link length to hit remaining==2
    # brute force a bit to stay stable
    for n in range(1, 2000):
        link = "h" * n
        base_candidate = format_message(title, link, prefix)
        rem = IRC_SAFE_MESSAGE_LEN - len(base_candidate) - len(sep)
        if rem == target_remaining:
            out = format_message(title, link, prefix, "abcdef")
            assert out.endswith("ab")  # truncated to exactly 2 chars
            break
    else:
        raise AssertionError("Could not find lengths to hit remaining==2")


def test_load_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[[instances]]
name = "demo"
nick = "demo"
ircname = "Demo"
channel = "#demo"
refresh_minutes = 5
opml_path = "feeds.opml"
extract_url = true
multisource = true
""".strip(),
        encoding="utf-8",
    )

    config = load_config(str(config_path), "demo")

    assert config.name == "demo"
    assert config.extract_url is True
    assert config.multisource is True


def test_load_config_missing(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[[instances]]
name = "demo"
nick = "demo"
ircname = "Demo"
channel = "#demo"
refresh_minutes = 5
opml_path = "feeds.opml"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        load_config(str(config_path), "missing")


def test_handlers_flow():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
        extract_url=True,
        multisource=True,
    )
    feeds = [Feed(url="http://example.com/rss", name="Source")]
    state = State()
    reactor = FakeReactor()
    messages: list[str] = []

    calls = 0

    def fetcher(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                {
                    "title": "Item 1",
                    "link": "https://example.com/?url=https%3A%2F%2Fsite.test%2F",
                }
            ]
        return [
            {
                "title": "Item 2",
                "link": "https://example.com/?url=https%3A%2F%2Fsite.test%2F",
            }
        ]

    handlers = make_handlers(
        config,
        feeds,
        state,
        reactor,
        fetcher,
        sleeper=lambda _n: None,
        printer=messages.append,
        executor=InlineExecutor(),
    )

    handlers["on_connect"](reactor.connection, FakeEvent([], FakeSource("nick")))
    assert reactor.connection.joined == ["#chan"]

    handlers["on_joined"](reactor.connection, FakeEvent([], FakeSource("nick")))
    assert reactor.scheduler.calls

    handlers["check_all_rss"](reactor.connection)
    handlers["drain_queue"](reactor.connection)

    handlers["check_all_rss"](reactor.connection)
    handlers["drain_queue"](reactor.connection)

    msg_event = FakeEvent(["~msg #target hello"], FakeSource("alice"))
    handlers["on_msg"](reactor.connection, msg_event)
    assert ("#target", "hello") in reactor.connection.privmsgs

    refresh_event = FakeEvent(["~refresh"], FakeSource("alice"))
    handlers["on_msg"](reactor.connection, refresh_event)

    handlers["on_cversion"](reactor.connection, FakeEvent([], FakeSource("bob")))
    assert reactor.connection.ctcps == [("bob", "VERSION RSS->IRC gateway")]

    assert any(message.startswith("[Source]") for _target, message in reactor.connection.privmsgs)


def test_include_description_appends_description():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
        multisource=False,
        include_description=True,
    )
    feeds = [Feed(url="http://example.com/rss")]
    state = State()
    reactor = FakeReactor()

    calls = 0

    def fetcher(_url: str):
        nonlocal calls
        calls += 1
        title = "Warmup" if calls == 1 else "Item 1"
        return [
            {
                "title": title,
                "link": "https://site.test/post",
                "summary": "<p>I don't necessarily believe in <b>anything</b> permanently.</p>",
            }
        ]

    handlers = make_handlers(
        config,
        feeds,
        state,
        reactor,
        fetcher,
        sleeper=lambda _n: None,
        printer=lambda _msg: None,
        executor=InlineExecutor(),
    )

    handlers["check_all_rss"](reactor.connection)
    handlers["drain_queue"](reactor.connection)
    # first run warms the seen-cache; second run should emit
    handlers["check_all_rss"](reactor.connection)
    handlers["drain_queue"](reactor.connection)

    # With include_description=True we should get an extra :: <description> segment
    sent = [msg for _target, msg in reactor.connection.privmsgs if "Item 1" in msg]
    assert sent
    assert "\x0314::\x03 https://site.test/post" in sent[-1]
    assert "\x0314::\x03 I don't necessarily believe in anything permanently." in sent[-1]


def test_handlers_fetch_error():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
    )
    feeds = [Feed(url="http://example.com/rss")]
    state = State()
    reactor = FakeReactor()
    errors: list[str] = []

    def fetcher(_url: str):
        raise RuntimeError("boom")

    handlers = make_handlers(
        config,
        feeds,
        state,
        reactor,
        fetcher,
        sleeper=lambda _n: None,
        printer=errors.append,
        executor=InlineExecutor(),
    )

    handlers["check_all_rss"](reactor.connection)
    handlers["drain_queue"](reactor.connection)
    assert any("Error fetching" in msg for msg in errors)


def test_on_msg_edge_cases():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
    )
    handlers = make_handlers(
        config,
        [Feed(url="http://example.com/rss")],
        State(),
        FakeReactor(),
        fetcher=lambda _url: [],
        sleeper=lambda _n: None,
        printer=lambda _msg: None,
        executor=InlineExecutor(),
    )
    connection = FakeConnection()

    handlers["on_msg"](connection, FakeEvent([], FakeSource("nick")))
    handlers["on_msg"](connection, FakeEvent(["~msg #target"], FakeSource("nick")))

    assert connection.privmsgs == []


def test_scheduler_signature_matches_real():
    import inspect
    import irc.client

    real_signature = inspect.signature(irc.client.Reactor().scheduler.execute_every)
    fake_signature = inspect.signature(FakeScheduler.execute_every)

    real_params = list(real_signature.parameters.values())
    fake_params = list(fake_signature.parameters.values())[1:]

    assert [param.kind for param in real_params] == [param.kind for param in fake_params]
    assert len(real_params) == len(fake_params)


def test_run_instance_registers_handlers(tmp_path):
    opml_path = tmp_path / "feeds.opml"
    opml_path.write_text(
        """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<opml version=\"2.0\"><body>
  <outline text=\"Example\" xmlUrl=\"http://example.com/rss\" />
</body></opml>""",
        encoding="utf-8",
    )
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path=str(opml_path),
    )
    reactor = FakeReactor()

    run_instance(
        config,
        reactor_factory=lambda: reactor,
        fetcher=lambda _url: [],
        sleeper=lambda _n: None,
        printer=lambda _msg: None,
    )

    assert reactor.connected
    assert reactor.processed is True
    assert set(reactor.handlers) == {"welcome", "endofnames", "disconnect", "cversion", "msg"}


def test_main_invokes_run_instance(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[[instances]]
name = "demo"
nick = "demo"
ircname = "Demo"
channel = "#demo"
refresh_minutes = 5
opml_path = "feeds.opml"
""".strip(),
        encoding="utf-8",
    )
    called = {}

    def fake_run_instance(config):
        called["name"] = config.name

    monkeypatch.setattr("rssfeed.run_instance", fake_run_instance)
    monkeypatch.setattr("sys.argv", ["rssfeed.py", "--config", str(config_path), "--instance", "demo"])

    from rssfeed import main

    main()
    assert called["name"] == "demo"


def test_drain_queue_skips_when_disconnected():
    """drain_queue returns immediately when connection is not connected."""
    config = InstanceConfig(
        name="demo", nick="demo", ircname="Demo", channel="#chan",
        refresh_minutes=1, opml_path="feeds.opml",
    )
    feeds = [Feed(url="http://example.com/rss", name="Source")]
    state = State()
    reactor = FakeReactor()
    messages: list[str] = []

    handlers = make_handlers(
        config, feeds, state, reactor,
        fetcher=lambda _url: [{"title": "T", "link": "http://x"}],
        sleeper=lambda _n: None, printer=messages.append,
        executor=InlineExecutor(),
    )

    # Disconnect the fake connection
    reactor.connection._connected = False
    handlers["on_joined"](reactor.connection, FakeEvent([], FakeSource("nick")))
    # drain_queue should skip; no privmsgs sent
    assert reactor.connection.privmsgs == []


def test_drain_queue_handles_server_not_connected_error():
    """drain_queue catches ServerNotConnectedError mid-send."""
    import irc.client

    config = InstanceConfig(
        name="demo", nick="demo", ircname="Demo", channel="#chan",
        refresh_minutes=1, opml_path="feeds.opml",
    )
    feeds = [Feed(url="http://example.com/rss", name="Source")]
    state = State()
    reactor = FakeReactor()
    messages: list[str] = []

    call_count = 0

    def fetcher(_url):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [{"title": "T1", "link": "http://x"}]
        return [{"title": "T1", "link": "http://x"}, {"title": "T2", "link": "http://y"}]

    class DisconnectingConnection(FakeConnection):
        def privmsg(self, target, message):
            raise irc.client.ServerNotConnectedError("Not connected.")

    conn = DisconnectingConnection()

    handlers = make_handlers(
        config, feeds, state, reactor,
        fetcher=fetcher,
        sleeper=lambda _n: None, printer=messages.append,
        executor=InlineExecutor(),
    )

    # First call seeds seen items
    handlers["check_all_rss"](conn)
    state.fetching = False  # reset
    # Second call has a new item -> triggers privmsg -> raises
    handlers["check_all_rss"](conn)
    assert any("Lost connection" in m for m in messages), messages


def test_on_disconnect_reconnects():
    """on_disconnect sleeps then reconnects."""
    config = InstanceConfig(
        name="demo", nick="demo", ircname="Demo", channel="#chan",
        refresh_minutes=1, opml_path="feeds.opml",
        server="irc.test", port=6667,
    )
    feeds = [Feed(url="http://example.com/rss", name="Source")]
    state = State()
    reactor = FakeReactor()
    messages: list[str] = []
    sleeps: list = []

    class ReconnectableConnection(FakeConnection):
        def __init__(self):
            super().__init__()
            self.connect_calls: list[tuple] = []

        def connect(self, server, port, nick, ircname=""):
            self.connect_calls.append((server, port, nick, ircname))

    conn = ReconnectableConnection()

    handlers = make_handlers(
        config, feeds, state, reactor,
        fetcher=lambda _url: [],
        sleeper=sleeps.append, printer=messages.append,
        executor=InlineExecutor(),
    )

    handlers["on_disconnect"](conn, FakeEvent([], FakeSource("nick")))
    assert sleeps == [60]
    assert conn.connect_calls == [("irc.test", 6667, "demo", "Demo (RSS feed)")]


def test_on_disconnect_schedules_retry_on_failure():
    """on_disconnect schedules retry when reconnect fails."""
    import irc.client

    config = InstanceConfig(
        name="demo", nick="demo", ircname="Demo", channel="#chan",
        refresh_minutes=1, opml_path="feeds.opml",
        server="irc.test", port=6667,
    )
    feeds = [Feed(url="http://example.com/rss", name="Source")]
    state = State()
    reactor = FakeReactor()
    messages: list[str] = []

    class FailingConnection(FakeConnection):
        def connect(self, server, port, nick, ircname=""):
            raise irc.client.ServerConnectionError("fail")

    conn = FailingConnection()

    handlers = make_handlers(
        config, feeds, state, reactor,
        fetcher=lambda _url: [],
        sleeper=lambda _n: None, printer=messages.append,
        executor=InlineExecutor(),
    )

    handlers["on_disconnect"](conn, FakeEvent([], FakeSource("nick")))
    assert any("Reconnect failed" in m for m in messages)
    assert len(reactor.scheduler.delayed) == 1
    assert reactor.scheduler.delayed[0][0] == 60
