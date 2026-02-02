from __future__ import annotations

import argparse
import dataclasses
import time
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
    server: str = "open.ircnet.net"
    port: int = 6667


@dataclass
class State:
    seen: dict[str, dict[str, bool]] = dataclasses.field(default_factory=dict)


def load_config(path: str, instance_name: str) -> InstanceConfig:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    for entry in data.get("instances", []):
        if entry.get("name") == instance_name:
            return InstanceConfig(
                name=entry["name"],
                nick=entry["nick"],
                ircname=entry["ircname"],
                channel=entry["channel"],
                refresh_minutes=int(entry["refresh_minutes"]),
                opml_path=entry["opml_path"],
                extract_url=bool(entry.get("extract_url", False)),
                multisource=bool(entry.get("multisource", False)),
                server=entry.get("server", "open.ircnet.net"),
                port=int(entry.get("port", 6667)),
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


def format_message(title: str, link: str, prefix: str) -> str:
    return f"{prefix}\x02{title}\x02 \x0314::\x03 {link}"


def make_handlers(config, feeds, state, reactor, fetcher, sleeper, printer) -> dict[str, Callable]:
    def check_rss(connection: irc.client.ServerConnection, feed: Feed) -> None:
        printer(f"Fetching {feed.url}")
        prefix = f"[{feed.name}] " if config.multisource and feed.name else ""
        try:
            new_items = fetcher(feed.url)
        except Exception as exc:  # noqa: BLE001
            printer(f"Error fetching {feed.url}: {exc}")
            return
        state.seen.setdefault(feed.url, {})
        delta = delta_items(state.seen[feed.url], new_items)
        for item in reversed(delta):
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            if config.extract_url:
                link = extract_url(link)
            printer(f"-> {prefix}: {title} {link}")
            connection.privmsg(config.channel, format_message(title, link, prefix))

    def check_all_rss(connection: irc.client.ServerConnection) -> None:
        for feed in feeds:
            check_rss(connection, feed)
            sleeper(1)

    def schedule_check(connection: irc.client.ServerConnection) -> None:
        check_all_rss(connection)
        reactor.scheduler.execute_every(
            config.refresh_minutes * 60,
            lambda: check_all_rss(connection),
        )

    def on_connect(connection: irc.client.ServerConnection, _event: irc.client.Event) -> None:
        connection.join(config.channel)

    def on_joined(connection: irc.client.ServerConnection, _event: irc.client.Event) -> None:
        schedule_check(connection)

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
        "schedule_check": schedule_check,
        "on_connect": on_connect,
        "on_joined": on_joined,
        "on_cversion": on_cversion,
        "on_msg": on_msg,
    }


def run_instance(config, reactor_factory=irc.client.Reactor, fetcher=fetch_feed, sleeper=time.sleep, printer=print) -> None:
    feeds = parse_opml(config.opml_path)
    state = State()
    reactor = reactor_factory()
    handlers = make_handlers(config, feeds, state, reactor, fetcher, sleeper, printer)
    reactor.add_global_handler("welcome", handlers["on_connect"])
    reactor.add_global_handler("endofnames", handlers["on_joined"])
    reactor.add_global_handler("cversion", handlers["on_cversion"])
    reactor.add_global_handler("msg", handlers["on_msg"])
    printer(f"Connecting to {config.server}:{config.port} as {config.nick}")
    reactor.server().connect(
        config.server,
        config.port,
        config.nick,
        ircname=f"{config.ircname} (RSS feed)",
    )
    reactor.process_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    config = load_config(args.config, args.instance)
    run_instance(config)


if __name__ == "__main__":  # pragma: no cover
    main()
