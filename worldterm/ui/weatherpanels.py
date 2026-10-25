"""Renderables for the dashboard's weather page (NWS alert analytics)."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text

from .. import analytics
from ..nws import NWS_LABEL
from ..severity import Severity
from .charts import MUTED, StackedBar, bg, chip, dots, num, pct, sparkline
from .styles import ACK_STYLE, SEVERITY_STYLE, clip

# Colour for each "ends within" bucket: sooner is hotter, ended is green (Clear).
EXPIRY_STYLES: dict[str, str] = {
    "ended": SEVERITY_STYLE[Severity.CLEAR],
    "≤ 1h": SEVERITY_STYLE[Severity.MAJOR],
    "≤ 6h": SEVERITY_STYLE[Severity.MINOR],
    "≤ 24h": SEVERITY_STYLE[Severity.WARNING],
    "> 24h": ACK_STYLE[Severity.WARNING],
}


def nws_label(sev: Severity) -> str:
    """'Severe → Major': the NWS word and the Netcool level it lands on."""
    return f"{NWS_LABEL.get(sev, sev.label)} → {sev.label}"


def kind_line(kinds: list[tuple[str, int]]) -> Text:
    plural = {"Warning": "Warnings", "Watch": "Watches", "Advisory": "Advisories",
              "Statement": "Statements", "Outlook": "Outlooks", "Other": "Other"}
    text = Text("Kinds: ", style="bold")
    text.append_text(dots([(plural.get(k, k), n) for k, n in kinds], empty="no alerts"))
    return text


def type_table(rows: list[analytics.TypeRow], sort: str, limit: int = 40) -> Table:
    scale = max((r.total for r in rows), default=1)
    t = Table(expand=True, box=None, padding=(0, 1), collapse_padding=True, pad_edge=False,
              header_style="bold", show_edge=False)
    t.add_column("Event type" + (" ▾" if sort == "name" else ""), no_wrap=True, min_width=12)
    t.add_column("Level" + (" ▾" if sort == "level" else ""), no_wrap=True)
    t.add_column("Alerts" + (" ▾" if sort == "count" else ""), justify="right", no_wrap=True)
    t.add_column("States", justify="right", no_wrap=True)
    t.add_column("", ratio=1, min_width=8)
    for r in rows[:limit]:
        parts = [(r.by_severity[sev], bg(SEVERITY_STYLE[sev])) for sev in analytics.SEVERITY_ORDER]
        t.add_row(Text(clip(r.event, 24)), chip(r.level), num(r.total), num(len(r.states)),
                  StackedBar(parts, scale=scale))
    if len(rows) > limit:
        t.add_row(Text(f"… {len(rows) - limit} more", style=MUTED), "", "", "", "")
    return t


def issued_line(buckets: list[int], older: int) -> Text:
    """Hourly issuance sparkline for the last 24h, with its scale on a second line."""
    peak = max(buckets, default=0)
    text = Text()
    text.append("24h ago ", style=MUTED)
    text.append_text(sparkline(buckets, style="bold " + SEVERITY_STYLE[Severity.WARNING].rsplit(" on ", 1)[1]))
    text.append(" now", style=MUTED)
    text.append(f"\npeak {peak}/h · {sum(buckets)} issued in the last 24h", style=MUTED)
    if older:
        text.append(f" · {older} older", style=MUTED)
    return text


def expiry_table(buckets: list[tuple[str, int]]) -> Table:
    scale = max((n for _, n in buckets), default=1)
    total = sum(n for _, n in buckets)
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(no_wrap=True, min_width=6)
    t.add_column(justify="right", no_wrap=True)
    t.add_column(justify="right", no_wrap=True)
    t.add_column(ratio=1, min_width=8)
    for label, n in buckets:
        t.add_row(Text(label), num(n), pct(n, total), StackedBar([(n, bg(EXPIRY_STYLES[label]))], scale=scale))
    return t


def urgency_table(urgencies: list[str], certainties: list[str], grid: dict[tuple[str, str], int]) -> Table:
    """Urgency down the side, certainty across: how imminent and how sure."""
    t = Table(expand=False, box=None, padding=(0, 1), collapse_padding=True, pad_edge=False,
              header_style="bold", show_edge=False)
    t.add_column("", no_wrap=True, min_width=9)
    for c in certainties:
        t.add_column(c, justify="right", no_wrap=True)
    t.add_column("All", justify="right", no_wrap=True, style="bold")
    peak = max(grid.values(), default=0)
    for u in urgencies:
        cells: list = [Text(u)]
        row_total = 0
        for c in certainties:
            n = grid.get((u, c), 0)
            row_total += n
            cells.append(Text(f"{n:,}", style="bold reverse" if n and n == peak else ("" if n else MUTED)))
        cells.append(Text(f"{row_total:,}"))
        t.add_row(*cells)
    if not urgencies:
        t.add_row(Text("no active alerts", style=MUTED))
    return t


def timing_panel(buckets: list[int], older: int, expiry: list[tuple[str, int]],
                 urgencies: list[str], certainties: list[str], grid: dict[tuple[str, str], int],
                 responses: list[tuple[str, int]], s: analytics.WeatherSummary) -> Table:
    outer = Table.grid(padding=(0, 2), expand=True)
    outer.add_column(no_wrap=True, style="bold")
    outer.add_column(ratio=1)
    outer.add_row("Issued", issued_line(buckets, older))
    outer.add_row("Ends in", expiry_table(expiry))
    outer.add_row("Urgency ↓", urgency_table(urgencies, certainties, grid))
    outer.add_row("Response", dots(responses, empty="no active alerts"))
    msgs = dots([("new", s.total - s.updates), ("updates", s.updates)])
    msgs.append(" · ", style=MUTED)
    msgs.append(f"immediate {s.immediate:,}")
    msgs.append(" · ", style=MUTED)
    msgs.append(f"observed {s.observed:,}")
    outer.add_row("Messages", msgs)
    return outer


def weather_legend() -> Text:
    text = Text("Bar: ", style=MUTED)
    for sev in (Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.WARNING, Severity.INDETERMINATE,
                Severity.CLEAR):
        text.append("  ", style=bg(SEVERITY_STYLE[sev]))
        text.append(f" {NWS_LABEL[sev]}  ", style=MUTED)
    return text
