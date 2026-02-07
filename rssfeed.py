from __future__ import annotations

import argparse
import dataclasses
import time
import concurrent.futures
import queue
import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import feedparser
import irc.client
import requests
import tomllib

USER_AGENT = "curl/7.21.0"

@dataclass
class Feed:
    url: str
    name: str | None = None

@dataclass
class InstanceConfig:
    name: str
    nick: str
    ircname: str
    channel: str
    refresh_minutes: int
    opml_path: str
    extract_url: bool = False
    multisource: bool = False
    # Append an extra ":: <description>" segment (truncated) to each IRC message.
    include_description: bool = False
    server: str = "irc.ircnet.com"
    port: int = 6667


@dataclass
class State:
    seen: dict[str, dict[str, bool]] = dataclasses.field(default_factory=dict)
    fetching: bool = False
    pending: int = 0


def load_config(path: str, instance_name: str) -> InstanceConfig:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    for entry in data.get("instances", []):
        if entry.get("name") == instance_name:
            # Optional keys fall back to InstanceConfig defaults.
            return InstanceConfig(
                name=entry["name"],
                nick=entry["nick"],
                ircname=entry["ircname"],
                channel=entry["channel"],
                refresh_minutes=int(entry["refresh_minutes"]),
                opml_path=entry["opml_path"],
                extract_url=bool(entry["extract_url"]) if "extract_url" in entry else InstanceConfig.extract_url,
                multisource=bool(entry["multisource"]) if "multisource" in entry else InstanceConfig.multisource,
                include_description=(
                    bool(entry["include_description"]) if "include_description" in entry
                    else bool(entry["longreads"]) if "longreads" in entry
                    else InstanceConfig.include_description
                ),
                server=entry["server"] if "server" in entry else InstanceConfig.server,
                port=int(entry["port"]) if "port" in entry else InstanceConfig.port,
            )
    raise SystemExit(f"Instance '{instance_name}' not found in {path}")


def parse_opml(path: str) -> list[Feed]:
    tree = ET.parse(path)
    root = tree.getroot()
    feeds: list[Feed] = []
    for outline in root.findall(".//outline"):
        xml_url = outline.attrib.get("xmlUrl")
        if not xml_url:
            continue
        name = outline.attrib.get("text") or outline.attrib.get("title")
        feeds.append(Feed(url=xml_url, name=name))
    return feeds


def fetch_feed(url: str) -> list[dict[str, str]]:
    response = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    return list(parsed.entries)


