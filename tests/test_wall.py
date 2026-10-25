"""The wall page: the --wall flag, the JSON snapshot, the read-only server, and the app wiring."""
import asyncio
import http.client
import json
import socket
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worldterm import feed, nws, wall
from worldterm.feed import Article
from worldterm.severity import Severity
from worldterm.store import Store
from worldterm.ui.app import WorldTerm

NOW = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"
BBC = (FIXTURES / "bbc_sample.xml").as_uri()
HTML = b"<!doctype html><title>worldterm wall</title><p>test page</p>"


def article(guid, title="Earthquake kills dozens", source="Wire", published=NOW):
    return Article(guid, title, "summary", f"https://x/{guid}", published, source)


def snapshot(news, weather=None, now=NOW, **overrides):
    kwargs = dict(now=now, feeds=1, interval=300, last_refresh=datetime(2026, 10, 10, 11, 58),
                  weather_refreshed=None, next_refresh_at=now + timedelta(minutes=4))
    kwargs.update(overrides)
    return wall.build_snapshot(news, weather, **kwargs)


class ParseTests(unittest.TestCase):
    def test_parse_wall(self):
        self.assertIsNone(wall.parse_wall("off"))
        self.assertEqual(wall.parse_wall("8070"), ("", 8070))
        self.assertEqual(wall.parse_wall("0.0.0.0:80"), ("0.0.0.0", 80))
        self.assertEqual(wall.parse_wall("localhost:9000"), ("localhost", 9000))
        self.assertEqual(wall.parse_wall("0"), ("", 0), "0 means any free port")
        for bad in ("abc", ":", "host:", "1:2:3x", "70000", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                wall.parse_wall(bad)

    def test_command_line_default(self):
        from main import build_parser
        self.assertEqual(build_parser().parse_args([]).wall, "8070")
        self.assertEqual(build_parser().parse_args(["--wall", "off"]).wall, "off")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.load()
        self.store.merge(feed.load(BBC), now=NOW)
        self.news = list(self.store.events.values())

    def tearDown(self):
        self.tmp.cleanup()

    def weather_events(self):
        wx = Store(Path(self.tmp.name), filename="weather.json")
        wx.load()
        nws.apply_alerts(wx, nws.parse((FIXTURES / "nws_sample.json").read_bytes()), now=NOW)
        return wx, list(wx.events.values())

    def test_rotation_is_critical_and_major_news_plus_extreme_weather(self):
        snap = snapshot(self.news)
        self.assertEqual(snap["headlines"]["total"], 25)
        items = snap["headlines"]["items"]
        self.assertEqual({i["severity"] for i in items}, {"CRT", "MAJ"})
        self.assertEqual([i["when"] for i in items], sorted((i["when"] for i in items), reverse=True))
        self.assertTrue(all(i["kind"] == "news" and i["key"].startswith("news:") for i in items))
        _wx, weather = self.weather_events()
        snap = snapshot(self.news, weather)
        self.assertEqual(snap["headlines"]["total"], 26, "one Extreme alert joins; the Severe ones do not")
        tornado = next(i for i in snap["headlines"]["items"] if i["kind"] == "weather")
        self.assertEqual(tornado["severity"], "CRT")
        self.assertTrue(tornado["source"].startswith("NWS "))
        self.assertEqual(snap["weather"]["active"], 23)

    def test_acknowledged_and_cleared_are_left_out(self):
        crit = next(ev for ev in self.news if ev.severity == Severity.CRITICAL)
        self.store.ack(crit.guid)
        self.assertEqual(snapshot(self.news)["headlines"]["total"], 24)
        wx, weather = self.weather_events()
        wx.clear_missing(set(), now=NOW)   # every alert has left the feed
        self.assertEqual(snapshot(self.news, weather)["headlines"]["total"], 24)
        self.assertEqual(snapshot(self.news, weather)["weather"]["active"], 0)

    def test_cap_keeps_the_true_total(self):
        store = Store(Path(self.tmp.name), filename="many.json")
        store.load()
        store.merge([article(f"g{i}", published=NOW - timedelta(minutes=i)) for i in range(70)], now=NOW)
        snap = snapshot(store.events.values())
        self.assertEqual(snap["headlines"]["total"], 70)
        self.assertEqual(len(snap["headlines"]["items"]), wall.HEADLINE_CAP)
        self.assertEqual(snap["headlines"]["items"][0]["key"], "news:g0")

    def test_ticker_sources_and_folding(self):
        store = Store(Path(self.tmp.name), filename="many.json")
        store.load()
        articles = [article(f"s{s}-{i}", source=f"Source {s:02d}") for s in range(10) for i in range(s + 1)]
        store.merge(articles, now=NOW)
        snap = snapshot(store.events.values())
        self.assertEqual(len(snap["ticker"]), wall.TICKER)
        self.assertEqual(len(snap["sources"]), wall.TOP_SOURCES + 1)
        self.assertEqual(snap["sources"][0]["node"], "Source 09")
        other = snap["sources"][-1]
        self.assertEqual((other["node"], other["folded"], other["total"]), ("Other", 2, 3))
        self.assertEqual(snap["totals"], {"events": 55, "outstanding": 55, "sources": 10})
        self.assertEqual(set(snap["sources"][0]["by_severity"]), set(snap["order"]))

    def test_statistics_and_refresh_block(self):
        snap = snapshot(self.news)
        by = {r["severity"]: r for r in snap["severity"]}
        self.assertEqual((by["CRT"]["total"], by["MAJ"]["total"]), (13, 12))
        self.assertEqual(by["CRT"]["label"], "Critical")
        self.assertEqual(snap["arrivals"]["hours"], 24)
        self.assertEqual(snap["arrivals"]["buckets"][-1], 35, "all first seen at NOW")
        self.assertEqual(snap["arrivals"]["peak"], 35)
        self.assertEqual(snap["order"], ["CRT", "MAJ", "MIN", "WRN", "IND", "CLR"])
        self.assertEqual(snap["palette"]["CRT"], {"bg": "#f4005f", "fg": "#ffffff", "label": "Critical"})
        self.assertIsNone(snap["weather"])
        self.assertEqual(snap["refresh"]["interval"], 300)
        self.assertTrue(snap["refresh"]["next_refresh_at"].endswith("+00:00"))
        self.assertTrue(snap["refresh"]["last_refresh"].endswith("+00:00"), "naive local times become UTC")
        self.assertIsNone(snapshot(self.news, last_refresh=None, next_refresh_at=None)["refresh"]["last_refresh"])

    def test_json_safe_and_titles_verbatim(self):
        store = Store(Path(self.tmp.name), filename="many.json")
        store.load()
        store.merge([article("x", title='Breaking: <script>alert("x")</script> & "quotes" — war')], now=NOW)
        _wx, weather = self.weather_events()
        snap = snapshot(list(store.events.values()), weather)
        text = json.dumps(snap)   # no datetimes, sets or enums left behind
        self.assertIn('<script>alert(\\"x\\")</script>', text)
        self.assertEqual(snap["headlines"]["items"][0]["title"], 'Breaking: <script>alert("x")</script> & "quotes" — war')
        self.assertNotIn("link", text)
        self.assertNotIn("note", text.replace('"noted"', ""))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = wall.WallServer("127.0.0.1", 0, html=HTML)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def request(self, method, path):
        conn = http.client.HTTPConnection(*self.server.address, timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()

    def test_page_and_state(self):
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertIn(b"worldterm", body)
        self.assertEqual(self.request("GET", "/index.html")[1], body)
        resp, body = self.request("GET", "/api/state")
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(json.loads(body), {"ready": False})
        self.server.update({"ready": True, "title": "héllo"})
        resp, body = self.request("GET", "/api/state?x=1")
        self.assertEqual(json.loads(body), {"ready": True, "title": "héllo"})
        self.assertEqual(int(resp.getheader("Content-Length")), len(body))
        self.assertEqual(self.server.label, f"127.0.0.1:{self.server.address[1]}")

    def test_head_404_and_read_only(self):
        resp, body = self.request("HEAD", "/api/state")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(resp.getheader("Content-Length")), len(b'{"ready":false}'))
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)
        self.assertEqual(self.request("GET", "/../etc/passwd")[0].status, 404)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            resp, body = self.request(method, "/api/state")
            self.assertEqual(resp.status, 405, method)
            self.assertEqual(resp.getheader("Allow"), "GET, HEAD")
        self.assertNotIn("Python", self.request("GET", "/")[0].getheader("Server"))

    def test_stop_is_prompt_and_releases_the_port(self):
        address = self.server.address
        started = time.monotonic()
        self.server.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.server.stop()   # twice is fine
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=1).close()
        again = wall.WallServer(*address, html=HTML)   # the port can be taken straight away
        again.stop()

    def test_port_in_use_and_stop_without_start(self):
        with self.assertRaises(OSError):
            wall.WallServer(*self.server.address, html=HTML)
        idle = wall.WallServer("127.0.0.1", 0, html=HTML)
        idle.stop()   # never started: must not hang


