"""Textual application: the news event list, the weather alert list, and the fetch workers."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.worker import get_current_worker

from .. import feed, nws
from ..article import Body
from ..cyd import CydService, select_critical, service_for
from ..wall import WallServer, build_snapshot, parse_wall
from ..severity import Severity
from ..sources import Source
from ..store import Event, Store
from .dashboard import DashboardScreen
from .listscreen import EventListScreen, HelpScreen, note_template  # noqa: F401  (re-exported)
from .styles import SEVERITY_STYLE, clip, fmt_time

ARTICLE_CACHE = 50   # article texts kept for the session, the least recently read dropped first


class MainScreen(EventListScreen):
    """The news feed list. Also owns the refresh timer."""

    NAME = "WORLDTERM"
    DASHBOARD_PAGE = "news"
    BINDINGS = EventListScreen.BINDINGS + [Binding("w", "weather", "Weather")]

    def __init__(self) -> None:
        super().__init__()
        self._wx_span: tuple[int, int] | None = None  # header cells of the WX lamp

    @property
    def store(self) -> Store:
        return self.wt.store

    def on_mount(self) -> None:
        super().on_mount()
        # The screen is composed now, so it is safe to start fetching.
        self.set_interval(self.wt.interval, self.wt.start_fetch)
        self.wt.start_fetch()
        # The wall page's hourly arrivals window moves with the clock between saves.
        self.set_interval(60, self.wt._sync_wall)

    def header_extra(self, text: Text) -> None:
        wt = self.wt
        self._wx_span = None
        if wt.weather_url is not None:
            alerts = [e for e in wt.weather_store.events.values() if e.severity != Severity.CLEAR]
            top = max((e.severity for e in alerts), default=None)
            start = text.cell_len
            text.append(f" WX {len(alerts)} ", style=SEVERITY_STYLE[top] if top is not None else "dim")
            self._wx_span = (start, text.cell_len)
            text.append(" ")
        if wt.cyd is not None:
            # Lit while the display is connected; a label, not only a color (principle 4).
            text.append(" CYD ", style=SEVERITY_STYLE[Severity.CLEAR] if wt.cyd_port else "dim")
            text.append(" ")
        if wt.wall is not None:
            text.append(f" WALL {wt.wall.label} ", style=SEVERITY_STYLE[Severity.CLEAR])
            text.append(" ")
        elif wt.wall_error:
            text.append(" WALL ", style="dim")
            text.append(" ")

    def header_clicked(self, x: int) -> None:
        if self._wx_span and self._wx_span[0] <= x < self._wx_span[1]:
            self.action_weather()
        else:
            self.action_dashboard()

    def action_weather(self) -> None:
        if self.wt.weather_url is None:
            self.notify("Weather alerts are off: run with --weather us (or a list of states)", severity="warning")
            return
        self.app.push_screen(WeatherScreen())


class WeatherScreen(EventListScreen):
    """NWS alerts as events: Node is the issuing office, Summary the event and its area."""

    NAME = "WEATHER"
    DASHBOARD_PAGE = "weather"
    COLUMNS = [
        ("Sel", 1), ("Sev", 3), ("A", 1), ("R", 1), ("N", 1), ("Office", 14), ("Summary", None),
        ("Tally", 5), ("Issued", 11), ("Expires", 11),
    ]
    LAMPS = EventListScreen.LAMPS + (Severity.CLEAR,)
    BINDINGS = EventListScreen.BINDINGS + [Binding("w", "back", "News")]

    @property
    def store(self) -> Store:
        return self.wt.weather_store

    def cells(self, ev: Event, summary_w: int) -> list[str]:
        a = ev.alert
        return [
            "●" if ev.guid in self.selection else "",
            ev.severity.short, "A" if ev.acknowledged else "", "R" if ev.read else "", "N" if ev.note else "",
            clip(ev.node, 14), clip(ev.summary, summary_w), str(ev.tally).rjust(5),
            fmt_time(a.sent if a else ev.first_seen), fmt_time(a.active_until if a else None),
        ]

    def header_left(self) -> Text:
        evs = list(self.store.events.values())
        active = [e for e in evs if e.severity != Severity.CLEAR]
        ended = len(evs) - len(active)
        offices = {e.node for e in active}
        states = {s for e in active if e.alert for s in e.alert.states}
        text = Text.assemble((f" {self.NAME} ", "bold"),
                             (f" Alerts: {len(active)}" + (f" (+{ended} ended)" if ended else ""), ""))
        text.append(f"  Offices: {len(offices)}  States: {len(states)}  ")
        return text

    def refreshed_at(self) -> datetime | None:
        return self.wt.weather_refreshed

    def time_key(self, ev: Event) -> datetime:
        return (ev.alert.sent if ev.alert and ev.alert.sent else None) or ev.last_seen

    def action_back(self) -> None:
        self.dismiss(None)

    def action_clear_selection(self) -> None:
        if self.selection:
            super().action_clear_selection()
        else:
            self.action_back()


class WorldTerm(App[None]):
    TITLE = "worldterm"

    def __init__(self, feeds: list[str | Source], interval: int = 300, data_dir: Path | str | None = None,
                 timeout: float = 15.0, weather: str | None = nws.DEFAULT_AREA,
                 weather_url: str | None = None, cyd: str = "off",
                 cyd_service: CydService | None = None, wall: str = "off",
                 wall_server: WallServer | None = None,
                 article_loader: Callable[[str], Body] | None = None) -> None:
        super().__init__()
        # Plain URLs keep the feed's own channel title as the Node name.
        self.feeds: list[Source] = [f if isinstance(f, Source) else Source(key=f, label="", url=f) for f in feeds]
        self.interval = max(15, int(interval))
        self.timeout = timeout
        self.store = Store(data_dir)
        self.weather_store = Store(data_dir, filename=nws.STATE_FILE)
        self.weather_url = weather_url if weather_url else nws.alerts_url(weather)
        self.last_refresh: datetime | None = None
        self.weather_refreshed: datetime | None = None
        self.next_refresh_at = time.monotonic() + self.interval
        self.initial_load_done = False
        self.weather_loaded = False
        self.fetching = False
        self.weather_fetching = False
        self._new_since_refresh = 0
        self._new_alerts = 0
        # The CYD display. "off" by default, so tests and other callers never touch a serial
        # port; main.py passes the command line's "auto". A ready-made service is for tests.
        self.cyd: CydService | None = cyd_service if cyd_service is not None else service_for(cyd)
        self.cyd_port: str | None = None
        if self.cyd is not None:
            self.cyd.on_connect = partial(self._from_thread, self._cyd_connected)
            self.cyd.on_disconnect = partial(self._from_thread, self._cyd_disconnected)
            self.cyd.on_error = partial(self._from_thread, self._cyd_error)
        # The wall page. Also "off" by default; main.py passes the command line's value.
        # A ready-made server (bound to port 0) is for tests.
        self._wall_mode = wall
        self.wall: WallServer | None = wall_server
        self.wall_error: str | None = None
        # Article text for the reader. None, the default, never fetches a page, so tests and
        # other callers stay offline; main.py passes article.load.
        self.article_loader = article_loader
        self._articles: OrderedDict[str, Body] = OrderedDict()
        self._article_waiters: dict[str, list[Callable[[str, Body | str], None]]] = {}

    # ------------------------------------------------------------------ lifecycle
    def on_mount(self) -> None:
        self.store.load()
        self.weather_store.load()
        self.push_screen(MainScreen())
        if self.cyd is not None:
            self.run_cyd()
        if self.wall is not None or self._wall_mode != "off":
            self.call_after_refresh(self._start_wall)

    async def action_quit(self) -> None:
        self.save()
        if self.cyd is not None:
            self.cyd.stop()
        if self.wall is not None:
            self.wall.stop()
        self.exit()

    def action_start_fetch(self) -> None:
        self.start_fetch()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    @property
    def dashboard(self) -> DashboardScreen | None:
        """The dashboard, when it is the screen on top."""
        screen = self.screen
        return screen if isinstance(screen, DashboardScreen) and screen.is_mounted else None

    @property
    def main(self) -> MainScreen | None:
        return self._mounted(MainScreen)

    @property
    def weather(self) -> WeatherScreen | None:
        return self._mounted(WeatherScreen)

    def _mounted(self, kind):
        for screen in self.screen_stack:
            if isinstance(screen, kind) and screen.is_mounted:
                return screen
        return None

    @property
    def busy(self) -> bool:
        return self.fetching or self.weather_fetching

    # Convenience views of the news list's state (used by tests and older callers).
    @property
    def selection(self) -> set[str]:
        main = self.main
        return main.selection if main else set()

    @property
    def sort_by_time(self) -> bool:
        main = self.main
        return bool(main and main.sort_by_time)

    def visible_events(self) -> list[Event]:
        main = self.main
        return main.visible_events() if main else []

    def save(self) -> None:
        try:
            self.store.save()
            self.weather_store.save()
        except OSError as exc:
            self.notify(f"Cannot save state: {exc}", severity="error")
        # Every change of state passes through here, so the displays are told here.
        self._sync_cyd()
        self._sync_wall()

    # ------------------------------------------------------------------ fetching
    def start_fetch(self) -> None:
        if not self.fetching:
            self.fetching = True
            self._new_since_refresh = 0
            self.fetch_feeds()
        if self.weather_url and not self.weather_fetching:
            self.weather_fetching = True
            self._new_alerts = 0
            self.fetch_alerts(self.weather_url)
        self._render_headers()

    def _render_headers(self) -> None:
        for screen in (self.main, self.weather):
            if screen:
                screen.render_header()

    def _load_source(self, src: Source):
        articles = feed.load(src.url, timeout=self.timeout)
        if src.label:
            for article in articles:
                article.source = src.label
        return articles

    @work(thread=True, exclusive=True, group="fetch")
    def fetch_feeds(self) -> None:
        # Fetch in parallel so one slow or dead feed doesn't hold up the rest;
        # each feed's events appear as soon as that feed arrives.
        sources = list(self.feeds)
        with ThreadPoolExecutor(max_workers=min(8, len(sources)) or 1) as pool:
            futures = {pool.submit(self._load_source, src): src for src in sources}
            for future in as_completed(futures):
                src = futures[future]
                name = src.label or src.url
                try:
                    articles = future.result()
                except feed.FeedError as exc:
                    self.call_from_thread(self._feed_error, name, str(exc))
                except Exception as exc:  # defensive: never kill the worker
                    self.call_from_thread(self._feed_error, name, f"{type(exc).__name__}: {exc}")
                else:
                    self.call_from_thread(self._apply_articles, src.url, articles)
        self.call_from_thread(self._fetch_done)

    @work(thread=True, exclusive=True, group="weather")
    def fetch_alerts(self, url: str) -> None:
        try:
            alerts = nws.load(url, timeout=self.timeout)
        except feed.FeedError as exc:
            self.call_from_thread(self._feed_error, "NWS alerts", str(exc))
        except Exception as exc:  # defensive: never kill the worker
            self.call_from_thread(self._feed_error, "NWS alerts", f"{type(exc).__name__}: {exc}")
        else:
            self.call_from_thread(self._apply_alerts, alerts)
        self.call_from_thread(self._weather_done)

    def _apply_articles(self, url: str, articles) -> None:
        fresh = self.store.merge(articles)
        if self.initial_load_done:
            self._new_since_refresh += len(fresh)
        else:
            for ev in fresh:
                ev.new = False
        if self.main:
            self.main.rebuild()
        if self.dashboard:
            self.dashboard.refresh_data()
        self.save()

    def _apply_alerts(self, alerts: list[nws.Alert]) -> None:
        fresh, _cleared = nws.apply_alerts(self.weather_store, alerts)
        if self.weather_loaded:
            self._new_alerts += len(fresh)
        else:
            for ev in fresh:
                ev.new = False
        if self.weather:
            self.weather.rebuild()
        if self.main:
            self.main.render_header()  # the WX lamp
        if self.dashboard:
            self.dashboard.refresh_data()
        self.save()

    def _feed_error(self, name: str, message: str) -> None:
        if name not in message:
            message = f"{name}: {message}"
        self.notify(f"Feed error: {message}", severity="error", timeout=8)

    def _fetch_done(self) -> None:
        self.fetching = False
        self.last_refresh = datetime.now()
        self.next_refresh_at = time.monotonic() + self.interval
        if not self.initial_load_done:
            self.initial_load_done = True
            self.notify(f"Loaded {len(self.store.events)} events")
        elif self._new_since_refresh:
            n = self._new_since_refresh
            self.notify(f"{n} new event{'s' if n != 1 else ''}")
        self._render_headers()
        if self.dashboard:
            self.dashboard.refresh_data()
        self._sync_wall()   # the refresh times changed

    def _weather_done(self) -> None:
        self.weather_fetching = False
        self.weather_refreshed = datetime.now()
        if not self.weather_loaded:
            self.weather_loaded = True
            active = sum(1 for e in self.weather_store.events.values() if e.severity != Severity.CLEAR)
            self.notify(f"Loaded {active} weather alerts")
        elif self._new_alerts:
            n = self._new_alerts
            self.notify(f"{n} new weather alert{'s' if n != 1 else ''}")
        self._render_headers()
        if self.dashboard:
            self.dashboard.refresh_data()
        self._sync_wall()

    # ------------------------------------------------------------------ CYD display
    @work(thread=True, exclusive=True, group="cyd")
    def run_cyd(self) -> None:
        """The display loop. It watches the worker's cancel flag, so quitting never waits on it."""
        worker = get_current_worker()
        assert self.cyd is not None
        self.cyd.run(cancelled=lambda: worker.is_cancelled)

    def _from_thread(self, callback, *args) -> None:
        """Run *callback* on the UI thread for a background thread. A finished app is not an error."""
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:  # "App is not running"
            pass

    def _sync_cyd(self) -> None:
        """Hand the display today's candidates. Cheap: the service only compares unless they changed."""
        if self.cyd is None:
            return
        self.cyd.update(select_critical(self.store.events.values(), "news")
                        + select_critical(self.weather_store.events.values(), "weather"))

    def _cyd_connected(self, path: str) -> None:
        self.cyd_port = path
        self.notify(f"CYD display connected on {path}")
        self._render_headers()

    def _cyd_disconnected(self, reason: str) -> None:
        if self.cyd_port is not None and reason != "stopped":
            self.notify(f"CYD display disconnected: {reason}", severity="warning")
        self.cyd_port = None
        self._render_headers()

    def _cyd_error(self, message: str) -> None:
        self.notify(message, severity="error", timeout=8)

    # ------------------------------------------------------------------ wall page
    def _start_wall(self) -> None:
        """Bind and start the wall server; a port that cannot be taken costs a toast, nothing else."""
        if self.wall is None:
            target = parse_wall(self._wall_mode)
            if target is None:
                return
            try:
                self.wall = WallServer(*target)
            except OSError as exc:
                self.wall_error = f"{target[0] or '0.0.0.0'}:{target[1]}: {exc.strerror or exc}"
                self.notify(f"Wall page: cannot listen on {self.wall_error}", severity="error", timeout=8)
                self._render_headers()
                return
        self.wall.start()
        self._sync_wall()
        self._render_headers()

    def _sync_wall(self) -> None:
        """Hand the wall server a fresh snapshot of both stores."""
        if self.wall is None:
            return
        now = datetime.now(timezone.utc)
        self.wall.update(build_snapshot(
            self.store.events.values(),
            self.weather_store.events.values() if self.weather_url else None,
            now=now, feeds=len(self.feeds), interval=self.interval, last_refresh=self.last_refresh,
            weather_refreshed=self.weather_refreshed,
            next_refresh_at=now + timedelta(seconds=max(0.0, self.next_refresh_at - time.monotonic())),
        ))

    # ------------------------------------------------------------------ article text
    def request_article(self, link: str, callback: Callable[[str, Body | str], None]) -> None:
        """Hand *callback* the article at *link*, or why there is none, on the UI thread.

        A cached article is handed over at once. Otherwise one download thread per link
        fetches it, however many readers ask meanwhile.
        """
        if self.article_loader is None:
            return
        body = self._articles.get(link)
        if body is not None:
            self._articles.move_to_end(link)
            callback(link, body)
            return
        waiters = self._article_waiters.setdefault(link, [])
        waiters.append(callback)
        if len(waiters) == 1:
            # A daemon thread, not a Textual worker: those run in the event loop's executor,
            # which is joined at exit, and a download cannot notice being cancelled. A page
            # still loading must never hold up q.
            threading.Thread(target=self._fetch_article, args=(link,), name="article", daemon=True).start()

    def _fetch_article(self, link: str) -> None:
        """Runs on the download thread: fetch and extract, then report to the UI thread."""
        assert self.article_loader is not None
        try:
            result: Body | str = self.article_loader(link)
        except feed.FeedError as exc:
            result = str(exc)
        except Exception as exc:  # defensive: a parser bug must not leave the reader waiting
            result = f"{type(exc).__name__}: {exc}"
        self._from_thread(self._article_done, link, result)

    def _article_done(self, link: str, result: Body | str) -> None:
        if isinstance(result, Body):   # failures are not kept, so opening the reader again retries
            self._articles[link] = result
            while len(self._articles) > ARTICLE_CACHE:
                self._articles.popitem(last=False)
        for callback in self._article_waiters.pop(link, []):
            callback(link, result)


# Backwards-compatible alias used by main.py
App = WorldTerm  # noqa: N816
