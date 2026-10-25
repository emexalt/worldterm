"""The Netcool-style event list: header lamps, full-row severity colours, selection,
batch acknowledge, context menu, reader and notes. The news list and the weather
list are both this screen over a different store."""
from __future__ import annotations

import shutil
import subprocess
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Static

from ..severity import Severity
from ..slug import note_filename, slugify
from ..store import Event, Store
from .dashboard import DashboardScreen
from .editor import NoteEditorScreen
from .menu import ContextMenu
from .reader import ReaderScreen
from .styles import SELECTED_MARK, clip, count_by_severity, fmt_time, lamp, severity_style, sort_events
from .table import EventTable

if TYPE_CHECKING:  # pragma: no cover
    from .app import WorldTerm

FILTERS: list[Severity | None] = [None, Severity.MINOR, Severity.MAJOR, Severity.CRITICAL]

CELL_PADDING = 2  # DataTable pads each cell by 1 on both sides
COLUMN_LABELS = {"Sel": ""}  # header text when it differs from the key

HELP_TEXT = """\
[b]worldterm[/b] - Netcool-style RSS event console

  ↑/↓ j/k          move               PgUp/PgDn   page
  Enter / m        context menu (same as right-click)
  Space            select / deselect row, move down
  Shift+↑/↓        extend selection
  Ctrl+A           select all visible (again: deselect all)
  Esc              clear selection
  Ctrl+click       select / deselect row
  Shift+click      select range from cursor
  a                acknowledge / unacknowledge
                   (all selected rows, or the cursor row)
  r                read article / alert
  n                create / edit note
  o                open link in browser
  R                refresh feeds and alerts now
  s                toggle sort (severity ↔ time)
  f                cycle severity filter
  x                hide / show acknowledged
  w                weather: NWS alerts as events (w again: back)
  d                dashboard: news and weather analytics (w: switch page)
  ?                this help          q  quit

Right-click a row: Acknowledge · Read · Create note
Right-click inside a selection to acknowledge all of it.

[dim]press any key to close[/dim]"""


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Static { width: auto; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT)

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.dismiss(None)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        self.dismiss(None)


