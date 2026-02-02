from __future__ import annotations

from rssfeed import Feed, InstanceConfig, State, make_handlers, run_instance


class InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        import concurrent.futures

        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []

    def execute_every(self, interval: int, func) -> None:
        self.calls.append((interval, func))


class FakeConnection:
    def __init__(self) -> None:
        self.privmsgs: list[tuple[str, str]] = []

    def privmsg(self, target: str, message: str) -> None:
        self.privmsgs.append((target, message))


class FakeReactor:
    def __init__(self) -> None:
        self.scheduler = FakeScheduler()
        self.handlers: dict[str, object] = {}
        self.connection = FakeConnection()

    def add_global_handler(self, name: str, callback) -> None:
        self.handlers[name] = callback

    def server(self):
        return self

    def connect(self, *_args, **_kwargs):
        return self.connection

    def process_forever(self) -> None:
        return None


def test_check_all_rss_skips_if_running():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
    )
    state = State(fetching=True, pending=1)
    messages: list[str] = []
    reactor = FakeReactor()
    handlers = make_handlers(
        config,
        [Feed(url="http://example.com/rss")],
        state,
        reactor,
        fetcher=lambda _url: [],
        sleeper=lambda _n: None,
        printer=messages.append,
        executor=InlineExecutor(),
    )

    handlers["check_all_rss"](reactor.connection)
    assert messages == ["Fetch already running, skipping"]


def test_check_rss_is_noop_if_running():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
    )
    state = State(fetching=True, pending=1)
    messages: list[str] = []
    reactor = FakeReactor()
    handlers = make_handlers(
        config,
        [Feed(url="http://example.com/rss")],
        state,
        reactor,
        fetcher=lambda _url: [],
        sleeper=lambda _n: None,
        printer=messages.append,
        executor=InlineExecutor(),
    )

    handlers["check_rss"](reactor.connection, Feed(url="http://example.com/rss"))
    assert messages == []


def test_check_all_rss_empty_feed_list():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
    )
    state = State()
    reactor = FakeReactor()
    handlers = make_handlers(
        config,
        [],
        state,
        reactor,
        fetcher=lambda _url: [],
        sleeper=lambda _n: None,
        printer=lambda _msg: None,
        executor=InlineExecutor(),
    )

    handlers["check_all_rss"](reactor.connection)
    assert state.fetching is False
    assert state.pending == 0


def test_run_instance_custom_executor_factory(tmp_path):
    # Covers executor_factory branch.
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

    created = {"workers": None}

    def executor_factory(workers: int):
        created["workers"] = workers
        return InlineExecutor()

    run_instance(
        config,
        reactor_factory=FakeReactor,
        fetcher=lambda _url: [],
        sleeper=lambda _n: None,
        printer=lambda _msg: None,
        executor_factory=executor_factory,
    )

    assert created["workers"] == 1


def test_check_rss_happy_path_sends_message():
    config = InstanceConfig(
        name="demo",
        nick="demo",
        ircname="Demo",
        channel="#chan",
        refresh_minutes=1,
        opml_path="feeds.opml",
    )
    reactor = FakeReactor()
    state = State(seen={"http://example.com/rss": {"old": True}})

    def fetcher(_url: str):
        return [{"title": "New", "link": "http://l"}]

    handlers = make_handlers(
        config,
        [Feed(url="http://example.com/rss")],
        state,
        reactor,
        fetcher=fetcher,
        sleeper=lambda _n: None,
        printer=lambda _msg: None,
        executor=InlineExecutor(),
    )

    handlers["check_rss"](reactor.connection, Feed(url="http://example.com/rss"))
    handlers["drain_queue"](reactor.connection)

    assert reactor.connection.privmsgs == [("#chan", "\x02New\x02 \x0314::\x03 http://l")]
