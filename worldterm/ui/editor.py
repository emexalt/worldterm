"""Note editor screen built on Textual's TextArea."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Label, Static, TextArea


def external_editor() -> str | None:
    return os.environ.get("VISUAL") or os.environ.get("EDITOR") or None


class ConfirmDiscard(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmDiscard { align: center middle; }
    ConfirmDiscard > Label {
        padding: 1 3; border: thick $error; background: $surface; text-style: bold;
    }
    """
    BINDINGS = [
        Binding("y", "yes", "Discard", show=False),
        Binding("n,escape", "no", "Keep editing", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Label("Discard unsaved changes?  [y] discard   [n] keep editing")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class NoteEditorScreen(Screen[str | None]):
    """Dismisses with the note text on save, or None when cancelled."""

    DEFAULT_CSS = """
    NoteEditorScreen { layout: vertical; }
    NoteEditorScreen #bar { height: 1; background: $primary; color: $text; text-style: bold; padding: 0 1; }
    NoteEditorScreen TextArea { height: 1fr; }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("f2", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+e", "external", "$EDITOR"),
    ]

    def __init__(self, text: str, title: str) -> None:
        super().__init__()
        self.initial = text
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield Static(f" NOTE  {self.title_text}", id="bar")
        yield TextArea(self.initial, soft_wrap=True, tab_behavior="indent", show_line_numbers=False, id="editor")
        yield Footer()

    def on_mount(self) -> None:
        ta = self.query_one(TextArea)
        ta.move_cursor(ta.document.end)
        ta.focus()

    def on_text_area_changed(self, _: TextArea.Changed) -> None:
        self._update_bar()

    def _update_bar(self) -> None:
        modified = self.query_one(TextArea).text != self.initial
        self.query_one("#bar", Static).update(f" NOTE  {self.title_text}{' [modified]' if modified else ''}")

    def action_save(self) -> None:
        self.dismiss(self.query_one(TextArea).text)

    def action_cancel(self) -> None:
        if self.query_one(TextArea).text == self.initial:
            self.dismiss(None)
            return

        def _done(discard: bool | None) -> None:
            if discard:
                self.dismiss(None)

        self.app.push_screen(ConfirmDiscard(), _done)

    def action_external(self) -> None:
        editor = external_editor()
        if not editor:
            self.notify("$VISUAL / $EDITOR is not set", severity="warning")
            return
        ta = self.query_one(TextArea)
        fd, path = tempfile.mkstemp(prefix="worldterm-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(ta.text)
            with self.app.suspend():
                subprocess.run(shlex.split(editor) + [path], check=False)
            with open(path, encoding="utf-8") as fh:
                new_text = fh.read()
            if new_text != ta.text:
                ta.load_text(new_text)
                ta.move_cursor(ta.document.end)
                self._update_bar()
        except OSError as exc:
            self.notify(f"External editor failed: {exc}", severity="error")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
