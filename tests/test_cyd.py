"""The CYD display: headline selection, frames, the wire protocol, the service, and the app wiring."""
import asyncio
import errno
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worldterm import cyd
from worldterm.cyd import (CydService, Headline, ascii_fold, encode, parse_line, render_headline, render_idle,
                           render_offline, select_critical, todays, wrap)
from worldterm.feed import Article
from worldterm.severity import Severity
from worldterm.store import Event
from worldterm.ui.app import WorldTerm

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
NEW_YORK = timezone(timedelta(hours=-4))
FIXTURE = (Path(__file__).parent / "fixtures" / "bbc_sample.xml").as_uri()
HELLO = b"HELLO worldterm-cyd 1\r\n"


def article(guid, title="Earthquake kills dozens", published=NOW, source="BBC News"):
    return Article(guid, title, "summary", f"https://x/{guid}", published, source)


def event(guid, title="Earthquake kills dozens", severity=Severity.CRITICAL, acknowledged=False,
          published=NOW, cleared_at=None, first_seen=NOW):
    return Event(article(guid, title, published), severity, first_seen, first_seen,
                 acknowledged=acknowledged, cleared_at=cleared_at)


def headline(guid, title="Earthquake kills dozens", when=NOW, kind="news", source="BBC News"):
    return Headline(kind, guid, title, source, when)


def texts(frame) -> str:
    """The rows of a decoded SHOW payload, or of a Frame, as one string."""
    rows = frame["rows"] if isinstance(frame, dict) else frame.rows
    return "\n".join(r[1] for r in rows)


class FakeClock:
    def __init__(self, now=NOW, mono=1000.0):
        self.now = now
        self.mono = mono

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


class FakePort:
    """A scripted board. `feed` queues what it sends; writes are recorded; `auto` answers like the board."""

    def __init__(self, *, auto=False, on_idle=None, responder=None):
        self.incoming = bytearray()
        self.written = bytearray()
        self.closed = False
        self.failed = False
        self.auto = auto
        self.on_idle = on_idle          # called with the timeout when there is nothing to read
        self.responder = responder      # line -> reply bytes or None; overrides auto
        self._pos = 0
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self.incoming += data

    def unplug(self) -> None:
        self.failed = True

    def read(self, max_bytes=4096, timeout=0.2):
        if self.failed:
            raise OSError(errno.EIO, "unplugged")
        with self._lock:
            data = bytes(self.incoming[:max_bytes])
            del self.incoming[:max_bytes]
        if not data and self.on_idle:
            self.on_idle(timeout)
        return data

    def write(self, data, timeout=1.0):
        if self.failed:
            raise OSError(errno.EIO, "unplugged")
        with self._lock:
            self.written += data
            for line in self._new_lines():
                reply = self._reply(line)
                if reply:
                    self.incoming += reply

    def _new_lines(self):
        out = []
        while True:
            end = self.written.find(b"\n", self._pos)
            if end < 0:
                break
            out.append(bytes(self.written[self._pos:end]).lstrip(b"\x04"))
            self._pos = end + 1
        if self.written[self._pos:] == b"\x04":   # a soft reset has no newline
            out.append(b"\x04")
            self._pos += 1
        return out

    def _reply(self, line):
        if self.responder:
            return self.responder(line)
        if not self.auto:
            return None
        if line.startswith(b"SHOW "):
            return b"OK %d\r\n" % json.loads(line[5:])["seq"]
        if line == b"PING":
            return b"PONG worldterm-cyd 1\r\n"
        return None

    def close(self):
        self.closed = True

    def lines(self):
        with self._lock:
            raw = bytes(self.written)
        return [line.lstrip(b"\x04") for line in raw.split(b"\n")[:-1]]

    def frames(self):
        return [json.loads(line[5:]) for line in self.lines() if line.startswith(b"SHOW ")]

    def pings(self):
        return sum(1 for line in self.lines() if line == b"PING")

    @property
    def resets(self):
        return bytes(self.written).count(b"\x04")