class EventListScreen(Screen):
    """Base class: subclasses say which store they show and how a row is cut."""

    NAME = "WORLDTERM"
    DASHBOARD_PAGE = "news"
    # (label, width) — Summary gets whatever is left
    COLUMNS: list[tuple[str, int | None]] = [
        ("Sel", 1), ("Sev", 3), ("A", 1), ("R", 1), ("N", 1), ("Node", 12), ("Summary", None),
        ("Tally", 5), ("First", 11), ("Last", 11),
    ]
    LAMPS: tuple[Severity, ...] = (
        Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.WARNING, Severity.INDETERMINATE,
    )

    DEFAULT_CSS = """
    EventListScreen { layout: vertical; }
    EventListScreen #header { height: 1; background: $primary; color: $text; }
    EventListScreen EventTable { height: 1fr; }
    EventListScreen EventTable > .datatable--header { background: $surface; text-style: bold; }
    """

    BINDINGS = [
        Binding("enter,m", "menu", "Menu", priority=True),
        Binding("a", "ack", "Ack"),
        Binding("space", "toggle_select", "Select"),
        Binding("shift+down", "extend_select(1)", "Extend selection", show=False),
        Binding("shift+up", "extend_select(-1)", "Extend selection", show=False),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("escape", "clear_selection", "Clear selection", show=False),
        Binding("r", "read", "Read"),
        Binding("n", "note", "Note"),
        Binding("o", "open_link", "Open"),
        Binding("R", "refresh", "Refresh"),
        Binding("s", "toggle_sort", "Sort"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("x", "toggle_hide_acked", "Hide acked"),
        Binding("d", "dashboard", "Dashboard"),
        Binding("question_mark,h", "help", "Help"),
        Binding("q", "app.quit", "Quit"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "cursor_home", show=False),
        Binding("G", "cursor_end", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sort_by_time = False
        self.filter_idx = 0
        self.hide_acked = False
        self.selection: set[str] = set()  # guids of multi-selected events

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield EventTable(cursor_type="row", zebra_stripes=False, id="events")
        yield Footer()

    # ------------------------------------------------------------------ hooks for subclasses
    @property
    def store(self) -> Store:
        raise NotImplementedError

    def cells(self, ev: Event, summary_w: int) -> list[str]:
        return [
            SELECTED_MARK if ev.guid in self.selection else "",
            ev.severity.short, "A" if ev.acknowledged else "", "R" if ev.read else "", "N" if ev.note else "",
            clip(ev.node, 12), clip(ev.summary, summary_w), str(ev.tally).rjust(5),
            fmt_time(ev.first_seen), fmt_time(ev.last_seen),
        ]

    def header_left(self) -> Text:
        events_all = list(self.store.events.values())
        nodes = sorted({e.node for e in events_all})
        node = ", ".join(nodes) if len(nodes) <= 3 else f"{len(nodes)} sources"
        return Text.assemble(
            (f" {self.NAME} ", "bold"),
            (f" Node: {clip(node or '—', 30)}  Events: {self.table.row_count}  ", ""),
        )

    def header_extra(self, text: Text) -> None:
        """Append anything that goes after the severity lamps."""

    def refreshed_at(self) -> datetime | None:
        return self.wt.last_refresh

    def time_key(self, ev: Event) -> datetime:
        return ev.last_seen

    # ------------------------------------------------------------------ properties
    @property
    def wt(self) -> "WorldTerm":
        return self.app  # type: ignore[return-value]

    @property
    def table(self) -> EventTable:
        return self.query_one("#events", EventTable)

    @property
    def current(self) -> Event | None:
        table = self.table
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return self.store.events.get(str(key.value))

    def visible_events(self) -> list[Event]:
        min_sev = FILTERS[self.filter_idx]
        evs = list(self.store.events.values())
        if min_sev is not None:
            evs = [e for e in evs if e.severity >= min_sev]
        if self.hide_acked:
            evs = [e for e in evs if not e.acknowledged]
        return sort_events(evs, by_time=self.sort_by_time, when=self.time_key)

    # ------------------------------------------------------------------ lifecycle
    def on_mount(self) -> None:
        self._add_columns()
        self.set_interval(1.0, self.render_header)
        self.rebuild()
        self.table.focus()

    def on_resize(self, _: events.Resize) -> None:
        self.rebuild()

    # ------------------------------------------------------------------ table
    def _summary_width(self) -> int:
        # Size Summary so the columns fill the row exactly, leaving no
        # unstyled gap between the last column and the scrollbar.
        fixed = sum(w for _, w in self.COLUMNS if w is not None)
        padding = CELL_PADDING * len(self.COLUMNS)
        return max(10, self.table.row_area_width - fixed - padding)

    def _add_columns(self) -> None:
        table = self.table
        table.clear(columns=True)
        for key, width in self.COLUMNS:
            table.add_column(COLUMN_LABELS.get(key, key), width=width if width is not None else self._summary_width(),
                             key=key)

    def rebuild(self) -> None:
        """Repopulate the table from the store, preserving the cursor row."""
        table = self.table
        keep = self.current.guid if self.current else None
        summary_w = self._summary_width()
        self._add_columns()
        visible = self.visible_events()
        # Never keep hidden events selected: a batch action must only touch
        # rows the user can see.
        self.selection &= {ev.guid for ev in visible}
        table.set_row_styles([self._row_style(ev) for ev in visible])
        for ev in visible:
            # Unstyled text: the row style supplies the colour for the whole row.
            table.add_row(*(Text(c, no_wrap=True) for c in self.cells(ev, summary_w)), key=ev.guid)
        if keep is not None:
            try:
                table.move_cursor(row=table.get_row_index(keep), animate=False)
            except Exception:
                pass
        self.render_header()

    def _row_style(self, ev: Event) -> Style:
        return Style.parse(severity_style(ev.severity, ev.acknowledged, ev.new, ev.guid in self.selection))

    def refresh_rows(self, guids) -> None:
        """Restyle the given rows in place (selection/ack changes) without a rebuild."""
        table = self.table
        for guid in guids:
            ev = self.store.events.get(guid)
            if ev is None:
                continue
            try:
                index = table.get_row_index(guid)
            except Exception:  # row not visible
                continue
            table.set_row_style(index, self._row_style(ev))
            # update_cell bumps DataTable's update count, invalidating its caches.
            table.update_cell(guid, "Sel", Text(SELECTED_MARK if guid in self.selection else "", no_wrap=True))
            table.update_cell(guid, "A", Text("A" if ev.acknowledged else "", no_wrap=True))
        self.render_header()

    def render_header(self) -> None:
        wt = self.wt
        text = self.header_left()
        if self.selection:
            text.append(f" Selected: {len(self.selection)} ", style="reverse bold")
            text.append("  ")
        counts = count_by_severity(list(self.store.events.values()))
        for sev in self.LAMPS:
            text.append_text(lamp(sev, counts[sev]))
            text.append(" ")
        self.header_extra(text)
        tail: list[str] = []
        refreshed = self.refreshed_at()
        if refreshed:
            tail.append(f"Refreshed {refreshed.strftime('%H:%M:%S')}")
        remaining = max(0, int(wt.next_refresh_at - time.monotonic()))
        tail.append(f"next {remaining // 60}:{remaining % 60:02d}")
        if self.sort_by_time:
            tail.append("sort:time")
        f = FILTERS[self.filter_idx]
        if f is not None:
            tail.append(f"filter:≥{f.short}")
        if self.hide_acked:
            tail.append("ack:hidden")
        if wt.busy:
            tail.append("fetching…")
        tail_s = "  ".join(tail) + " "
        gap = self.size.width - text.cell_len - len(tail_s)
        if gap > 0:
            text.append(" " * gap)
        text.append(tail_s, style="dim" if not wt.busy else "bold")
        self.query_one("#header", Static).update(text)

    # ------------------------------------------------------------------ mouse
    def _row_at(self, screen_x: int, screen_y: int) -> int | None:
        table = self.table
        region = table.content_region
        if not region.contains(screen_x, screen_y):
            return None
        header = 1 if table.show_header else 0
        row = int(table.scroll_y) + (screen_y - region.y) - header
        return row if 0 <= row < table.row_count else None

    def _guid_at(self, row: int) -> str:
        return str(self.table.coordinate_to_cell_key((row, 0)).row_key.value)

    def on_click(self, event: events.Click) -> None:
        if event.button == 1 and self.query_one("#header").region.contains(event.screen_x, event.screen_y):
            event.stop()
            self.header_clicked(event.screen_x)

    def header_clicked(self, x: int) -> None:
        self.action_dashboard()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        row = self._row_at(event.screen_x, event.screen_y)
        if row is None:
            return
        if event.button == 1 and (event.ctrl or event.shift):
            # DataTable moves the cursor on the Click that follows, so the
            # cursor here is still the anchor for a shift-click range.
            if event.shift:
                lo, hi = sorted((self.table.cursor_row, row))
                self._select({self._guid_at(r) for r in range(lo, hi + 1)})
            else:
                self._toggle(self._guid_at(row))
            return
        if event.button != 3:
            return
        event.stop()
        guid = self._guid_at(row)
        if self.selection and guid not in self.selection:
            # Right-clicking outside the selection acts on that row alone.
            self.action_clear_selection()
        self.table.move_cursor(row=row, animate=False)
        self.open_menu(event.screen_x, event.screen_y + 1)

    # ------------------------------------------------------------------ actions
    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()

    def action_cursor_home(self) -> None:
        self.table.move_cursor(row=0, animate=False)

    def action_cursor_end(self) -> None:
        self.table.move_cursor(row=self.table.row_count - 1, animate=False)

    def action_menu(self) -> None:
        ev = self.current
        if ev is None:
            return
        table = self.table
        row_y = table.content_region.y + (1 if table.show_header else 0) + table.cursor_row - int(table.scroll_y)
        self.open_menu(table.content_region.x + 6, row_y + 1)

    def open_menu(self, x: int, y: int) -> None:
        ev = self.current
        if ev is None:
            return
        targets = self.ack_targets()
        verb = "Unacknowledge" if all(t.acknowledged for t in targets) else "Acknowledge"
        ack_label = f"{verb} {len(targets)} selected" if self.selection else verb
        items = [
            ("ack", ack_label),
            ("read", "Read"),
            ("note", "Edit note" if ev.note else "Create note"),
            ("open", "Open link in browser"),
        ]
        if self.selection:
            items.append(("deselect", "Deselect all"))
        items.append(("cancel", "Cancel"))

        def _chosen(choice: str | None) -> None:
            if choice == "ack":
                self.action_ack()
            elif choice == "read":
                self.action_read()
            elif choice == "note":
                self.action_note()
            elif choice == "open":
                self.action_open_link()
            elif choice == "deselect":
                self.action_clear_selection()

        self.app.push_screen(ContextMenu(items, x, y), _chosen)

    def ack_targets(self) -> list[Event]:
        """The selected events in display order, or the cursor row if none are selected."""
        if self.selection:
            return [ev for ev in self.visible_events() if ev.guid in self.selection]
        return [self.current] if self.current else []

    def action_ack(self) -> None:
        self.acknowledge(self.ack_targets())

    def acknowledge(self, targets: list[Event]) -> None:
        """Acknowledge every target; if all are already acknowledged, unacknowledge them.

        The selection is kept, so pressing the key again undoes a batch.
        """
        if not targets:
            return
        value = not all(ev.acknowledged for ev in targets)
        for ev in targets:
            self.store.ack(ev.guid, value)
            ev.new = False
        self.wt.save()
        verb = "Acknowledged" if value else "Unacknowledged"
        if len(targets) == 1:
            self.notify(f"{verb}: {clip(targets[0].summary, 60)}")
        else:
            self.notify(f"{verb} {len(targets)} events")
        if self.hide_acked:
            self.rebuild()  # acknowledged rows disappear
        else:
            self.refresh_rows([ev.guid for ev in targets])

    # ------------------------------------------------------------------ selection
    def _toggle(self, guid: str) -> None:
        self.selection ^= {guid}
        self.refresh_rows([guid])

    def _select(self, guids: set[str]) -> None:
        added = guids - self.selection
        self.selection |= guids
        self.refresh_rows(added)

    def action_toggle_select(self) -> None:
        ev = self.current
        if ev is None:
            return
        self._toggle(ev.guid)
        self.table.action_cursor_down()

    def action_extend_select(self, direction: int) -> None:
        table = self.table
        if self.current is None:
            return
        start = table.cursor_row
        end = max(0, min(table.row_count - 1, start + direction))
        self._select({self._guid_at(start), self._guid_at(end)})
        table.move_cursor(row=end, animate=False)

    def action_select_all(self) -> None:
        visible = {ev.guid for ev in self.visible_events()}
        if visible and visible <= self.selection:
            self.action_clear_selection()
        else:
            self._select(visible)

    def action_clear_selection(self) -> None:
        if not self.selection:
            return
        cleared = self.selection
        self.selection = set()
        self.refresh_rows(cleared)

    def action_read(self) -> None:
        ev = self.current
        if ev is None:
            return
        self.store.mark_read(ev.guid)
        ev.new = False
        self.wt.save()

        def _done(result: str | None) -> None:
            if result == "note":
                self.action_note()
            elif result == "ack":
                self.acknowledge([ev])
            elif result == "open":
                self.action_open_link()
            self.rebuild()

        self.app.push_screen(ReaderScreen(ev), _done)

    def action_note(self) -> None:
        ev = self.current
        if ev is None:
            return
        store = self.store
        existing = store.read_note(ev)
        if existing is not None and ev.note:
            filename = Path(ev.note).name
            initial = existing
        else:
            filename = note_filename(ev.article.title, ev.first_seen.astimezone())
            initial = note_template(ev)

        def _saved(text: str | None) -> None:
            if text is None:
                self.notify("Note cancelled", severity="warning")
                return
            try:
                path = store.write_note(ev, filename, text)
            except OSError as exc:
                self.notify(f"Cannot write note: {exc}", severity="error")
                return
            self.wt.save()
            self.notify(f"Note saved: {path}")
            self.rebuild()

        self.app.push_screen(NoteEditorScreen(initial, filename), _saved)

    def action_open_link(self) -> None:
        ev = self.current
        if ev is None or not ev.article.link:
            self.notify("No link for this event", severity="warning")
            return
        link = ev.article.link
        opener = shutil.which("xdg-open")
        try:
            if opener:
                subprocess.Popen([opener, link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            else:
                webbrowser.open(link)
            self.notify(f"Opened {clip(link, 60)}")
        except OSError as exc:
            self.notify(f"Cannot open browser: {exc}", severity="error")

    def action_refresh(self) -> None:
        self.wt.start_fetch()

    def action_toggle_sort(self) -> None:
        self.sort_by_time = not self.sort_by_time
        self.rebuild()

    def action_cycle_filter(self) -> None:
        self.filter_idx = (self.filter_idx + 1) % len(FILTERS)
        self.rebuild()

    def action_toggle_hide_acked(self) -> None:
        self.hide_acked = not self.hide_acked
        self.rebuild()

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_dashboard(self) -> None:
        self.app.push_screen(DashboardScreen(page=self.DASHBOARD_PAGE))


def note_template(ev: Event) -> str:
    a = ev.article
    pub = a.published.astimezone().isoformat(timespec="minutes") if a.published else "unknown"
    alert = ev.alert
    if alert is not None:
        def when(dt: datetime | None) -> str:
            return dt.astimezone().isoformat(timespec="minutes") if dt else "unknown"
        return (
            f"# {slugify(a.title)}\n"
            f"\n"
            f"Alert:     {alert.event}\n"
            f"Office:    {alert.sender}\n"
            f"Severity:  {ev.severity.label} (NWS {alert.nws_severity}, {alert.urgency}, {alert.certainty})\n"
            f"Areas:     {alert.area_desc}\n"
            f"Issued:    {when(alert.sent)}\n"
            f"Ends:      {when(alert.active_until)}\n"
            f"Headline:  {alert.nws_headline or alert.headline}\n"
            f"Link:      {a.link}\n"
            f"\n"
        )
    return (
        f"# {slugify(a.title)}\n"
        f"\n"
        f"Title:    {a.title}\n"
        f"Source:   {a.source}\n"
        f"Severity: {ev.severity.label}\n"
        f"Date:     {pub}\n"
        f"Link:     {a.link}\n"
        f"\n"
    )
