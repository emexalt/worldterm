"""Right-click context menu as a modal screen anchored at the pointer."""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

MENU_MIN_WIDTH = 24


class ContextMenu(ModalScreen[str | None]):
    """Dismisses with the chosen item id, or None when cancelled."""

    DEFAULT_CSS = """
    ContextMenu {
        background: $background 0%;
        align: left top;
    }
    ContextMenu > OptionList {
        width: auto;
        height: auto;
        min-width: 24;
        border: round $accent;
        background: $surface;
        padding: 0;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "cancel", "Cancel", show=False),
    ]

    def __init__(self, items: list[tuple[str, str]], x: int, y: int) -> None:
        super().__init__()
        self.items = items
        self.anchor_x = x
        self.anchor_y = y

    def compose(self) -> ComposeResult:
        yield OptionList(*[Option(label, id=key) for key, label in self.items], id="menu")

    def on_mount(self) -> None:
        menu = self.query_one(OptionList)
        # The screen has not been laid out yet (its size is 0x0 here), so clamp
        # against the terminal size. Width/height mirror the CSS: border (2) +
        # option padding (2), never narrower than min-width.
        width = max(MENU_MIN_WIDTH, max(len(label) for _, label in self.items) + 4)
        height = len(self.items) + 2
        screen_w, screen_h = self.app.size
        max_x = max(0, screen_w - width)
        max_y = max(0, screen_h - height)
        menu.styles.offset = (max(0, min(self.anchor_x, max_x)), max(0, min(self.anchor_y, max_y)))
        menu.highlighted = 0
        menu.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        menu = self.query_one(OptionList)
        if not menu.region.contains(event.screen_x, event.screen_y):
            event.stop()
            self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        # first-letter accelerators
        if event.character and event.character.isalpha():
            for key, label in self.items:
                if label and label[0].lower() == event.character.lower():
                    event.stop()
                    self.dismiss(key)
                    return

    def action_cancel(self) -> None:
        self.dismiss(None)
