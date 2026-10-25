"""The severity palette as plain hex values, shared by the terminal UI and the wall page.

Kept out of `ui/styles.py` (which imports Rich) so the data layer can serve it as JSON.
"""
from __future__ import annotations

from .severity import Severity

# (background, foreground) per severity, as explicit truecolor values.
# ANSI names are not used because Textual redraws them through its own palette,
# where "yellow" comes out orange (indistinguishable from Major) and "magenta"
# comes out the same pink-red as "red" (indistinguishable from Critical).
SEVERITY_COLORS: dict[Severity, tuple[str, str]] = {
    Severity.CRITICAL: ("#f4005f", "#ffffff"),
    Severity.MAJOR: ("#ff8700", "#000000"),
    Severity.MINOR: ("#ffd700", "#000000"),
    Severity.WARNING: ("#58d1eb", "#000000"),
    Severity.INDETERMINATE: ("#af5fd7", "#ffffff"),
    Severity.CLEAR: ("#98e024", "#000000"),
}

# Acknowledged rows use the same hue at 45% brightness with light text.
# Every pair keeps at least 4:1 text contrast on the terminal's dark rows.
ACK_DARKEN = 0.45
ACK_FOREGROUND = "#e4e4e4"


def darken(hex_color: str, factor: float = ACK_DARKEN) -> str:
    """Scale an #rrggbb colour's channels by *factor* (0..1)."""
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02x}{:02x}{:02x}".format(*(round(c * factor) for c in (r, g, b)))
