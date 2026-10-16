"""Случайный анекдот из публичного RSS без данных обращения."""

import asyncio
from html.parser import HTMLParser
from http.client import HTTPException
import logging
import os
import random
import time
from urllib.request import Request, urlopen
from xml.etree import ElementTree


FEED_URL = "https://www.anekdot.ru/rss/export_j.xml"
SOURCE_URL = "https://www.anekdot.ru/"
FETCH_TIMEOUT = 3
MAX_FEED_BYTES = 512 * 1024
CACHE_SECONDS = 60 * 60
RETRY_SECONDS = 5 * 60
LOG = logging.getLogger("moneyrun_relay")


def jokes_enabled() -> bool:
    value = os.environ.get("JOKES_ENABLED", "false").strip().casefold()
    if value not in ("true", "false"):
        raise ValueError("JOKES_ENABLED: допустимые значения — true или false.")
    return value == "true"


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def _fetch_jokes():
    request = Request(FEED_URL, headers={"User-Agent": "MoneyrunRelay/1.0"})
    with urlopen(request, timeout=FETCH_TIMEOUT) as response:
        body = response.read(MAX_FEED_BYTES + 1)
    if len(body) > MAX_FEED_BYTES:
        raise ValueError("RSS превышает допустимый размер.")
    # NUL также убираются для обнаружения DOCTYPE в XML с UTF-16/UTF-32.
    if b"<!DOCTYPE" in body.replace(b"\x00", b"").upper():
        raise ValueError("DTD в RSS запрещён.")
    feed = ElementTree.fromstring(body)
    jokes = []
    for description in feed.findall("./channel/item/description"):
        parser = _PlainText()
        parser.feed((description.text or "") + "".join(
            ElementTree.tostring(child, encoding="unicode") for child in description))
        parser.close()
        text = "".join(parser.parts).strip()
        if text:
            jokes.append(text)
    if not jokes:
        raise ValueError("RSS не содержит анекдотов.")
    return jokes


class JokeSource:
    def __init__(self):
        self._jokes = []
        self._next_fetch_at = 0

    async def choose(self):
        now = time.monotonic()
        if now >= self._next_fetch_at:
            self._next_fetch_at = now + RETRY_SECONDS
            try:
                jokes = await asyncio.wait_for(asyncio.to_thread(_fetch_jokes),
                                               timeout=FETCH_TIMEOUT)
            except (OSError, HTTPException, ElementTree.ParseError, ValueError, LookupError) as error:
                self._next_fetch_at = time.monotonic() + RETRY_SECONDS
                LOG.warning("Не удалось обновить анекдоты (%s); используется прежний кеш или пропуск.",
                            type(error).__name__)
            else:
                self._jokes = jokes
                self._next_fetch_at = time.monotonic() + CACHE_SECONDS
        return random.choice(self._jokes) if self._jokes else None


def append_joke(text, joke):
    if not joke or not joke.strip():
        return text
    result = text + "\n\n────────────\n😄 Анекдот\n" + joke + "\n\nИсточник: " + SOURCE_URL
    return result if len(result.encode("utf-16-le")) // 2 <= 4096 else text
