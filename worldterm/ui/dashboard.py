"""Dashboard screen: a news page (counts by severity and source, trigger analytics)
and a weather page (NWS alerts by severity, state or office, event type and timing)."""
from __future__ import annotations

from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from .. import analytics
from ..analytics import SOURCE_SORTS, TRIGGER_SORTS, TYPE_SORTS, Summary
from ..severity import Severity, display_keyword
from . import weatherpanels as wx
from .charts import (MUTED, StackedBar, bg, chip, group_table, mix_legend, num, severity_legend,
                     severity_table)
from .styles import ACK_STYLE, SEVERITY_STYLE

NARROW_BELOW = 100  # columns; below this the panels stack vertically
PAGES: tuple[str, ...] = ("news", "weather")
GROUPINGS: tuple[str, ...] = ("state", "office")


# --------------------------------------------------------------------------- news panels

def trigger_table(rows: list[analytics.TriggerRow], sort: str) -> Table:
    scale = max((r.events for r in rows), default=1)
    t = Table(expand=True, box=None, padding=(0, 1), header_style="bold", show_edge=False)
    t.add_column("Trigger", no_wrap=True, min_width=10)
    t.add_column("Level" + (" ▾" if sort == "level" else ""), no_wrap=True)
    t.add_column("Events" + (" ▾" if sort == "events" else ""), justify="right", no_wrap=True)
    t.add_column("Decided" + (" ▾" if sort == "deciding" else ""), justify="right", no_wrap=True)
    t.add_column("", ratio=1, min_width=8)
    for r in rows:
        bar = StackedBar(
            [(r.deciding, bg(SEVERITY_STYLE[r.level])), (r.outranked, bg(ACK_STYLE[r.level]))],
            scale=scale,
        )
        t.add_row(Text(display_keyword(r.keyword)), chip(r.level), num(r.events), num(r.deciding), bar)
    return t


def trigger_summary(s: Summary, levels: dict[Severity, tuple[int, int]]) -> Table:
    """Short rows that fit a half-width panel without wrapping mid-phrase."""
    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(no_wrap=True, style="bold")
    grid.add_column(ratio=1)

    assigned = Text(f"keyword {s.by_rule} ({s.pct(s.by_rule)}%)")
    assigned.append(" · ", style=MUTED)
    assigned.append(f"tag {s.by_category}")
    assigned.append(" · ", style=MUTED)
    assigned.append(f"none → Warning {s.by_default} ({s.pct(s.by_default)}%)")
    if s.empty:
        assigned.append(" · ", style=MUTED)
        assigned.append(f"no text {s.empty}")
    grid.add_row("Severity set by", assigned)

    mods = Text(f"Live/Breaking ↑ {s.bumped_up}")
    mods.append(" · ", style=MUTED)
    mods.append(f"Analysis/Opinion ↓ {s.bumped_down}")
    grid.add_row("Modifiers", mods)

    lv = Text()
    for i, (sev, (keywords, deciding)) in enumerate(levels.items()):
        if i:
            lv.append("\n")
        lv.append_text(chip(sev))
        lv.append(f" {keywords} keywords fired, decided {deciding} events")
    if not levels:
        lv.append("no keyword has fired yet", style=MUTED)
    grid.add_row("Rule levels", lv)

    legend = Text()
    legend.append("  ", style=bg(SEVERITY_STYLE[Severity.MAJOR]))
    legend.append(" set severity   ", style=MUTED)
    legend.append("  ", style=bg(ACK_STYLE[Severity.MAJOR]))
    legend.append(" outranked by higher level", style=MUTED)
    grid.add_row(Text("Bar", style=MUTED), legend)
    return grid


# --------------------------------------------------------------------------- screen

