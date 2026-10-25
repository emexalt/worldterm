"""Headless Textual tests driving the app with Pilot."""
import html
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from rich.console import Console
from textual import events
from textual.widgets import DataTable, OptionList, TextArea

from worldterm.sources import Source
from worldterm.ui.styles import SEVERITY_COLORS, darken
from worldterm.ui.table import EventTable
from worldterm.ui.app import MainScreen, WorldTerm
from worldterm.ui.dashboard import DashboardScreen
from worldterm.ui.editor import NoteEditorScreen
from worldterm.ui.menu import ContextMenu
from worldterm.ui.reader import ReaderScreen

FIXTURE = (Path(__file__).parent / "fixtures" / "bbc_sample.xml").as_uri()


async def wait_for_load(app: WorldTerm, pilot, timeout: float = 10.0) -> None:
    import asyncio
    waited = 0.0
    while not app.initial_load_done and waited < timeout:
        await asyncio.sleep(0.1)
        waited += 0.1
    await pilot.pause()
    assert app.initial_load_done, "feed did not load"


class UITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_app(self, weather: str = "off", weather_url: str | None = None) -> WorldTerm:
        return WorldTerm([FIXTURE], interval=600, data_dir=self.data_dir, weather=weather, weather_url=weather_url)

    def state(self) -> dict:
        return json.loads((self.data_dir / "state.json").read_text())

    async def test_loads_rows_and_header(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            self.assertIsInstance(main, MainScreen)
            table = main.query_one(DataTable)
            self.assertEqual(table.row_count, 35)
            header = str(main.query_one("#header").renderable)
            self.assertIn("WORLDTERM", header)
            self.assertIn("CRT", header)

    async def test_keyboard_ack_read_note(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            await pilot.press("j")
            ev = main.current
            self.assertIsNotNone(ev)
            await pilot.press("a")
            await pilot.pause()
            self.assertTrue(ev.acknowledged)
            self.assertIs(main.current, ev, "cursor should stay on the same event after rebuild")

            await pilot.press("r")
            await pilot.pause()
            self.assertIsInstance(app.screen, ReaderScreen)
            self.assertTrue(ev.read)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)

            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, NoteEditorScreen)
            ta = app.screen.query_one(TextArea)
            self.assertTrue(ta.text.startswith("# "))
            self.assertIn("Link:", ta.text)
            await pilot.press(*"hello")
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertIsNotNone(ev.note)
            note = (self.data_dir / ev.note).read_text()
            self.assertTrue(note.startswith("# "))
            self.assertTrue(note.rstrip().endswith("hello"))

            # Reopen -> existing note is loaded, Esc with no change cancels silently
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(app.screen.query_one(TextArea).text, note)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)

            await pilot.press("q")
            await pilot.pause()
        st = self.state()
        rec = st[ev.guid]
        self.assertTrue(rec["acknowledged"] and rec["read"] and rec["note"])

    async def test_right_click_menu_acknowledges(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            table = main.query_one(DataTable)
            region = table.content_region
            row = 3
            x, y = region.x + 20, region.y + 1 + row  # +1 for the header line
            main.post_message(events.MouseDown(table, x - region.x, y - region.y, 0, 0, 3, False, False, False, x, y))
            await pilot.pause()
            self.assertIsInstance(app.screen, ContextMenu)
            self.assertEqual(table.cursor_row, row)
            labels = [str(app.screen.query_one(OptionList).get_option_at_index(i).prompt) for i in range(5)]
            self.assertEqual(labels[:3], ["Acknowledge", "Read", "Create note"])
            ev = main.current
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertTrue(ev.acknowledged)

            # Menu again shows Unacknowledge; Escape cancels
            await pilot.press("m")
            await pilot.pause()
            self.assertIsInstance(app.screen, ContextMenu)
            first = str(app.screen.query_one(OptionList).get_option_at_index(0).prompt)
            self.assertEqual(first, "Unacknowledge")
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertTrue(ev.acknowledged)

    async def test_context_menu_opens_at_pointer(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            table = main.query_one(DataTable)
            # (click x, click y) -> expected menu top-left; the last click is in the
            # bottom-right corner, so the menu must shift to stay fully on screen.
            cases = [((50, 8), (50, 9)), ((5, 3), (5, 4)), ((110, 27), (120 - 24, 30 - 7))]
            for (x, y), expected in cases:
                main.post_message(events.MouseDown(table, x, y - 1, 0, 0, 3, False, False, False, x, y))
                await pilot.pause()
                await pilot.pause()
                self.assertIsInstance(app.screen, ContextMenu)
                region = app.screen.query_one(OptionList).region
                self.assertEqual((region.x, region.y), expected, f"click at {(x, y)}")
                await pilot.press("escape")
                await pilot.pause()

    async def test_source_label_replaces_channel_title(self):
        atom = (Path(__file__).parent / "fixtures" / "atom_sample.xml").as_uri()
        app = WorldTerm([Source("wire", "Wire", FIXTURE), atom], interval=600, data_dir=self.data_dir, weather="off")
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            nodes = {ev.node for ev in app.store.events.values()}
            # labelled source uses its label; the plain URL keeps the channel title
            self.assertEqual(nodes, {"Wire", "Example Atom Wire"})
            self.assertEqual(app.screen.query_one(DataTable).row_count, 35 + 3)

    # ------------------------------------------------------------ multi-select
    def row_bg(self, table, row):
        """Background colour (hex) and underline flag of a rendered row's first cell."""
        seg = next(iter(table.render_line(row + 1)))  # +1: header line
        return seg.style.bgcolor.triplet.hex, bool(seg.style.underline)

    def guids(self, main, rows):
        return {main._guid_at(r) for r in rows}

    async def test_keyboard_multi_select_and_batch_ack(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            table = main.query_one(DataTable)
            await pilot.press("space", "space", "down", "space")  # rows 0, 1, 3
            await pilot.pause()
            self.assertEqual(app.selection, self.guids(main, [0, 1, 3]))
            self.assertEqual(table.cursor_row, 4, "space moves the cursor down")
            self.assertIn("Selected: 3", str(main.query_one("#header").renderable))
            self.assertTrue(self.row_bg(table, 1)[1], "selected row is underlined")
            self.assertFalse(self.row_bg(table, 2)[1])

            await pilot.press("a")
            await pilot.pause()
            acked = {g for g, e in app.store.events.items() if e.acknowledged}
            self.assertEqual(acked, self.guids(main, [0, 1, 3]), "cursor row 4 is not part of the batch")
            self.assertEqual(app.selection, self.guids(main, [0, 1, 3]), "selection kept for undo")

            await pilot.press("a")  # all selected are acked -> batch unacknowledge
            await pilot.pause()
            self.assertFalse(any(e.acknowledged for e in app.store.events.values()))

            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.selection, set())
            self.assertFalse(self.row_bg(table, 1)[1])

        st = self.state()
        self.assertFalse(any(r["acknowledged"] for r in st.values()))

    async def test_mixed_selection_acks_all(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            await pilot.press("a")            # ack row 0 alone (no selection)
            await pilot.press("shift+down", "shift+down")  # select rows 0..2
            await pilot.pause()
            self.assertEqual(app.selection, self.guids(main, [0, 1, 2]))
            await pilot.press("a")
            await pilot.pause()
            for g in self.guids(main, [0, 1, 2]):
                self.assertTrue(app.store.events[g].acknowledged, "mixed batch -> everything acknowledged")

    async def test_select_all_toggle_and_hide_acked(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            table = app.screen.query_one(DataTable)
            await pilot.press("ctrl+a")
            await pilot.pause()
            self.assertEqual(len(app.selection), 35)
            await pilot.press("ctrl+a")
            await pilot.pause()
            self.assertEqual(app.selection, set(), "second ctrl+a deselects all")

            await pilot.press("x", "space", "space", "a")  # hide acked, select two, ack
            await pilot.pause()
            self.assertEqual(table.row_count, 33)
            self.assertEqual(app.selection, set(), "hidden rows drop out of the selection")

    async def test_mouse_ctrl_and_shift_click(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            table = main.query_one(DataTable)

            async def click(row, *, ctrl=False, shift=False):
                # Pilot routes the click through the app exactly like a terminal click.
                await pilot.click(EventTable, offset=(30, 1 + row), control=ctrl, shift=shift)

            await click(2, ctrl=True)
            await click(5, ctrl=True)
            await pilot.pause()
            self.assertEqual(app.selection, self.guids(main, [2, 5]))
            await click(2, ctrl=True)  # toggle off
            await pilot.pause()
            self.assertEqual(app.selection, self.guids(main, [5]))

            table.move_cursor(row=7)
            await pilot.pause()
            await click(10, shift=True)  # range from cursor row 7 to 10
            await pilot.pause()
            self.assertEqual(app.selection, self.guids(main, [5, 7, 8, 9, 10]))

    async def test_right_click_menu_batch_and_outside_selection(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            table = main.query_one(DataTable)
            region = table.content_region

            def right_click(row):
                x, y = region.x + 30, region.y + 1 + row
                main.post_message(events.MouseDown(table, 30, y - region.y, 0, 0, 3, False, False, False, x, y))

            await pilot.press("space", "space", "space")  # rows 0..2
            right_click(1)  # inside the selection
            await pilot.pause()
            await pilot.pause()
            menu = app.screen.query_one(OptionList)
            labels = [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]
            self.assertEqual(labels[0], "Acknowledge 3 selected")
            self.assertIn("Deselect all", labels)
            await pilot.press("enter")
            await pilot.pause()
            for g in self.guids(main, [0, 1, 2]):
                self.assertTrue(app.store.events[g].acknowledged)

            right_click(6)  # outside -> selection cleared, menu acts on row 6 alone
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(app.selection, set())
            first = str(app.screen.query_one(OptionList).get_option_at_index(0).prompt)
            self.assertEqual(first, "Acknowledge")
            await pilot.press("enter")
            await pilot.pause()
            acked = {g for g, e in app.store.events.items() if e.acknowledged}
            self.assertEqual(acked, self.guids(main, [0, 1, 2, 6]))

    async def test_acknowledged_rows_use_darker_background(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            main = app.screen
            table = main.query_one(DataTable)
            await pilot.press("down")  # leave row 0 (cursor) alone; ack row 1
            sev = main.current.severity
            normal_bg = SEVERITY_COLORS[sev][0]
            self.assertEqual(self.row_bg(table, 1)[0], normal_bg)
            await pilot.press("a", "down")
            await pilot.pause()
            self.assertEqual(self.row_bg(table, 1)[0], darken(normal_bg))
            # whole row, not only the text: every segment carries the dark background
            bgs = {seg.style.bgcolor.triplet.hex for seg in table.render_line(2)}
            self.assertEqual(len(bgs - {darken(normal_bg)}), 1, "only the scrollbar filler may differ")

    # ------------------------------------------------------------ dashboard
    @staticmethod
    def rendered(widget, width=120) -> str:
        """Plain text of a Static's Rich renderable."""
        buf = io.StringIO()
        Console(file=buf, width=width, force_terminal=False, color_system=None).print(widget.renderable)
        return buf.getvalue()

    @staticmethod
    def screen_text(app) -> str:
        svg = app.export_screenshot()
        svg = re.sub(r"<style.*?</style>", "", svg, flags=re.S)
        return html.unescape(re.sub(r"<[^>]+>", "", svg)).replace("\xa0", " ")

    async def test_dashboard_opens_and_shows_counts(self):
        app = self.make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await wait_for_load(app, pilot)
            await pilot.press("down", "a")  # acknowledge one event so the ack counts are non-zero
            await pilot.press("d")
            await pilot.pause()
            self.assertIsInstance(app.screen, DashboardScreen)
            dash = app.screen
            bar = str(dash.query_one("#bar").renderable)
            self.assertIn("35 events", bar)
            self.assertIn("1 sources", bar)
            self.assertIn("1 acknowledged (3%)", bar)

            sev = self.rendered(dash.query_one("#sev"))
            self.assertIn("Critical", sev)
            self.assertIn("Warning", sev)
            self.assertIn("1 ack", sev)
            src = self.rendered(dash.query_one("#src"))
            self.assertIn("BBC News", src)
            self.assertIn("35", src)
            trig = self.rendered(dash.query_one("#trig"))
            self.assertIn("kill", trig)
            self.assertIn("Decided", trig)
            summary = self.rendered(dash.query_one("#trig-summary"))
            self.assertIn("none → Warning", summary)
            self.assertIn("Live/Breaking", summary)
            self.assertIn("keywords fired", summary)

            text = self.screen_text(app)
            for needle in ("By severity", "By source", "Triggers", "DASHBOARD"):
                self.assertIn(needle, text)

            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertEqual(app.screen.query_one(DataTable).row_count, 35, "main table intact")

    async def test_dashboard_sorts_and_refresh_while_open(self):
        app = self.make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            await wait_for_load(app, pilot)
            await pilot.press("d")
            await pilot.pause()
            dash = app.screen
            self.assertIn("sorted by total", dash.query_one("#src-panel").border_title)
            await pilot.press("s")
            self.assertIn("sorted by critical", dash.query_one("#src-panel").border_title)
            self.assertIn("sorted by events", dash.query_one("#trig-panel").border_title)
            await pilot.press("t")
            self.assertIn("sorted by deciding", dash.query_one("#trig-panel").border_title)
            await pilot.press("t")
            self.assertIn("sorted by level", dash.query_one("#trig-panel").border_title)
            first_line = self.rendered(dash.query_one("#trig")).splitlines()[1]
            self.assertIn("CRT", first_line, "level sort puts a Critical keyword first")

            # A fetch landing while the dashboard is open updates it in place.
            from worldterm import feed
            arts = feed.parse(Path(FIXTURE[7:]).read_bytes())
            app._apply_articles(FIXTURE, arts)
            app._fetch_done()
            await pilot.pause()
            self.assertIsInstance(app.screen, DashboardScreen)
            self.assertIn("35 events", str(dash.query_one("#bar").renderable))
            self.assertEqual(list(app.store.events.values())[0].tally, 2)

            await pilot.press("d")  # d toggles back too
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)

    async def test_dashboard_narrow_layout_and_header_click(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for_load(app, pilot)
            await pilot.click("#header")
            await pilot.pause()
            self.assertIsInstance(app.screen, DashboardScreen)
            self.assertTrue(app.screen.query_one("#body").has_class("narrow"))
            text = self.screen_text(app)
            self.assertIn("By severity", text)
            await pilot.press("q")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)

    async def test_filters_and_sort(self):
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            table = app.screen.query_one(DataTable)
            total = table.row_count
            await pilot.press("f")  # >= MINOR
            await pilot.pause()
            self.assertLess(table.row_count, total)
            await pilot.press("f", "f", "f")  # back to all
            await pilot.pause()
            self.assertEqual(table.row_count, total)
            await pilot.press("a", "x")  # ack current, hide acked
            await pilot.pause()
            self.assertEqual(table.row_count, total - 1)
            await pilot.press("s")
            await pilot.pause()
            self.assertTrue(app.sort_by_time)

    async def test_feed_error_does_not_crash(self):
        app = WorldTerm(["http://127.0.0.1:9/rss"], interval=600, data_dir=self.data_dir, timeout=2, weather="off")
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            self.assertEqual(app.screen.query_one(DataTable).row_count, 0)
            await pilot.press("a", "r", "n", "enter")  # no-ops with no rows
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)


if __name__ == "__main__":
    unittest.main()


NWS_FIXTURE = (Path(__file__).parent / "fixtures" / "nws_sample.json").as_uri()


async def wait_for_weather(app: WorldTerm, pilot, timeout: float = 10.0) -> None:
    import asyncio
    waited = 0.0
    while not (app.initial_load_done and app.weather_loaded) and waited < timeout:
        await asyncio.sleep(0.1)
        waited += 0.1
    await pilot.pause()
    assert app.weather_loaded, "alerts did not load"


class WeatherUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_app(self) -> WorldTerm:
        return WorldTerm([FIXTURE], interval=600, data_dir=self.data_dir, weather_url=NWS_FIXTURE)

    async def test_weather_screen_lists_alerts_as_events(self):
        from worldterm.ui.app import WeatherScreen
        app = self.make_app()
        async with app.run_test(size=(140, 36)) as pilot:
            await wait_for_weather(app, pilot)
            main = app.screen
            self.assertIsInstance(main, MainScreen)
            self.assertIn("WX 23", str(main.query_one("#header").renderable), "main header shows the weather lamp")

            await pilot.press("w")
            await pilot.pause()
            wx = app.screen
            self.assertIsInstance(wx, WeatherScreen)
            table = wx.query_one(DataTable)
            self.assertEqual(table.row_count, 23)
            header = str(wx.query_one("#header").renderable)
            self.assertIn("WEATHER", header)
            self.assertIn("Alerts: 23", header)
            self.assertIn("Offices: 18", header)
            text = UITests.screen_text(app)
            self.assertIn("Office", text)
            self.assertIn("Expires", text)
            self.assertIn("Tornado Warning: Taylor, FL", text)

            ev = wx.current
            self.assertEqual(ev.alert.event, "Tornado Warning", "most severe alert sorts first")
            await pilot.press("a")
            await pilot.pause()
            self.assertTrue(ev.acknowledged)
            self.assertIs(wx.current, ev)

            await pilot.press("r")
            await pilot.pause()
            self.assertIsInstance(app.screen, ReaderScreen)
            reader = UITests.screen_text(app)
            self.assertIn("Office:", reader)
            self.assertIn("Immediate", reader)
            self.assertIn("TAKE COVER NOW", reader)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, WeatherScreen)

            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, NoteEditorScreen)
            note = app.screen.query_one(TextArea).text
            self.assertTrue(note.startswith("# tornado-warning-taylor-fl"))
            self.assertIn("Alert:     Tornado Warning", note)
            self.assertIn("NWS Extreme", note)
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("w")  # back to the news list
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertEqual(app.screen.query_one(DataTable).row_count, 35, "news list intact")
            await pilot.press("q")
            await pilot.pause()
        state = json.loads((self.data_dir / "weather.json").read_text())
        self.assertTrue(state[ev.guid]["acknowledged"])
        self.assertTrue(state[ev.guid]["read"])
        self.assertNotIn(ev.guid, json.loads((self.data_dir / "state.json").read_text()))

    async def test_weather_selection_batch_and_escape(self):
        from worldterm.ui.app import WeatherScreen
        app = self.make_app()
        async with app.run_test(size=(140, 36)) as pilot:
            await wait_for_weather(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            wx = app.screen
            await pilot.press("space", "space", "space")
            self.assertEqual(len(wx.selection), 3)
            self.assertEqual(app.selection, set(), "the news list's selection is separate")
            await pilot.press("a")
            await pilot.pause()
            acked = [e for e in app.weather_store.events.values() if e.acknowledged]
            self.assertEqual(len(acked), 3)
            await pilot.press("escape")  # clears the selection first...
            await pilot.pause()
            self.assertEqual(wx.selection, set())
            self.assertIsInstance(app.screen, WeatherScreen)
            await pilot.press("escape")  # ...then leaves the screen
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)

    async def test_dashboard_weather_page(self):
        app = self.make_app()
        async with app.run_test(size=(140, 42)) as pilot:
            await wait_for_weather(app, pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            dash = app.screen
            self.assertIsInstance(dash, DashboardScreen)
            self.assertEqual(dash.page, "weather", "opened from the weather list, it starts on the weather page")
            bar = str(dash.query_one("#bar").renderable)
            self.assertIn("23 active alerts", bar)
            self.assertIn("18 offices", bar)
            self.assertIn("20 event types", bar)

            sev = UITests.rendered(dash.query_one("#wx-sev"))
            self.assertIn("Extreme → Critical", sev)
            self.assertIn("Minor → Warning", sev)
            kinds = UITests.rendered(dash.query_one("#wx-kinds"))
            self.assertIn("Warnings 8", kinds)
            self.assertIn("Advisories 8", kinds)
            states = UITests.rendered(dash.query_one("#wx-state"))
            self.assertIn("FL", states)
            self.assertIn("Marine Lakes", states)
            types = UITests.rendered(dash.query_one("#wx-type"))
            self.assertIn("Small Craft Advisory", types)
            self.assertIn("Event type", types)
            timing = UITests.rendered(dash.query_one("#wx-time"))
            for needle in ("Issued", "24h ago", "Ends in", "Urgency", "Immediate", "Observed", "Shelter 1",
                           "updates 6"):
                self.assertIn(needle, timing)
            text = UITests.screen_text(app)
            for needle in ("Alerts by severity", "By state", "Event types", "Timing and urgency", "WEATHER"):
                self.assertIn(needle, text)

            await pilot.press("o")
            self.assertIn("By office", dash.query_one("#wx-state-panel").border_title)
            self.assertIn("Tallahassee", UITests.rendered(dash.query_one("#wx-state")))
            await pilot.press("t")
            self.assertIn("sorted by level", dash.query_one("#wx-type-panel").border_title)
            first = UITests.rendered(dash.query_one("#wx-type")).splitlines()[1]
            self.assertIn("Tornado Warning", first)

            await pilot.press("w")  # switch to the news page
            await pilot.pause()
            self.assertEqual(dash.page, "news")
            self.assertIn("35 events", str(dash.query_one("#bar").renderable))
            self.assertIn("By source", UITests.screen_text(app))
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(dash.page, "weather")

            # An alert fetch landing while the dashboard is open updates it in place.
            from worldterm import nws
            app._apply_alerts(nws.parse(Path(NWS_FIXTURE[7:]).read_bytes())[:20])
            app._weather_done()
            await pilot.pause()
            self.assertIn("20 active alerts · 3 ended", str(dash.query_one("#bar").renderable))
            self.assertIn("Ended → Clear", UITests.rendered(dash.query_one("#wx-sev")))

            await pilot.press("escape")
            await pilot.pause()
            from worldterm.ui.app import WeatherScreen
            self.assertIsInstance(app.screen, WeatherScreen)
            self.assertIn("(+3 ended)", str(app.screen.query_one("#header").renderable))

    async def test_dashboard_weather_page_narrow(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for_weather(app, pilot)
            await pilot.press("d", "w")
            await pilot.pause()
            dash = app.screen
            self.assertEqual(dash.page, "weather")
            self.assertTrue(dash.query_one("#wx-body").has_class("narrow"))
            self.assertFalse(dash.query_one("#wx-body").has_class("hidden"))
            self.assertTrue(dash.query_one("#body").has_class("hidden"))
            self.assertIn("Alerts by severity", UITests.screen_text(app))

    async def test_weather_off(self):
        app = WorldTerm([FIXTURE], interval=600, data_dir=self.data_dir, weather="off")
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            self.assertIsNone(app.weather_url)
            self.assertNotIn("WX", str(app.screen.query_one("#header").renderable))
            await pilot.press("w")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen, "w does nothing but notify when weather is off")
            await pilot.press("d", "w")
            await pilot.pause()
            self.assertIn("weather alerts are off", str(app.screen.query_one("#bar").renderable))

    async def test_header_lamp_click_opens_weather(self):
        from worldterm.ui.app import WeatherScreen
        app = self.make_app()
        async with app.run_test(size=(140, 36)) as pilot:
            await wait_for_weather(app, pilot)
            main = app.screen
            x0, x1 = main._wx_span
            await pilot.click("#header", offset=((x0 + x1) // 2, 0))
            await pilot.pause()
            self.assertIsInstance(app.screen, WeatherScreen)
            await pilot.press("w")
            await pilot.pause()
            await pilot.click("#header", offset=(2, 0))
            await pilot.pause()
            self.assertIsInstance(app.screen, DashboardScreen)
            self.assertEqual(app.screen.page, "news")