def delta_items(seen: dict[str, bool], new_items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    empty = not seen
    delta: list[dict[str, str]] = []
    for item in new_items:
        title = (item.get("title") or "").strip()
        if title and title not in seen:
            delta.append(item)
            seen[title] = True
    return [] if empty else delta


def extract_url(link: str) -> str:
    if "url=" not in link:
        return link
    candidate = link.rsplit("url=", 1)[-1]
    candidate = unquote(candidate)
    if not candidate.startswith("http"):
        if candidate.startswith("//"):
            candidate = "http:" + candidate
        elif candidate.startswith("/"):
            candidate = "http:" + candidate
    return candidate


IRC_SAFE_MESSAGE_LEN = 400  # conservative; RFC max line is 512 incl. overhead


def _strip_html(text: str) -> str:
    # Very small/fast HTML->text helper; good enough for RSS <description>/<summary>.
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _item_description(item: dict[str, str]) -> str:
    # feedparser uses 'summary' frequently; some feeds use 'description'.
    desc = (item.get("summary") or item.get("description") or "").strip()
    return _strip_html(desc)


def format_message(title: str, link: str, prefix: str, description: str | None = None) -> str:
    base = f"{prefix}\x02{title}\x02 \x0314::\x03 {link}"

    if not description:
        return base

    remaining = IRC_SAFE_MESSAGE_LEN - len(base) - len(" \x0314::\x03 ")
    if remaining <= 0:
        return base

    desc = description.strip()
    if len(desc) > remaining:
        # leave room for ellipsis
        if remaining >= 3:
            desc = desc[: remaining - 3].rstrip() + "..."
        else:
            desc = desc[:remaining]

    return base + f" \x0314::\x03 {desc}"


def make_handlers(
    config,
    feeds,
    state,
    reactor,
    fetcher,
    sleeper,
    printer,
    executor: concurrent.futures.Executor,
) -> dict[str, Callable]:
    # Worker threads enqueue results here; the reactor thread drains the queue and
    # performs state updates + IRC sends.
    results: queue.Queue[tuple[Feed, list[dict[str, str]] | Exception]] = queue.Queue()

    def _handle_feed_items(connection: irc.client.ServerConnection, feed: Feed, new_items: list[dict[str, str]]) -> None:
        prefix = f"[{feed.name}] " if config.multisource and feed.name else ""
        state.seen.setdefault(feed.url, {})
        delta = delta_items(state.seen[feed.url], new_items)
        for item in reversed(delta):
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            if config.extract_url:
                link = extract_url(link)
            printer(f"-> {prefix}: {title} {link}")
            desc = _item_description(item) if config.include_description else None
            connection.privmsg(config.channel, format_message(title, link, prefix, desc))

    def drain_queue(connection: irc.client.ServerConnection) -> None:
        if not connection.is_connected():
            return
        while True:
            try:
                feed, payload = results.get_nowait()
            except queue.Empty:
                break

            if isinstance(payload, Exception):
                printer(f"Error fetching {feed.url}: {payload}")
            else:
                try:
                    _handle_feed_items(connection, feed, payload)
                except irc.client.ServerNotConnectedError:
                    printer("Lost connection while sending, will retry after reconnect")
                    return

            if state.pending > 0:
                state.pending -= 1
            if state.pending == 0:
                state.fetching = False

    def _submit_fetch(feed: Feed) -> None:
        # This is the equivalent of a "threadpooled map" without blocking the
        # reactor thread: we submit each job and collect results later.
        printer(f"Fetching {feed.url}")
        future = executor.submit(fetcher, feed.url)

        def _done(fut: concurrent.futures.Future, _feed: Feed = feed) -> None:
            exc = fut.exception()
            if exc is not None:
                results.put((_feed, exc))
            else:
                results.put((_feed, fut.result()))

        future.add_done_callback(_done)

    def check_rss(connection: irc.client.ServerConnection, feed: Feed) -> None:
        # Backwards-compat; schedule a single-feed fetch.
        if state.fetching:
            return
        state.fetching = True
        state.pending = 1
        _submit_fetch(feed)
        drain_queue(connection)  # helps in tests with InlineExecutor

    def check_all_rss(connection: irc.client.ServerConnection) -> None:
        if state.fetching:
            printer("Fetch already running, skipping")
            return

        if not feeds:
            return

        state.fetching = True
        state.pending = len(feeds)
        for feed in feeds:
            _submit_fetch(feed)

        drain_queue(connection)  # helps in tests with InlineExecutor

    def schedule_check(connection: irc.client.ServerConnection) -> None:
        # Drain results regularly from within the reactor thread.
        reactor.scheduler.execute_every(1, lambda: drain_queue(connection))
        check_all_rss(connection)
        reactor.scheduler.execute_every(
            config.refresh_minutes * 60,
            lambda: check_all_rss(connection),
        )

    def on_connect(connection: irc.client.ServerConnection, _event: irc.client.Event) -> None:
        connection.join(config.channel)

    def on_joined(connection: irc.client.ServerConnection, _event: irc.client.Event) -> None:
        schedule_check(connection)

    def on_disconnect(connection: irc.client.ServerConnection, _event: irc.client.Event) -> None:
        printer(f"Disconnected from server, reconnecting in 60 seconds...")
        sleeper(60)
        try:
            connection.connect(
                config.server,
                config.port,
                config.nick,
                ircname=f"{config.ircname} (RSS feed)",
            )
        except irc.client.ServerConnectionError as e:
            printer(f"Reconnect failed: {e}, will retry on next scheduler tick")
            reactor.scheduler.execute_after(60, lambda: on_disconnect(connection, _event))

    def on_cversion(connection: irc.client.ServerConnection, event: irc.client.Event) -> None:
        connection.ctcp_reply(event.source.nick, "VERSION RSS->IRC gateway")

    def on_msg(connection: irc.client.ServerConnection, event: irc.client.Event) -> None:
        args = event.arguments
        if not args:
            return
        message = args[0]
        if message.startswith("~msg "):
            parts = message.split(" ", 2)
            if len(parts) == 3:
                target, body = parts[1], parts[2]
                connection.privmsg(target, body)
                connection.privmsg(event.source.nick, f"Sent to {target}: {body}")
        elif message.startswith("~refresh"):
            check_all_rss(connection)

    return {
        "check_rss": check_rss,
        "check_all_rss": check_all_rss,
        "drain_queue": drain_queue,
        "schedule_check": schedule_check,
        "on_connect": on_connect,
        "on_joined": on_joined,
        "on_disconnect": on_disconnect,
        "on_cversion": on_cversion,
        "on_msg": on_msg,
    }


def run_instance(
    config,
    reactor_factory=irc.client.Reactor,
    fetcher=fetch_feed,
    sleeper=time.sleep,
    printer=print,
    executor_factory: Callable[[int], concurrent.futures.Executor] | None = None,
) -> None:
    feeds = parse_opml(config.opml_path)
    state = State()
    reactor = reactor_factory()

    if executor_factory is None:
        executor_factory = lambda workers: concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    max_workers = max(1, min(8, len(feeds) or 1))
    executor = executor_factory(max_workers)

    handlers = make_handlers(config, feeds, state, reactor, fetcher, sleeper, printer, executor)
    reactor.add_global_handler("welcome", handlers["on_connect"])
    reactor.add_global_handler("endofnames", handlers["on_joined"])
    reactor.add_global_handler("disconnect", handlers["on_disconnect"])
    reactor.add_global_handler("cversion", handlers["on_cversion"])
    reactor.add_global_handler("msg", handlers["on_msg"])
    printer(f"Connecting to {config.server}:{config.port} as {config.nick}")
    connection = reactor.server()
    hosts = [config.server] if config.server.endswith(".") else [config.server, f"{config.server}."]
    for attempt in range(3):
        for host in hosts:
            try:
                connection.connect(host, config.port, config.nick, ircname=f"{config.ircname} (RSS feed)")
                reactor.process_forever()
                return
            except irc.client.ServerConnectionError as exc:
                printer(f"Connect failed for {host}:{config.port}: {exc}")
        if attempt < 2:
            sleeper(5)
    raise SystemExit(f"Unable to connect to IRC server(s): {', '.join(hosts)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    config = load_config(args.config, args.instance)
    run_instance(config)


if __name__ == "__main__":  # pragma: no cover
    main()