async def wait_for_load(app, pilot, timeout=10.0):
    waited = 0.0
    while not app.initial_load_done and waited < timeout:
        await asyncio.sleep(0.1)
        waited += 0.1
    await pilot.pause()
    assert app.initial_load_done, "feed did not load"


def fetch_state(address):
    conn = http.client.HTTPConnection(*address, timeout=5)
    try:
        conn.request("GET", "/api/state")
        return json.loads(conn.getresponse().read())
    finally:
        conn.close()


class WallUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_page_follows_the_list(self):
        server = wall.WallServer("127.0.0.1", 0, html=HTML)   # the app starts it
        app = WorldTerm([BBC], interval=600, data_dir=self.data_dir, weather="off", wall_server=server)
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            await pilot.pause()
            state = await asyncio.to_thread(fetch_state, server.address)
            self.assertTrue(state["ready"])
            self.assertEqual(state["headlines"]["total"], 25)
            self.assertEqual(state["totals"]["events"], 35)
            self.assertIsNone(state["weather"])
            self.assertIn(f"WALL {server.label}", str(app.screen.query_one("#header").renderable))
            crit = next(ev for ev in app.store.events.values() if ev.severity == Severity.CRITICAL)
            app.screen.acknowledge([crit])
            await pilot.pause()
            state = await asyncio.to_thread(fetch_state, server.address)
            self.assertEqual(state["headlines"]["total"], 24)
            self.assertEqual(state["totals"]["outstanding"], 34)
            await pilot.press("q")
        with self.assertRaises(OSError):
            socket.create_connection(server.address, timeout=1).close()

    async def test_mode_string_binds_and_a_taken_port_fails_soft(self):
        app = WorldTerm([BBC], interval=600, data_dir=self.data_dir, weather="off", wall="127.0.0.1:0")
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            self.assertIsNotNone(app.wall)
            self.assertEqual(app.wall.address[0], "127.0.0.1")
            state = await asyncio.to_thread(fetch_state, app.wall.address)
            self.assertEqual(state["totals"]["events"], 35)
            await pilot.press("q")
        holder = wall.WallServer("127.0.0.1", 0, html=HTML)   # somebody else has the port
        taken = "127.0.0.1:%d" % holder.address[1]
        try:
            other = WorldTerm([BBC], interval=600, data_dir=self.data_dir, weather="off", wall=taken)
            async with other.run_test(size=(120, 30)) as pilot:
                await wait_for_load(other, pilot)
                self.assertIsNone(other.wall)
                self.assertIn(taken, other.wall_error)
                self.assertIn("WALL", str(other.screen.query_one("#header").renderable))
                await pilot.press("q")
        finally:
            holder.stop()

    async def test_off_by_default(self):
        app = WorldTerm([BBC], interval=600, data_dir=self.data_dir, weather="off")
        self.assertIsNone(app.wall)
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_for_load(app, pilot)
            self.assertIsNone(app.wall)
            self.assertNotIn("WALL", str(app.screen.query_one("#header").renderable))


if __name__ == "__main__":
    unittest.main()
