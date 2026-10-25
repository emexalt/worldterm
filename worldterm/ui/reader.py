"""Article detail screen (Netcool: event information)."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ..severity import matched_keywords
from ..store import Event
from .styles import SEVERITY_STYLE


class ReaderScreen(Screen[str]):
    """Dismisses with 'back', 'note', 'ack' or 'open'."""

    DEFAULT_CSS = """
    ReaderScreen { layout: vertical; }
    ReaderScreen #bar { height: 1; background: $primary; color: $text; text-style: bold; padding: 0 1; }
    ReaderScreen VerticalScroll { padding: 1 2; }
    ReaderScreen #title { text-style: bold; color: $warning; margin-bottom: 1; }
    ReaderScreen #sev { width: auto; margin-bottom: 1; }
    ReaderScreen #meta { margin-bottom: 1; }
    ReaderScreen #body { }
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

    def __init__(self, event: Event) -> None:
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        ev = self.event
        a = ev.article
        alert = ev.alert
        yield Static(f" READ  {ev.node}", id="bar")
        with VerticalScroll():
            yield Static((alert.event if alert else a.title) or "(no title)", id="title")
            yield Static(Text(f" {ev.severity.label.upper()} ", style=SEVERITY_STYLE[ev.severity]), id="sev")
            yield Static(self._alert_meta() if alert else self._meta_text(), id="meta")
            yield Static(self._body_text(), id="body")
            if ev.note:
                yield Static(f"Note: {ev.note}", id="note")
        yield Footer()

    def _body_text(self) -> str:
        ev = self.event
        alert = ev.alert
        if alert is None:
            return ev.article.summary or "(no summary)"
        parts = [alert.description or "(no description)"]
        if alert.instruction:
            parts.append("PRECAUTIONARY/PREPAREDNESS ACTIONS\n\n" + alert.instruction)
        return "\n\n".join(parts)

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
