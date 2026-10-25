"""Fetch and parse RSS 2.0 / Atom feeds into Article records (stdlib only)."""
from __future__ import annotations

import email.utils
import hashlib
import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

USER_AGENT = "worldterm/0.1 (+https://github.com/emmeryn/worldterm)"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"


class FeedError(Exception):
    """Raised when a feed cannot be fetched or parsed."""


@dataclass
class Article:
    guid: str
    title: str
    summary: str
    link: str
    published: datetime | None
    source: str
    categories: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- fetch

def fetch(url: str, timeout: float = 15.0) -> bytes:
    """Return raw bytes for *url*. Supports http(s) and file:// URLs."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise FeedError(f"{exc.reason} fetching {url}") from exc
    except (OSError, ValueError) as exc:
        raise FeedError(f"{exc} fetching {url}") from exc


# --------------------------------------------------------------------------- HTML strip

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("br", "p", "div", "li"):
            self.parts.append(" ")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # pragma: no cover - extremely malformed input
        return _collapse(html.unescape(re.sub(r"<[^>]+>", " ", text)))
    return _collapse(html.unescape("".join(parser.parts)))


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- dates

def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt is not None:
            return _aware(dt)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- parse

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, *names: str) -> str | None:
    """Non-empty text of a direct child, trying *names* in priority order."""
    for name in names:
        for child in el:
            if _local(child.tag) == name and child.text and child.text.strip():
                return child.text.strip()
    return None


def parse(data: bytes | str, fallback_source: str = "feed") -> list[Article]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"XML parse error: {exc}") from exc

    root_name = _local(root.tag)
    if root_name == "feed":
        return _parse_atom(root, fallback_source)
    if root_name in ("rss", "RDF"):
        return _parse_rss(root, fallback_source)
    channel = root.find("channel")
    if channel is not None:
        return _parse_rss(root, fallback_source)
    raise FeedError(f"Unrecognised feed root element <{root_name}>")


def _parse_rss(root: ET.Element, fallback_source: str) -> list[Article]:
    channel = next((c for c in root.iter() if _local(c.tag) == "channel"), None)
    source = fallback_source
    if channel is not None:
        source = _child_text(channel, "title") or fallback_source
    items = [el for el in root.iter() if _local(el.tag) == "item"]
    articles: list[Article] = []
    for item in items:
        title = strip_html(_child_text(item, "title"))
        summary = strip_html(_child_text(item, "description", "summary", "encoded"))
        link = _child_text(item, "link") or ""
        guid = _child_text(item, "guid") or link
        published = parse_date(_child_text(item, "pubDate", "date", "published", "updated"))
        if not guid:
            guid = _hash_guid(title, published)
        cats = [c.text.strip() for c in item if _local(c.tag) == "category" and c.text]
        articles.append(Article(guid, title, summary, link, published, source, cats))
    return articles


def _parse_atom(root: ET.Element, fallback_source: str) -> list[Article]:
    source = _child_text(root, "title") or fallback_source
    articles: list[Article] = []
    for entry in (e for e in root if _local(e.tag) == "entry"):
        title = strip_html(_child_text(entry, "title"))
        summary = strip_html(_child_text(entry, "summary", "content"))
        link = ""
        for l in (c for c in entry if _local(c.tag) == "link"):
            rel = l.get("rel", "alternate")
            if rel == "alternate" and l.get("href"):
                link = l.get("href", "")
                break
        guid = _child_text(entry, "id") or link
        published = parse_date(_child_text(entry, "published", "updated"))
        if not guid:
            guid = _hash_guid(title, published)
        cats = [c.get("term", "") for c in entry if _local(c.tag) == "category" and c.get("term")]
        articles.append(Article(guid, title, summary, link, published, source, cats))
    return articles


def _hash_guid(title: str, published: datetime | None) -> str:
    h = hashlib.sha1()
    h.update(title.encode("utf-8"))
    h.update((published.isoformat() if published else "").encode("utf-8"))
    return "sha1:" + h.hexdigest()


def load(url: str, timeout: float = 15.0) -> list[Article]:
    """fetch + parse convenience wrapper."""
    return parse(fetch(url, timeout), fallback_source=url)
