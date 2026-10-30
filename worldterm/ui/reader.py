"""Article detail screen (Netcool: event information).

For a news event it also asks for the article's text, which arrives from a download thread
and replaces the feed's summary. Every string from a feed or a page reaches a widget as
`Text`, never as markup (principle 8).
"""
from __future__ import annotations

from typing import Callable, Iterable
from urllib.parse import urlsplit

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ..article import Block, Body
from ..severity import matched_keywords
from ..store import Event
from .styles import SEVERITY_STYLE

# WorldTerm.request_article: (link, callback) -> None; the callback gets (link, Body or why not).
RequestArticle = Callable[[str, Callable[[str, "Body | str"], None]], None]
FETCHABLE = ("http://", "https://", "file://")


class ReaderScreen(Screen[str]):
    """Dismisses with 'back', 'note', 'ack' or 'open'."""

    DEFAULT_CSS = """
    ReaderScreen { layout: vertical; }
    ReaderScreen #bar { height: 1; background: $primary; color: $text; text-style: bold; padding: 0 1; }
    ReaderScreen VerticalScroll { padding: 1 2; }
    ReaderScreen #title { text-style: bold; color: $warning; margin-bottom: 1; }
    ReaderScreen #sev { width: auto; margin-bottom: 1; }
    ReaderScreen #meta { margin-bottom: 1; }
    ReaderScreen #body { max-width: 100; }
    ReaderScreen #note { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape,q,h,left", "back", "Back"),
        Binding("n", "note", "Note"),
        Binding("a", "ack", "Ack"),
        Binding("o", "open", "Open link"),
        Binding("j,down", "scroll_down", "Scroll", show=False),
        Binding("k,up", "scroll_up", "Scroll", show=False),
    ]

    def __init__(self, event: Event, request_article: RequestArticle | None = None) -> None:
        super().__init__()
        self.event = event
        link = event.article.link
        # Only news has an article to fetch: an alert's reader already shows its whole text.
        fetch = request_article is not None and event.alert is None and link.startswith(FETCHABLE)
        self._request_article = request_article if fetch else None
        self.article: Body | None = None
        self.article_status: str | None = f"fetching from {_host(link)} …" if fetch else None

    def compose(self) -> ComposeResult:
        ev = self.event
        a = ev.article
        alert = ev.alert
        yield Static(Text(f" READ  {ev.node}"), id="bar")
        with VerticalScroll():
            yield Static(Text((alert.event if alert else a.title) or "(no title)"), id="title")
            yield Static(Text(f" {ev.severity.label.upper()} ", style=SEVERITY_STYLE[ev.severity]), id="sev")
            yield Static(self._alert_meta() if alert else self._meta_text(), id="meta")
            yield Static(self._body_text(), id="body")
            if ev.note:
                yield Static(Text(f"Note: {ev.note}"), id="note")
        yield Footer()

    def on_mount(self) -> None:
        if self._request_article is not None:
            self._request_article(self.event.article.link, self._article_ready)

    def _article_ready(self, link: str, result: Body | str) -> None:
        """The fetch is over: show the article, or say why the summary stays."""
        if not self.is_attached:
            return  # the reader was closed, or the app quit, while the page downloaded
        if isinstance(result, Body):
            self.article = result
            self.article_status = (f"{result.words:,} words from {result.host or 'a local file'}, "
                                   "extracted from the page (o opens the original)")
            self.query_one("#body", Static).update(self._body_text())
        else:
            reason = result.removesuffix(f" fetching {link}")
            self.article_status = f"unavailable ({reason}); showing the feed's summary"
        self.query_one("#meta", Static).update(self._meta_text())

    def _body_text(self) -> Text:
        ev = self.event
        alert = ev.alert
        if self.article is not None:
            return blocks_text(self.article.blocks)
        if alert is None:
            return Text(ev.article.summary or "(no summary)")
        parts = [alert.description or "(no description)"]
        if alert.instruction:
            parts.append("PRECAUTIONARY/PREPAREDNESS ACTIONS\n\n" + alert.instruction)
        return Text("\n\n".join(parts))

    def _alert_meta(self) -> Text:
        ev = self.event
        alert = ev.alert
        assert alert is not None

        def when(dt) -> str:
            return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z") if dt else "unknown"

        flags = " ".join(f for f, on in (("ACK", ev.acknowledged), ("READ", ev.read), ("NOTE", bool(ev.note))) if on)
        rows = [
            ("Office:    ", alert.sender),
            ("NWS:       ", f"{alert.nws_severity} · {alert.urgency} · {alert.certainty}"
                            + (f" · response: {alert.response}" if alert.response else "")),
            ("Areas:     ", alert.area_desc or "-"),
            ("Issued:    ", f"{when(alert.sent)}   ({alert.message_type})"),
            ("Onset:     ", when(alert.onset)),
            ("Ends:      ", when(alert.active_until)),
            ("First:     ", ev.first_seen.astimezone().strftime("%Y-%m-%d %H:%M:%S")),
            ("Last:      ", f"{ev.last_seen.astimezone().strftime('%Y-%m-%d %H:%M:%S')}   Tally: {ev.tally}"),
            ("Flags:     ", flags or "-"),
            ("Link:      ", ev.article.link or "-"),
        ]
        if alert.nws_headline:
            rows.insert(1, ("Headline:  ", alert.nws_headline))
        if alert.zones:
            rows.append(("Zones:     ", f"{len(alert.zones)} ({', '.join(alert.zones[:6])}{'…' if len(alert.zones) > 6 else ''})"))
        return self._rows_text(rows)

    def _meta_text(self) -> Text:
        ev = self.event
        a = ev.article
        pub = a.published.astimezone().strftime("%Y-%m-%d %H:%M %Z") if a.published else "unknown"
        flags = " ".join(f for f, on in (("ACK", ev.acknowledged), ("READ", ev.read), ("NOTE", bool(ev.note))) if on)
        rows = [
            ("Node:      ", a.source),
            ("Published: ", pub),
            ("First:     ", ev.first_seen.astimezone().strftime("%Y-%m-%d %H:%M:%S")),
            ("Last:      ", f"{ev.last_seen.astimezone().strftime('%Y-%m-%d %H:%M:%S')}   Tally: {ev.tally}"),
            ("Flags:     ", flags or "-"),
            ("Link:      ", a.link or "-"),
        ]
        if self.article_status:
            rows.append(("Article:   ", self.article_status))
        if a.categories:
            rows.append(("Category:  ", ", ".join(a.categories)))
        kws = matched_keywords(a.title, a.summary)
        if kws:
            rows.append(("Triggers:  ", ", ".join(kws[:8])))
        return self._rows_text(rows)

    @staticmethod
    def _rows_text(rows: list[tuple[str, str]]) -> Text:
        text = Text()
        for i, (k, v) in enumerate(rows):
            if i:
                text.append("\n")
            text.append(k, style="bold")
            text.append(v, style="link " + v if k.startswith("Link") and v != "-" else "")
        return text

    def action_back(self) -> None:
        self.dismiss("back")

    def action_note(self) -> None:
        self.dismiss("note")

    def action_ack(self) -> None:
        self.dismiss("ack")

    def action_open(self) -> None:
        self.dismiss("open")

    def action_scroll_down(self) -> None:
        self.query_one(VerticalScroll).scroll_down()

    def action_scroll_up(self) -> None:
        self.query_one(VerticalScroll).scroll_up()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            event.stop()
            self.dismiss("back")


def blocks_text(blocks: Iterable[Block]) -> Text:
    """Article blocks as one Text: a blank line between blocks, list items one to a line."""
    text = Text()
    previous: Block | None = None
    for block in blocks:
        if previous is not None:
            text.append("\n" if block.kind == previous.kind == "li" else "\n\n")
        if block.kind == "h":
            text.append(block.text, style="bold")
        elif block.kind == "li":
            text.append("• " + block.text)
        elif block.kind == "quote":
            text.append(block.text, style="italic")
        else:
            text.append(block.text)
        previous = block
    return text


def _host(link: str) -> str:
    return urlsplit(link).hostname or "a local file"
