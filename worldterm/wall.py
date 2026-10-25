"""The wall page: a read-only dashboard for a big screen, served by the running worldterm.

`build_snapshot` turns the two stores into one JSON-ready dict: the rotation of
unacknowledged headlines, a ticker of the newest items, and the statistics the page
charts. `WallServer` serves the bundled page and the latest snapshot over plain HTTP from a
daemon thread. It answers GET and HEAD for two paths and nothing else, so there is no way
to change anything through it. The UI thread builds the snapshot (it owns the stores) and
hands the bytes over under a lock; the server thread never looks at an Event.

HTTP/1.0 on purpose: every response closes its connection, so no handler thread can
outlive a request and quitting never waits for one.
"""
from __future__ import annotations

import json
import socketserver
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Iterable

from . import analytics
from .cyd import Headline, select_headlines
from .nws import NWS_LABEL
from .palette import SEVERITY_COLORS
from .severity import Severity
from .store import Event

DEFAULT_PORT = 8070
HEADLINE_MIN = Severity.MAJOR               # news in the rotation: Critical and Major
WEATHER_HEADLINE_MIN = Severity.CRITICAL    # weather in the rotation: Extreme only (Severe alerts come by the hundred)
HEADLINE_CAP = 60                           # the rotation carries the newest this many; the true total is shown
TICKER = 12
TOP_SOURCES = 8                             # the rest fold into "Other": a fixed palette never needs a 9th hue
TOP_TYPES = 6
POLL_INTERVAL = 0.2                         # serve_forever's select timeout, which bounds stop()
PAGE = "wall.html"
NOT_READY = b'{"ready":false}'


def parse_wall(value: str) -> tuple[str, int] | None:
    """`--wall` as (host, port): "off" -> None, "8070" -> ("", 8070), "127.0.0.1:9000" -> (host, port)."""
    value = value.strip()
    if value == "off":
        return None
    host, sep, port_text = value.rpartition(":")
    if not sep:
        host, port_text = "", value
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"expected PORT, HOST:PORT or off, not {value!r}") from None
    if not 0 <= port <= 65535:   # 0 lets the kernel pick; the header chip shows the real port
        raise ValueError(f"port {port} is out of range")
    return host, port


# ---------------------------------------------------------------------------- snapshot
def _iso(when: datetime | None) -> str | None:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds") if when else None


def _headline(h: Headline) -> dict:
    return {"key": h.key, "kind": h.kind, "severity": h.severity.short, "source": h.source,
            "title": h.title, "when": _iso(h.when)}


def _by_severity(counts: dict[Severity, int]) -> dict[str, int]:
    return {sev.short: counts.get(sev, 0) for sev in analytics.SEVERITY_ORDER}


def _source(row: analytics.SourceRow) -> dict:
    return {"node": row.node, "total": row.total, "outstanding": row.total - row.acknowledged,
            "by_severity": _by_severity(row.by_severity)}


def _weather(events: list[Event]) -> dict:
    s = analytics.weather_summary(events)
    return {
        "active": s.active, "ended": s.ended, "offices": s.offices, "states": s.states, "types": s.types,
        "immediate": s.immediate, "warnings": s.warnings, "updates": s.updates,
        "severity": [{"severity": r.severity.short, "nws": NWS_LABEL.get(r.severity, r.severity.label),
                      "total": r.total, "acknowledged": r.acknowledged}
                     for r in analytics.severity_rows(events) if r.total and r.severity != Severity.CLEAR],
        "kinds": [[kind, n] for kind, n in analytics.kind_counts(events)],
        "types": [{"event": r.event, "level": r.level.short, "total": r.total, "states": len(r.states)}
                  for r in analytics.type_rows(events)[:TOP_TYPES]],
    }


