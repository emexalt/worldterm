"""Rich renderables shared by the dashboard pages: bars, chips, sparklines, tables."""
from __future__ import annotations

from typing import Callable

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .. import analytics
from ..severity import Severity
from .styles import ACK_STYLE, SEVERITY_STYLE, clip

MUTED = "dim"
LAMP_LEVELS: tuple[Severity, ...] = (
    Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.WARNING, Severity.INDETERMINATE,
)
SPARK = "▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------- bar

class StackedBar:
    """A horizontal bar made of coloured segments that fills the width it is given.

    `parts` are (value, style) pairs drawn left to right; `scale` is the value
    that maps to the full width, so bars in one chart share an axis. Every
    non-zero part gets at least one cell so small counts stay visible.
    """

    def __init__(self, parts: list[tuple[float, str]], scale: float, min_width: int = 6) -> None:
        self.parts = [(v, s) for v, s in parts if v > 0]
        self.scale = max(scale, 1e-9)
        self.min_width = min_width

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(self.min_width, options.max_width)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(1, options.max_width)
        total = sum(v for v, _ in self.parts)
        if total <= 0:
            yield Segment(" " * width)
            yield Segment.line()
            return
        cells = max(len(self.parts), min(width, round(width * total / self.scale)))
        cells = min(cells, width)
        # Largest-remainder allocation, with a floor of one cell per part.
        exact = [cells * v / total for v, _ in self.parts]
        alloc = [max(1, int(x)) for x in exact]
        while sum(alloc) > cells:
            i = max(range(len(alloc)), key=lambda k: alloc[k])
            if alloc[i] <= 1:
                break
            alloc[i] -= 1
        remainders = sorted(range(len(alloc)), key=lambda k: exact[k] - int(exact[k]), reverse=True)
        for i in remainders:
            if sum(alloc) >= cells:
                break
            alloc[i] += 1
        drawn = 0
        for (_, style), n in zip(self.parts, alloc):
            if n <= 0:
                continue
            yield Segment(" " * n, Style.parse(style))
            drawn += n
        if drawn < width:
            yield Segment(" " * (width - drawn))
        yield Segment.line()


def bg(style: str) -> str:
    """'fg on bg' -> 'on bg' (bars are colour only, no text)."""
    return "on " + style.rsplit(" on ", 1)[1]


def chip(sev: Severity) -> Text:
    return Text(f" {sev.short} ", style=SEVERITY_STYLE[sev])


def num(n: int, style: str = "") -> Text:
    return Text(f"{n:,}", style=style or ("" if n else MUTED), justify="right")


def pct(part: int, whole: int) -> Text:
    return Text(f"{round(100 * part / whole) if whole else 0:3d}%", style=MUTED, justify="right")


def sparkline(values: list[int], style: str = "bold") -> Text:
    """One block character per value, scaled to the largest; zeros are a dim baseline."""
    peak = max(values, default=0)
    text = Text()
    for v in values:
        if v <= 0 or peak <= 0:
            text.append(SPARK[0], style=MUTED)
        else:
            idx = min(len(SPARK) - 1, max(0, round((len(SPARK) - 1) * v / peak)))
            text.append(SPARK[idx], style=style)
    return text


# --------------------------------------------------------------------------- tables

def severity_table(rows: list[analytics.SeverityRow],
                   label: Callable[[Severity], str] = lambda sev: sev.label) -> Table:
    """One bar per level: outstanding in the level's colour, acknowledged darker."""
    scale = max((r.total for r in rows), default=1)
    total = sum(r.total for r in rows)
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(no_wrap=True)                       # chip
    t.add_column(no_wrap=True, min_width=9)          # label
    t.add_column(justify="right", no_wrap=True)      # count
    t.add_column(justify="right", no_wrap=True)      # pct
    t.add_column(ratio=1, min_width=8)               # bar
    t.add_column(justify="right", no_wrap=True)      # acked
    for r in rows:
        bar = StackedBar(
            [(r.outstanding, bg(SEVERITY_STYLE[r.severity])), (r.acknowledged, bg(ACK_STYLE[r.severity]))],
            scale=scale,
        )
        t.add_row(chip(r.severity), Text(label(r.severity)), num(r.total), pct(r.total, total), bar,
                  Text(f"{r.acknowledged} ack", style=MUTED) if r.acknowledged else Text(""))
    return t


def group_table(rows: list[analytics.SourceRow], sort: str, header: str = "Source", name_width: int = 14) -> Table:
    """Groups against severities: a Netcool lamp per count, ack %, stacked mix bar."""
    scale = max((r.total for r in rows), default=1)
    t = Table(expand=True, box=None, padding=(0, 1), collapse_padding=True, pad_edge=False,
              header_style="bold", show_edge=False)
    t.add_column(header + (" ▾" if sort == "name" else ""), no_wrap=True, min_width=8)
    t.add_column("Total" + (" ▾" if sort == "total" else ""), justify="right", no_wrap=True)
    for sev in LAMP_LEVELS:
        mark = " ▾" if sort == "critical" and sev == Severity.CRITICAL else ""
        t.add_column(sev.short + mark, justify="right", no_wrap=True)
    t.add_column("Ack" + (" ▾" if sort == "outstanding" else ""), justify="right", no_wrap=True)
    t.add_column("", ratio=1, min_width=8)
    for r in rows:
        cells: list = [Text(clip(r.node, name_width)), num(r.total)]
        for sev in LAMP_LEVELS:
            n = r.by_severity[sev]
            # Netcool "lamp": a count on a block of its severity colour, blank when zero.
            cells.append(Text(f"{n:>3}", style=SEVERITY_STYLE[sev]) if n else Text("·", style=MUTED, justify="center"))
        cells.append(Text(f"{r.ack_pct}%", style=MUTED))
        parts = [(r.by_severity[sev], bg(SEVERITY_STYLE[sev])) for sev in analytics.SEVERITY_ORDER]
        cells.append(StackedBar(parts, scale=scale))
        t.add_row(*cells)
    return t


def severity_legend() -> Text:
    text = Text("Bar: ", style=MUTED)
    text.append("  ", style=bg(SEVERITY_STYLE[Severity.MAJOR]))
    text.append(" outstanding   ", style=MUTED)
    text.append("  ", style=bg(ACK_STYLE[Severity.MAJOR]))
    text.append(" acknowledged", style=MUTED)
    return text


def mix_legend(levels: tuple[Severity, ...] = LAMP_LEVELS) -> Text:
    text = Text("Bar: ", style=MUTED)
    for sev in levels:
        text.append("  ", style=bg(SEVERITY_STYLE[sev]))
        text.append(f" {sev.short}  ", style=MUTED)
    return text


def dots(items: list[tuple[str, int]], empty: str = "none") -> Text:
    """'label 12 · label 3 · ...' with the separators muted."""
    text = Text()
    for i, (label, n) in enumerate(items):
        if i:
            text.append(" · ", style=MUTED)
        text.append(f"{label} {n:,}")
    if not items:
        text.append(empty, style=MUTED)
    return text