def make_service(clock, ports, scanner=None, **kwargs):
    """A service over fake ports. *ports* maps a path to a FakePort, or to an OSError to raise on open.
    Returns the service and a log of its callbacks."""
    log = []
    opened = []

    def open_port(path):
        opened.append(path)
        port = ports[path]
        if isinstance(port, Exception):
            raise port
        port.on_idle = clock.advance
        return port

    svc = CydService(scanner or (lambda: list(ports)), open_port, clock=lambda: clock.now,
                     monotonic=lambda: clock.mono, sleep=clock.advance, **kwargs)
    svc.on_connect = lambda path: log.append(("connect", path))
    svc.on_disconnect = lambda reason: log.append(("disconnect", reason))
    svc.on_error = lambda message: log.append(("error", message))
    return svc, log, opened


def connect(headlines=(), port=None):
    """A connected service showing *headlines*, with its port, clock and callback log."""
    clock = FakeClock()
    port = port or FakePort(auto=True)
    port.feed(HELLO)
    svc, log, _opened = make_service(clock, {"/dev/ttyFAKE": port})
    svc.update(headlines)
    svc.tick()
    assert log == [("connect", "/dev/ttyFAKE")], log
    return svc, port, clock, log


# ---------------------------------------------------------------------------- pure functions
class SelectionTests(unittest.TestCase):
    def test_select_critical_filters_and_orders(self):
        events = [
            event("older", published=NOW - timedelta(hours=1)),
            event("newest"),
            event("major", severity=Severity.MAJOR),
            event("acked", acknowledged=True),
            event("cleared", cleared_at=NOW),
            event("clear", severity=Severity.CLEAR),
        ]
        picked = select_critical(events, "news")
        self.assertEqual([h.key for h in picked], ["news:newest", "news:older"])
        self.assertEqual(picked[0].source, "BBC News")
        self.assertEqual(picked[0].title, "Earthquake kills dozens")

    def test_select_headlines_takes_a_minimum_level(self):
        events = [event("crit"), event("major", severity=Severity.MAJOR), event("minor", severity=Severity.MINOR),
                  event("acked-major", severity=Severity.MAJOR, acknowledged=True)]
        picked = cyd.select_headlines(events, "news", min_severity=Severity.MAJOR)
        self.assertEqual(sorted(h.guid for h in picked), ["crit", "major"])
        self.assertEqual({h.guid: h.severity for h in picked}, {"crit": Severity.CRITICAL, "major": Severity.MAJOR})
        self.assertEqual([h.guid for h in select_critical(events, "news")], ["crit"])

    def test_weather_source_names_the_nws(self):
        ev = event("alert", title="Tornado Warning: Taylor, FL")
        ev.article.source = "Tallahassee FL"
        self.assertEqual(select_critical([ev], "weather")[0].source, "NWS Tallahassee FL")

    def test_when_falls_back_to_first_seen(self):
        seen = NOW - timedelta(days=1)
        ev = event("undated", published=None, first_seen=seen)
        self.assertEqual(select_critical([ev], "news")[0].when, seen)

    def test_todays_uses_the_clocks_calendar(self):
        h = headline("h", when=datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(todays([h], NOW), [h])
        self.assertEqual(todays([h], datetime(2026, 10, 11, 0, 1, tzinfo=timezone.utc)), [])
        # 02:00 UTC on the 10th is 22:00 on the 9th in New York.
        self.assertEqual(todays([h], datetime(2026, 10, 9, 23, 0, tzinfo=NEW_YORK)), [h])
        self.assertEqual(todays([h], datetime(2026, 10, 10, 10, 0, tzinfo=NEW_YORK)), [])

    def test_todays_is_newest_first_whatever_the_input_order(self):
        older = headline("old", when=NOW - timedelta(hours=2))
        newer = headline("new", when=NOW - timedelta(hours=1))
        self.assertEqual(todays([older, newer], NOW), [newer, older])
        self.assertEqual(todays([newer, older], NOW), [newer, older])


class RenderingTests(unittest.TestCase):
    def test_ascii_fold(self):
        self.assertEqual(ascii_fold("Élysée: “non” à l’accord – vraiment…"), 'Elysee: "non" a l\'accord - vraiment...')
        self.assertEqual(ascii_fold("  two   spaces here "), "two spaces here")
        self.assertEqual(ascii_fold("東京で大地震 2026"), cyd.NOT_DISPLAYABLE)
        self.assertEqual(ascii_fold(""), "")
        self.assertEqual(ascii_fold("12 killed"), "12 killed")

    def test_wrap(self):
        rows = wrap("Life-threatening Hurricane Isaias makes landfall in Florida")
        self.assertEqual(rows, ["Life-threatening Hurricane Isaias makes", "landfall in Florida"])
        self.assertTrue(all(len(r) <= 40 for r in wrap("x" * 100)))
        capped = wrap("word " * 100, max_rows=3)
        self.assertEqual(len(capped), 3)
        self.assertTrue(capped[-1].endswith("..."))
        self.assertEqual(wrap(""), [""])

    def test_render_headline(self):
        h = headline("h", title="Powerful magnitude 7.7 earthquake hits Panama, damaging buildings",
                     when=NOW - timedelta(hours=1))
        frame = render_headline(h, 2, 5, NOW)
        text = texts(frame)
        self.assertIn("CRITICAL 2/5", text)
        self.assertIn("Sat 10 Oct 12:00", text)
        self.assertIn("BBC News", text)
        self.assertIn("11:00", text)
        self.assertIn("Powerful magnitude 7.7 earthquake hits", text)
        self.assertIn("5 unacknowledged critical today", text)
        self.assertEqual(frame.led, "r")
        self.assertTrue(all(0 <= r[0] < 24 and len(r[1]) <= 40 and r[1].isascii() for r in frame.rows))
        self.assertEqual(frame.rows[2][0], cyd.TITLE_ROW)
        yesterday = render_headline(headline("y", when=NOW - timedelta(days=1)), 1, 1, NOW)
        self.assertIn("09 Oct 12:00", texts(yesterday))

    def test_long_source_leaves_room_for_the_time(self):
        frame = render_headline(headline("h", source="A very long source name indeed, truly"), 1, 1, NOW)
        row = frame.rows[1][1]
        self.assertEqual(len(row), 40)
        self.assertTrue(row.endswith(" 12:00"))

    def test_idle_and_offline(self):
        self.assertIn("No unacknowledged critical\nheadlines today", texts(render_idle(NOW)))
        self.assertEqual(render_idle(NOW).led, "off")
        self.assertIn("worldterm stopped 12:00", texts(render_offline(NOW)))


class ProtocolTests(unittest.TestCase):
    def test_encode(self):
        frame = cyd.Frame(((0, "héllo\x03 " + "x" * 50, "w", "k"), (30, "dropped", "w", "k")), led="r")
        line = encode(frame, 17)
        self.assertTrue(line.startswith(b"SHOW {") and line.endswith(b"}\n"))
        payload = json.loads(line[5:])
        self.assertEqual(payload["seq"], 17)
        self.assertEqual(payload["led"], "r")
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0][1], "h?llo? " + "x" * 33)
        self.assertTrue(all(0x20 <= b < 0x7F or b == 0x0A for b in line))

    def test_parse_line(self):
        self.assertEqual(parse_line(b"HELLO worldterm-cyd 1\r"), ("hello", "1"))
        self.assertEqual(parse_line(b"PONG worldterm-cyd 2"), ("pong", "2"))
        self.assertEqual(parse_line(b"OK 17"), ("ok", 17))
        self.assertEqual(parse_line(b"OK x"), ("other", "OK x"))
        self.assertEqual(parse_line(b"ERR bad json"), ("err", "bad json"))
        self.assertEqual(parse_line(b"TOUCH 10 20"), ("touch", 10, 20))
        self.assertEqual(parse_line(b"HELLO somethingelse 1"), ("other", "HELLO somethingelse 1"))
        self.assertEqual(parse_line(b">>> "), ("other", ">>>"))
        self.assertEqual(parse_line(b"\xff\xfe")[0], "other")