def build_snapshot(news: Iterable[Event], weather: Iterable[Event] | None, *, now: datetime,
                   feeds: int, interval: int, last_refresh: datetime | None,
                   weather_refreshed: datetime | None, next_refresh_at: datetime | None) -> dict:
    """Everything the page shows, as plain JSON types. *weather* is None when weather is off."""
    news = list(news)
    weather = list(weather) if weather is not None else None
    rotation = select_headlines(news, "news", min_severity=HEADLINE_MIN)
    if weather is not None:
        rotation += select_headlines(weather, "weather", min_severity=WEATHER_HEADLINE_MIN)
    rotation.sort(key=lambda h: (h.when, h.key), reverse=True)
    ticker = sorted(news, key=lambda ev: (ev.first_seen, ev.guid), reverse=True)[:TICKER]
    source_rows = analytics.source_rows(news)
    sources = [_source(r) for r in source_rows[:TOP_SOURCES]]
    if len(source_rows) > TOP_SOURCES:
        rest = source_rows[TOP_SOURCES:]
        other = analytics.SourceRow("Other")
        for r in rest:
            other.total += r.total
            other.acknowledged += r.acknowledged
            for sev, n in r.by_severity.items():
                other.by_severity[sev] += n
        sources.append({**_source(other), "folded": len(rest)})
    buckets, older = analytics.arrival_histogram(news, now=now)
    return {
        "ready": True,
        "generated_at": _iso(now),
        "refresh": {"interval": interval, "feeds": feeds, "last_refresh": _iso(last_refresh),
                    "next_refresh_at": _iso(next_refresh_at), "weather_refreshed": _iso(weather_refreshed)},
        "order": [sev.short for sev in analytics.SEVERITY_ORDER],
        "palette": {sev.short: {"bg": bg, "fg": fg, "label": sev.label} for sev, (bg, fg) in SEVERITY_COLORS.items()},
        "headlines": {"total": len(rotation), "items": [_headline(h) for h in rotation[:HEADLINE_CAP]]},
        "ticker": [{"severity": ev.severity.short, "source": ev.node, "title": ev.article.title,
                    "when": _iso(ev.first_seen)} for ev in ticker],
        "severity": [{"severity": r.severity.short, "label": r.severity.label, "total": r.total,
                      "acknowledged": r.acknowledged, "outstanding": r.outstanding}
                     for r in analytics.severity_rows(news)],
        "totals": {"events": len(news), "outstanding": sum(1 for ev in news if not ev.acknowledged),
                   "sources": len(source_rows)},
        "sources": sources,
        "arrivals": {"hours": len(buckets), "buckets": buckets, "older": older,
                     "peak": max(buckets, default=0), "total": sum(buckets)},
        "weather": _weather(weather) if weather is not None else None,
    }


# ---------------------------------------------------------------------------- server
class _Server(ThreadingHTTPServer):
    """Carries the page and the latest snapshot; handlers reach them through `self.server`."""

    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], html: bytes) -> None:
        self.html = html
        self.state = NOT_READY
        self.lock = threading.Lock()
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse DNS lookup for server_name, which can stall
        # on a laptop with a flaky resolver, and this runs on the UI thread at startup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    server_version = "worldterm/0.1"
    sys_version = ""
    server: _Server  # for type checkers

    def do_GET(self) -> None:
        self._serve(with_body=True)

    def do_HEAD(self) -> None:
        self._serve(with_body=False)

    def _serve(self, with_body: bool) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            payload, ctype = self.server.html, "text/html; charset=utf-8"
        elif path == "/api/state":
            with self.server.lock:
                payload = self.server.state
            ctype = "application/json; charset=utf-8"
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if with_body:
            self.wfile.write(payload)

    def _not_allowed(self) -> None:
        # Read-only by construction: nothing but GET and HEAD exists.
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def log_message(self, format: str, *args) -> None:  # noqa: A002  (the stdlib's name)
        pass   # the terminal belongs to Textual


class WallServer:
    """Serves the wall page and the latest snapshot. Binds in the constructor (OSError if it cannot)."""

    def __init__(self, host: str = "", port: int = DEFAULT_PORT, *, html: bytes | None = None) -> None:
        if html is None:
            html = resources.files("worldterm").joinpath(PAGE).read_bytes()
        self._server = _Server((host, port), html)
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def label(self) -> str:
        """":8070" when listening everywhere, else "host:port": for the header chip."""
        host, port = self.address
        return f":{port}" if host in ("", "0.0.0.0") else f"{host}:{port}"

    def update(self, snapshot: dict) -> None:
        """Replace what `/api/state` returns. Called on the UI thread; cheap."""
        data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._server.lock:
            self._server.state = data

    def start(self) -> None:
        if self._thread is None and not self._stopped:
            self._thread = threading.Thread(target=self._server.serve_forever, name="wall", daemon=True,
                                            kwargs={"poll_interval": POLL_INTERVAL})
            self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the port. Returns within POLL_INTERVAL; safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        if self._thread is not None:
            # shutdown() waits for serve_forever to notice; it would wait forever if the loop
            # had never been started, hence the guard on the thread.
            self._server.shutdown()
        self._server.server_close()