class DashboardScreen(Screen[None]):
    DEFAULT_CSS = """
    DashboardScreen { layout: vertical; }
    DashboardScreen #bar { height: 1; background: $primary; color: $text; text-style: bold; padding: 0 1; }
    DashboardScreen .body {
        height: 1fr;
        overflow-y: hidden;
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 1fr;
        grid-rows: auto 1fr;
    }
    DashboardScreen #wx-body { grid-size: 2 3; grid-rows: auto 1fr 1fr; }
    DashboardScreen .body.hidden { display: none; }
    DashboardScreen .panel {
        border: round $accent;
        border-title-color: $text;
        border-title-style: bold;
        padding: 0 1;
    }
    DashboardScreen .panel:focus { border: round $primary; }
    DashboardScreen #sev-panel, DashboardScreen #wx-sev-panel { height: auto; }
    DashboardScreen #src-panel { row-span: 2; height: 1fr; }
    DashboardScreen #wx-state-panel { row-span: 3; height: 1fr; }
    DashboardScreen #trig-panel, DashboardScreen #wx-type-panel, DashboardScreen #wx-time-panel { height: 1fr; }
    DashboardScreen .legend { color: $text-muted; margin-top: 1; height: auto; }
    DashboardScreen #trig-summary { margin-bottom: 1; height: auto; }
    DashboardScreen #wx-kinds { margin-top: 1; height: auto; }

    /* Narrow terminals: stack the panels and scroll the body as a whole. */
    DashboardScreen .body.narrow { layout: vertical; overflow-y: auto; }
    DashboardScreen .body.narrow .panel,
    DashboardScreen .body.narrow #src-panel, DashboardScreen .body.narrow #trig-panel,
    DashboardScreen .body.narrow #wx-state-panel, DashboardScreen .body.narrow #wx-type-panel,
    DashboardScreen .body.narrow #wx-time-panel { height: auto; }
    DashboardScreen .body.narrow:focus { border-left: thick $primary; }
    """

    BINDINGS = [
        Binding("escape,q,d", "back", "Back"),
        Binding("w,tab", "toggle_page", "News/Weather"),
        Binding("R", "app.start_fetch", "Refresh"),
        Binding("s", "cycle_source_sort", "Group sort"),
        Binding("t", "cycle_trigger_sort", "Type sort"),
        Binding("o", "toggle_grouping", "State/Office"),
        Binding("question_mark,h", "app.help", "Help", show=False),
    ]

    def __init__(self, page: str = "news") -> None:
        super().__init__()
        self.page = page if page in PAGES else "news"
        self.source_sort = SOURCE_SORTS[0]
        self.trigger_sort = TRIGGER_SORTS[0]
        self.type_sort = TYPE_SORTS[0]
        self.grouping = GROUPINGS[0]

    def compose(self) -> ComposeResult:
        yield Static("", id="bar")
        with VerticalScroll(id="body", classes="body"):
            with Container(id="sev-panel", classes="panel") as sev_panel:
                sev_panel.border_title = "By severity"
                yield Static("", id="sev")
                yield Static(severity_legend(), classes="legend")
            with VerticalScroll(id="src-panel", classes="panel") as src_panel:
                src_panel.border_title = "By source"
                yield Static("", id="src")
                yield Static(mix_legend(), classes="legend")
            with VerticalScroll(id="trig-panel", classes="panel") as trig_panel:
                trig_panel.border_title = "Triggers"
                yield Static("", id="trig-summary")
                yield Static("", id="trig")
        with VerticalScroll(id="wx-body", classes="body hidden"):
            with Container(id="wx-sev-panel", classes="panel") as wx_sev:
                wx_sev.border_title = "Alerts by severity"
                yield Static("", id="wx-sev")
                yield Static("", id="wx-kinds")
                yield Static(severity_legend(), classes="legend")
            with VerticalScroll(id="wx-state-panel", classes="panel") as wx_state:
                wx_state.border_title = "By state"
                yield Static("", id="wx-state")
                yield Static(wx.weather_legend(), classes="legend")
            with VerticalScroll(id="wx-type-panel", classes="panel") as wx_type:
                wx_type.border_title = "Event types"
                yield Static("", id="wx-type")
            with VerticalScroll(id="wx-time-panel", classes="panel") as wx_time:
                wx_time.border_title = "Timing and urgency"
                yield Static("", id="wx-time")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_layout(self.size.width)
        self._show_page(focus=True)
        self.refresh_data()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(event.size.width)

    # ------------------------------------------------------------------ layout
    @property
    def body(self) -> VerticalScroll:
        return self.query_one("#wx-body" if self.page == "weather" else "#body", VerticalScroll)

    def _scroller(self) -> str:
        return "#wx-state-panel" if self.page == "weather" else "#src-panel"

    def _apply_layout(self, width: int) -> None:
        narrow = width < NARROW_BELOW
        changed = False
        for body in self.query(".body"):
            changed = changed or body.has_class("narrow") != narrow
            body.set_class(narrow, "narrow")
            # When stacked, the body is the only scroller, so it takes the focus
            # (and the arrow keys); otherwise the scrolling panels do.
            body.can_focus = narrow
        if changed:
            self._focus_page()

    def _focus_page(self) -> None:
        body = self.body
        (body if body.has_class("narrow") else self.query_one(self._scroller())).focus()

    def _show_page(self, focus: bool = False) -> None:
        for body in self.query(".body"):
            body.set_class(body.id != ("wx-body" if self.page == "weather" else "body"), "hidden")
        if focus:
            self._focus_page()

    # ------------------------------------------------------------------ data
    def refresh_data(self) -> None:
        if self.page == "weather":
            self._refresh_weather()
        else:
            self._refresh_news()

    def _refresh_news(self) -> None:
        app = self.app
        evs = list(app.store.events.values())  # type: ignore[attr-defined]
        s = analytics.summary(evs)
        sev_rows = analytics.severity_rows(evs)
        src_rows = analytics.source_rows(evs, self.source_sort)
        trig_rows = analytics.trigger_rows(evs, self.trigger_sort)

        bar = (f" DASHBOARD · NEWS  {s.total:,} events · {s.sources} sources · "
               f"{s.acknowledged:,} acknowledged ({s.pct(s.acknowledged)}%) · {s.read:,} read · {s.noted:,} notes"
               f" · refreshed {self._when(getattr(app, 'last_refresh', None))}")
        self.query_one("#bar", Static).update(bar)
        self.query_one("#sev", Static).update(severity_table(sev_rows))
        self.query_one("#src", Static).update(group_table(src_rows, self.source_sort))
        self.query_one("#src-panel").border_title = f"By source · sorted by {self.source_sort} (s)"
        self.query_one("#trig-summary", Static).update(trigger_summary(s, analytics.level_totals(trig_rows)))
        self.query_one("#trig", Static).update(
            trigger_table(trig_rows, self.trigger_sort) if trig_rows else Text("No keyword has fired yet.", style=MUTED)
        )
        self.query_one("#trig-panel").border_title = (
            f"Triggers · {s.distinct_triggers} keywords · sorted by {self.trigger_sort} (t)"
        )

    def _refresh_weather(self) -> None:
        app = self.app
        store = getattr(app, "weather_store", None)
        evs = list(store.events.values()) if store is not None else []
        s = analytics.weather_summary(evs)
        sev_rows = analytics.severity_rows(evs)
        if self.grouping == "office":
            grp_rows = analytics.office_rows(evs, self.source_sort)
        else:
            grp_rows = analytics.state_rows(evs, self.source_sort)
        type_rows = analytics.type_rows(evs, self.type_sort)
        buckets, older = analytics.issued_histogram(evs)
        expiry = analytics.expiry_buckets(evs)
        urgencies, certainties, grid = analytics.urgency_certainty(evs)
        responses = analytics.response_counts(evs)

        enabled = getattr(app, "weather_url", None) is not None
        if not enabled:
            bar = " DASHBOARD · WEATHER  weather alerts are off (run with --weather us or --weather MA,RI)"
        else:
            bar = (f" DASHBOARD · WEATHER  {s.active:,} active alerts"
                   + (f" · {s.ended:,} ended" if s.ended else "")
                   + f" · {s.states} states · {s.offices} offices · {s.types} event types · "
                   f"{s.acknowledged:,} acknowledged ({s.pct(s.acknowledged)}%)"
                   f" · refreshed {self._when(getattr(app, 'weather_refreshed', None))}")
        self.query_one("#bar", Static).update(bar)
        self.query_one("#wx-sev", Static).update(
            severity_table(sev_rows, wx.nws_label) if evs else Text("No weather alerts loaded.", style=MUTED)
        )
        self.query_one("#wx-kinds", Static).update(wx.kind_line(analytics.kind_counts(evs)))
        self.query_one("#wx-state", Static).update(
            group_table(grp_rows, self.source_sort, header=self.grouping.title(), name_width=12)
        )
        other = "office" if self.grouping == "state" else "state"
        self.query_one("#wx-state-panel").border_title = (
            f"By {self.grouping} · sorted by {self.source_sort} (s) · by {other} (o)"
        )
        self.query_one("#wx-type", Static).update(
            wx.type_table(type_rows, self.type_sort) if type_rows else Text("No alerts.", style=MUTED)
        )
        self.query_one("#wx-type-panel").border_title = (
            f"Event types · {len(type_rows)} firing · sorted by {self.type_sort} (t)"
        )
        self.query_one("#wx-time", Static).update(
            wx.timing_panel(buckets, older, expiry, urgencies, certainties, grid, responses, s)
        )

    @staticmethod
    def _when(refreshed) -> str:
        return refreshed.strftime("%H:%M:%S") if isinstance(refreshed, datetime) else "—"

    # ------------------------------------------------------------------ actions
    def action_back(self) -> None:
        self.dismiss(None)

    def action_toggle_page(self) -> None:
        self.page = "weather" if self.page == "news" else "news"
        self._show_page(focus=True)
        self.refresh_data()

    def action_cycle_source_sort(self) -> None:
        i = SOURCE_SORTS.index(self.source_sort)
        self.source_sort = SOURCE_SORTS[(i + 1) % len(SOURCE_SORTS)]
        self.refresh_data()

    def action_cycle_trigger_sort(self) -> None:
        if self.page == "weather":
            i = TYPE_SORTS.index(self.type_sort)
            self.type_sort = TYPE_SORTS[(i + 1) % len(TYPE_SORTS)]
        else:
            i = TRIGGER_SORTS.index(self.trigger_sort)
            self.trigger_sort = TRIGGER_SORTS[(i + 1) % len(TRIGGER_SORTS)]
        self.refresh_data()

    def action_toggle_grouping(self) -> None:
        if self.page != "weather":
            return
        i = GROUPINGS.index(self.grouping)
        self.grouping = GROUPINGS[(i + 1) % len(GROUPINGS)]
        self.refresh_data()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            event.stop()
            self.dismiss(None)