# ---------------------------------------------------------------------------- the service
class ServiceTests(unittest.TestCase):
    def test_connects_on_hello_then_shows_idle(self):
        svc, port, clock, log = connect()
        self.assertEqual(svc.path, "/dev/ttyFAKE")
        svc.tick()
        self.assertEqual([f["seq"] for f in port.frames()], [1])
        self.assertIn("No unacknowledged critical", texts(port.frames()[0]))
        self.assertEqual(port.frames()[0]["led"], "off")
        for _ in range(5):
            svc.tick()
        self.assertEqual(len(port.frames()), 1, "an unchanged frame is not resent")

    def test_connects_by_ping_when_the_board_is_already_running(self):
        clock = FakeClock()
        port = FakePort(auto=True)
        svc, log, _ = make_service(clock, {"/dev/ttyFAKE": port})
        svc.tick()
        self.assertEqual(log, [("connect", "/dev/ttyFAKE")])
        self.assertEqual(port.pings(), 1)
        self.assertGreaterEqual(clock.mono, 1000.0 + cyd.HELLO_WAIT)

    def test_cycles_and_follows_updates(self):
        older = headline("old", title="Tornado rips through Sicilian town", when=NOW - timedelta(hours=2))
        newer = headline("new", title="Powerful earthquake hits Panama", when=NOW - timedelta(hours=1))
        svc, port, clock, log = connect([older, newer])
        svc.tick()
        self.assertIn("CRITICAL 1/2", texts(port.frames()[-1]))
        self.assertIn("Powerful earthquake", texts(port.frames()[-1]))
        self.assertEqual(port.frames()[-1]["led"], "r")
        clock.advance(cyd.CYCLE_SECONDS)
        svc.tick()
        svc.tick()
        self.assertIn("2/2", texts(port.frames()[-1]))
        self.assertIn("Tornado rips", texts(port.frames()[-1]))
        svc.update([older])                        # the shown one was acknowledged
        svc.tick()
        svc.tick()
        self.assertIn("1/1", texts(port.frames()[-1]))
        self.assertIn("Tornado rips", texts(port.frames()[-1]))
        before = len(port.frames())
        svc.update([older])
        for _ in range(5):
            svc.tick()
        self.assertEqual(len(port.frames()), before)

    def test_midnight_empties_the_display(self):
        late = headline("late", when=datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc))
        svc, port, clock, log = connect([late])
        clock.now = datetime(2026, 10, 10, 23, 50, tzinfo=timezone.utc)
        svc.tick()
        self.assertIn("CRITICAL 1/1", texts(port.frames()[-1]))
        clock.advance(15 * 60)
        svc.tick()
        svc.tick()
        self.assertIn("No unacknowledged critical", texts(port.frames()[-1]))

    def test_unplug_then_reconnect(self):
        clock = FakeClock()
        first = FakePort(auto=True)
        first.feed(HELLO)
        ports = {"/dev/ttyFAKE": first}
        present = ["/dev/ttyFAKE"]
        svc, log, opened = make_service(clock, ports, scanner=lambda: list(present))
        svc.tick()
        svc.tick()
        self.assertEqual(len(first.frames()), 1)
        first.unplug()
        present.clear()
        svc.tick()
        self.assertEqual(log[-1][0], "disconnect")
        self.assertIsNone(svc.path)
        for _ in range(20):
            svc.tick()
        self.assertEqual(opened, ["/dev/ttyFAKE"], "nothing to open while the port is gone")
        second = FakePort(auto=True)
        second.feed(HELLO)
        ports["/dev/ttyFAKE"] = second
        present.append("/dev/ttyFAKE")
        clock.advance(cyd.RESCAN_SECONDS)
        svc.tick()
        svc.tick()
        self.assertEqual(log[-1], ("connect", "/dev/ttyFAKE"))
        self.assertEqual([f["seq"] for f in second.frames()], [1])

    def test_silent_port_is_backed_off(self):
        clock = FakeClock()
        port = FakePort()
        svc, log, opened = make_service(clock, {"/dev/ttyOTHER": port})
        svc.tick()
        self.assertIsNone(svc.path)
        self.assertTrue(port.closed)
        self.assertEqual(port.pings(), cyd.PING_TRIES)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0][0], "error")
        self.assertIn("/dev/ttyOTHER", log[0][1])
        clock.advance(cyd.RESCAN_SECONDS + 1)
        svc.tick()
        self.assertEqual(opened, ["/dev/ttyOTHER"], "left alone during the backoff")
        clock.advance(cyd.BACKOFF_SECONDS)
        svc.tick()
        self.assertEqual(opened, ["/dev/ttyOTHER", "/dev/ttyOTHER"])
        self.assertEqual(len(log), 1, "the warning is given once")

    def test_busy_port_is_retried_without_backoff(self):
        clock = FakeClock()
        svc, log, opened = make_service(clock, {"/dev/ttyBUSY": OSError(errno.EBUSY, "busy")})
        svc.tick()
        clock.advance(cyd.RESCAN_SECONDS + 0.5)
        svc.tick()
        self.assertEqual(opened, ["/dev/ttyBUSY", "/dev/ttyBUSY"])
        self.assertEqual(log, [])

    def test_missed_acknowledgements_disconnect(self):
        port = FakePort()
        svc, port, clock, log = connect([headline("h")], port=port)
        for _ in range(200):
            svc.tick()
            if svc.path is None:
                break
        self.assertEqual([f["seq"] for f in port.frames()], [1, 2, 3])
        self.assertEqual(log[-1], ("disconnect", "the display stopped answering"))
        self.assertTrue(port.closed)

    def test_keepalive_ping(self):
        svc, port, clock, log = connect()
        svc.tick()
        svc.tick()
        clock.advance(cyd.KEEPALIVE_SECONDS)
        svc.tick()
        self.assertEqual(port.pings(), 1)
        for _ in range(30):
            svc.tick()
        self.assertEqual(svc.path, "/dev/ttyFAKE")
        self.assertEqual(len(port.frames()), 1)

    def test_board_reboot_redraws(self):
        svc, port, clock, log = connect([headline("h")])
        svc.tick()
        svc.tick()
        port.feed(HELLO)
        svc.tick()
        self.assertEqual([f["seq"] for f in port.frames()], [1, 2])
        self.assertEqual(texts(port.frames()[0]), texts(port.frames()[1]))
        self.assertEqual(log, [("connect", "/dev/ttyFAKE")])

    def test_touch_and_junk_are_ignored(self):
        svc, port, clock, log = connect([headline("h")])
        svc.tick()
        svc.tick()
        port.feed(b"TOUCH 10 20\r\n>>> \r\nERR nope\r\nwarning: off grid\r\n")
        for _ in range(3):
            svc.tick()
        self.assertEqual(len(port.frames()), 1)
        self.assertEqual(log, [("connect", "/dev/ttyFAKE")])

    def test_board_at_the_repl_is_soft_reset(self):
        def repl(line):
            if line == b"PING":
                return (b">>> PING\r\nTraceback (most recent call last):\r\n  File \"<stdin>\", line 1, in <module>\r\n"
                        b"NameError: name 'PING' isn't defined\r\n>>> ")
            if line == b"\x04":
                return b"MPY: soft reboot\r\n" + HELLO
            return None
        clock = FakeClock()
        port = FakePort(responder=repl)
        svc, log, _ = make_service(clock, {"/dev/ttyFAKE": port})
        svc.tick()
        self.assertEqual(log, [("connect", "/dev/ttyFAKE")])
        self.assertEqual(port.resets, 1)

    def test_other_protocol_version_is_refused(self):
        clock = FakeClock()
        port = FakePort()
        port.feed(b"HELLO worldterm-cyd 2\r\n")
        svc, log, _ = make_service(clock, {"/dev/ttyFAKE": port})
        svc.tick()
        self.assertIsNone(svc.path)
        self.assertEqual(len(log), 1)
        self.assertIn("version '2'", log[0][1])

    def test_stop_shows_the_offline_frame(self):
        svc, port, clock, log = connect([headline("h")])
        svc.tick()
        svc.stop()
        svc.run()
        self.assertIn("worldterm stopped", texts(port.frames()[-1]))
        self.assertTrue(port.closed)
        self.assertEqual(log[-1], ("disconnect", "stopped"))

    def test_run_honours_the_cancel_flag(self):
        svc, port, clock, log = connect()
        flags = iter([False, False, True])
        svc.run(cancelled=lambda: next(flags))
        self.assertTrue(port.closed)
        self.assertEqual(log[-1], ("disconnect", "stopped"))

    def test_service_for(self):
        self.assertIsNone(cyd.service_for("off"))
        self.assertIsNone(cyd.unavailable_reason())
        svc = cyd.service_for("/dev/ttyNOPE")
        self.assertIsInstance(svc, CydService)
        svc.tick()     # the port does not exist: nothing happens, nothing is raised
        self.assertIsNone(svc.path)

    def test_command_line_default(self):
        from main import build_parser
        self.assertEqual(build_parser().parse_args([]).cyd, "auto")
        self.assertEqual(build_parser().parse_args(["--cyd", "off"]).cyd, "off")


