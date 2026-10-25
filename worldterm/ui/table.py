"""DataTable subclass that paints each whole row in its event's severity colour."""
from __future__ import annotations

from rich.style import Style
from textual.widgets import DataTable

# The cursor inverts the row's own colours instead of painting Textual's blue.
CURSOR_STYLE = Style(reverse=True, bold=True)


class EventTable(DataTable):
    """Row-cursor table whose rows carry a full-width background style.

    Stock DataTable styles only the text inside a cell, so a coloured cell
    leaves its padding, the unused column width, and the space to the right
    of the last column in the table's background colour. Here the severity
    style is returned as the row's base style, which DataTable applies to
    every one of those regions.
    """

    DEFAULT_CSS = """
    EventTable {
        /* Always reserve the scrollbar so column widths can fill the row exactly. */
        overflow-y: scroll;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._row_styles: list[Style] = []

    def set_row_styles(self, styles: list[Style]) -> None:
        """Set one style per row, in display order. Call before adding the rows."""
        self._row_styles = styles

    def set_row_style(self, index: int, style: Style) -> None:
        """Restyle one row in place. Call update_cell afterwards (or refresh) so
        DataTable invalidates its render caches, which are keyed on its update count."""
        if 0 <= index < len(self._row_styles):
            self._row_styles[index] = style

    @property
    def row_area_width(self) -> int:
        """Width available to columns, excluding the vertical scrollbar."""
        return self.scrollable_content_region.width

    def _get_row_style(self, row_index: int, base_style: Style) -> Style:
        if 0 <= row_index < len(self._row_styles):
            return base_style + self._row_styles[row_index]
        return super()._get_row_style(row_index, base_style)

    def get_component_rich_style(self, *names: str, partial: bool = False) -> Style:
        if names == ("datatable--cursor",):
            return CURSOR_STYLE
        if names == ("datatable--hover",):
            # Hover would otherwise repaint the row's background.
            return Style.null()
        return super().get_component_rich_style(*names, partial=partial)
