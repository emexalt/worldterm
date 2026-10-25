"""Severity → Rich style mapping and small formatting helpers."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from rich.text import Text

from ..severity import Severity
from ..store import Event

# The hex values live in palette.py so the wall page can use them too; they are
# re-exported here for the UI modules and the tests that import them from styles.
from ..palette import ACK_DARKEN, ACK_FOREGROUND, SEVERITY_COLORS, darken  # noqa: F401,E402


SEVERITY_STYLE: dict[Severity, str] = {
    sev: f"{'bold ' if sev == Severity.CRITICAL else ''}{fg} on {bg}"
    for sev, (bg, fg) in SEVERITY_COLORS.items()
}
ACK_STYLE: dict[Severity, str] = {
    sev: f"{ACK_FOREGROUND} on {darken(bg)}" for sev, (bg, _fg) in SEVERITY_COLORS.items()
}

SELECTED_MARK = "●"


def severity_style(sev: Severity, acknowledged: bool = False, new: bool = False,
                   selected: bool = False) -> str:
    """Rich style for a whole event row."""
    style = ACK_STYLE[sev] if acknowledged else SEVERITY_STYLE[sev]
    if new and not acknowledged:
        style += " bold"
    if selected:
        # Underline runs across the padding too, so a selected row reads as one band.
        style += " underline"
    return style


def fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    local = dt.astimezone()
    if local.date() == datetime.now().date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%m-%d %H:%M")


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def sort_events(events: list[Event], by_time: bool = False,
                when: Callable[[Event], datetime] = lambda e: e.last_seen) -> list[Event]:
    """Severity then time (newest first), or time only; *when* picks the timestamp."""
    if by_time:
        return sorted(events, key=when, reverse=True)
    return sorted(events, key=lambda e: (int(e.severity), when(e)), reverse=True)


def count_by_severity(events: list[Event]) -> dict[Severity, int]:
    counts = {s: 0 for s in Severity}
    for ev in events:
        counts[ev.severity] += 1
    return counts


def lamp(sev: Severity, count: int) -> Text:
    """Netcool-style severity counter chip."""
    style = SEVERITY_STYLE[sev] if count else "dim"
    return Text(f" {sev.short} {count} ", style=style)