# ---------------------------------------------------------------------------- the app
async def wait_for_load(app, pilot, timeout=10.0):
    waited = 0.0
    while not app.initial_load_done and waited < timeout:
        await asyncio.sleep(0.1)
        waited += 0.1
    await pilot.pause()
    assert app.initial_load_done, "feed did not load"


async def frame_containing(port, needle, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in port.frames():
            if needle in texts(frame):
                return texts(frame)
        await asyncio.sleep(0.1)
    raise AssertionError(f"no frame containing {needle!r}; got {[texts(f) for f in port.frames()]}")


class CydUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_display_follows_the_list(self):
        port = FakePort(auto=True, on_idle=time.sleep)
        port.feed(HELLO)
        service = CydService(lambda: ["/dev/ttyFAKE"], lambda path: port, clock=lambda: NOW, cycle_seconds=3600)
        app = WorldTerm([FIXTURE], interval=600, data_dir=self.data_dir, weather="off", cyd_service=service)
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            # The fixture has 13 Critical items; 4 are dated 2026-10-10 UTC, the hurricane newest.
            frame = await frame_containing(port, "CRITICAL 1/4")
            self.assertIn("Life-threatening Hurricane Isaias makes", frame)
            self.assertEqual(app.cyd_port, "/dev/ttyFAKE")
            await pilot.pause()
            self.assertIn("CYD", str(app.screen.query_one("#header").renderable))
            hurricane = next(ev for ev in app.store.events.values() if "Isaias" in ev.summary)
            app.screen.acknowledge([hurricane])
            frame = await frame_containing(port, "CRITICAL 1/3")
            self.assertNotIn("Isaias", frame)
            await pilot.press("q")
        for _ in range(50):
            if port.closed:
                break
            await asyncio.sleep(0.1)
        self.assertTrue(port.closed)
        self.assertIn("worldterm stopped", texts(port.frames()[-1]))

    async def test_off_means_no_service(self):
        app = WorldTerm([FIXTURE], interval=600, data_dir=self.data_dir, weather="off")
        self.assertIsNone(app.cyd)
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            self.assertNotIn("CYD", str(app.screen.query_one("#header").renderable))


if __name__ == "__main__":
    unittest.main()
