"""The CYD display: today's unacknowledged Critical headlines on a Cheap Yellow Display.

The board (cyd/main.py) is a dumb display. It draws rows of text it is sent, acknowledges
each frame, and shows a notice when the host goes quiet. Everything else happens here:
choosing the headlines, wrapping them into 40-column frames, cycling through them,
finding the board when it is plugged in and letting go when it is pulled.

The board is reached over USB serial (`serialport`) or Bluetooth LE (`bleport`); both carry
the same lines, so the service only differs in a few timings per transport.

`CydService` runs on a worker thread. The UI thread hands it `Headline` snapshots through
`update()` and hears back through the `on_connect`, `on_disconnect` and `on_error`
callbacks, which are called on the worker thread.

Wire protocol, newline-delimited ASCII (`cyd/README.md` has the details):

    host -> board                        board -> host
    PING                                 PONG worldterm-cyd 1        reply; "1" is the protocol version
    SHOW {"seq":n,"rows":[...],"led":..} OK n                        the frame has been drawn
    CLEAR                                HELLO worldterm-cyd 1       at boot only
                                         ERR text / TOUCH x y        bad line / a tap (ignored here)
"""
from __future__ import annotations

import json
import re
import textwrap
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Protocol

from .severity import Severity
from .store import Event

PROTOCOL = "worldterm-cyd"
PROTOCOL_VERSION = "1"
COLS = 40            # the 8x8 font gives 40 columns on a 320 px wide screen
ROWS = 24            # 10 px row pitch on 240 px
TITLE_ROW = 4        # first row of the wrapped headline
TITLE_ROWS = 16      # rows 4..19; 22 is the footer, 23 belongs to the board's own notice
BAUD = 115200

READ_TIMEOUT = 0.2        # the longest any read blocks
IDLE_WAIT = 0.25          # the longest sleep while disconnected, so a stop is noticed quickly
HELLO_WAIT = 3.0          # a board that reboots when the port is opened announces itself within this
PING_WAIT = 1.0
PING_TRIES = 3
OK_WAIT = 2.0             # the board acknowledges a frame within this
MAX_MISSES = 3            # unanswered commands in a row before the board counts as gone
CYCLE_SECONDS = 8.0       # how long each headline stays on screen
KEEPALIVE_SECONDS = 5.0   # PING cadence while nothing changes
RESCAN_SECONDS = 2.0      # how often to look for a board while disconnected
BACKOFF_SECONDS = 30.0    # how long to leave a port alone after it failed to answer
WRITE_CHUNK = 64          # the board's input buffer is about 256 bytes and it reads a byte at a time
WRITE_GAP = 0.02
RX_LIMIT = 4096           # a line longer than this (the REPL prompt has no newline) is dropped
REPL_RESCUE = True        # soft-reset a board found sitting at its REPL, so main.py runs again
BLE_RESCAN_SECONDS = 10.0  # a connect attempt keeps the radio busy, so a missing BLE board is tried less often

NOT_DISPLAYABLE = "(headline not displayable in ASCII)"


# ---------------------------------------------------------------------------- snapshot
@dataclass(frozen=True)
class Headline:
    """What the display needs to know about one event. Built on the UI thread, read on the worker."""

    kind: str          # "news" or "weather"
    guid: str
    title: str
    source: str
    when: datetime     # published time, or when worldterm first saw it
    severity: Severity = Severity.CRITICAL

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.guid}"   # the two stores may share a guid


Row = tuple[int, str, str, str]   # row number, text, foreground, background


@dataclass(frozen=True)
class Frame:
    """One screen: rows of 40-column text, and the RGB LED. Rows not listed are blank."""

    rows: tuple[Row, ...]
    led: str = "off"


def select_headlines(events: Iterable[Event], kind: str, *,
                     min_severity: Severity = Severity.CRITICAL) -> list[Headline]:
    """The unacknowledged events at *min_severity* or above that are still live, newest first."""
    out = []
    for ev in events:
        if ev.severity < min_severity or ev.acknowledged or ev.cleared_at is not None:
            continue
        source = f"NWS {ev.node}" if kind == "weather" else ev.node
        out.append(Headline(kind, ev.guid, ev.article.title, source, ev.article.published or ev.first_seen,
                            ev.severity))
    out.sort(key=lambda h: (h.when, h.key), reverse=True)
    return out


def select_critical(events: Iterable[Event], kind: str) -> list[Headline]:
    """The unacknowledged Critical events that are still live, newest first (what the CYD shows)."""
    return select_headlines(events, kind, min_severity=Severity.CRITICAL)


def todays(headlines: Iterable[Headline], now: datetime) -> list[Headline]:
    """The headlines dated the same calendar day as *now*, in its timezone, newest first."""
    today = now.date()
    picked = [h for h in headlines if h.when.astimezone(now.tzinfo).date() == today]
    picked.sort(key=lambda h: (h.when, h.key), reverse=True)
    return picked


# ---------------------------------------------------------------------------- rendering
_REPLACEMENTS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "…": "...", " ": " ", "•": "*", "×": "x", "€": "EUR", "£": "GBP",
})
_NOT_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def ascii_fold(text: str) -> str:
    """*text* with only the ASCII the board's font can draw.

    Accents are stripped and typographic punctuation replaced. A headline that loses most
    of its letters (a non-Latin script) becomes a placeholder rather than a few stray digits.
    """
    folded = unicodedata.normalize("NFKD", text.translate(_REPLACEMENTS)).encode("ascii", "ignore").decode()
    folded = " ".join(folded.split())
    letters = sum(c.isalpha() for c in text)
    if letters and sum(c.isalpha() for c in folded) < letters / 2:
        return NOT_DISPLAYABLE
    return folded


def wrap(text: str, width: int = COLS, max_rows: int = TITLE_ROWS) -> list[str]:
    """*text* as rows of at most *width* characters; the last row ends in "..." if cut."""
    rows = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=True) or [""]
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        rows[-1] = rows[-1][: width - 3].rstrip() + "..."
    return rows


def _ends(left: str, right: str, width: int = COLS) -> str:
    """*left* and *right* at the two ends of one row, clipping *left* to make room."""
    left = left[: max(0, width - len(right) - 1)]
    return left + " " * (width - len(left) - len(right)) + right


def _clock(now: datetime) -> str:
    return now.strftime("%a %d %b %H:%M")


def _when(when: datetime, now: datetime) -> str:
    local = when.astimezone(now.tzinfo)
    return local.strftime("%H:%M") if local.date() == now.date() else local.strftime("%d %b %H:%M")


def render_headline(h: Headline, index: int, total: int, now: datetime) -> Frame:
    rows: list[Row] = [
        (0, _ends(f"CRITICAL {index}/{total}", _clock(now)), "w", "r"),
        (2, _ends(ascii_fold(h.source), _when(h.when, now)), "y", "k"),
    ]
    for i, line in enumerate(wrap(ascii_fold(h.title))):
        rows.append((TITLE_ROW + i, line, "w", "k"))
    rows.append((ROWS - 2, f"{total} unacknowledged critical today", "d", "k"))
    return Frame(tuple(rows), led="r")


def render_idle(now: datetime) -> Frame:
    return Frame((
        (0, _ends("WORLDTERM", _clock(now)), "w", "b"),
        (4, "No unacknowledged critical", "d", "k"),
        (5, "headlines today", "d", "k"),
    ))


def render_offline(now: datetime) -> Frame:
    return Frame((
        (0, _ends("WORLDTERM", _clock(now)), "d", "k"),
        (4, f"worldterm stopped {now.strftime('%H:%M')}", "d", "k"),
    ))


# ---------------------------------------------------------------------------- protocol
def encode(frame: Frame, seq: int) -> bytes:
    """A SHOW line. Every row is cut to the screen and to printable ASCII, so nothing the
    board cannot draw, and no control byte, ever goes down the wire."""
    rows = [[row, _NOT_PRINTABLE.sub("?", text)[:COLS], fg, bg]
            for row, text, fg, bg in frame.rows if 0 <= row < ROWS]
    payload = json.dumps({"seq": seq, "rows": rows, "led": frame.led}, ensure_ascii=True, separators=(",", ":"))
    return b"SHOW " + payload.encode("ascii") + b"\n"


def parse_line(line: bytes) -> tuple:
    """A line from the board as ("hello", version), ("pong", version), ("ok", seq),
    ("err", text), ("touch", x, y) or ("other", text)."""
    text = line.rstrip(b"\r").decode("ascii", "replace").strip()
    head, _, rest = text.partition(" ")
    if head in ("HELLO", "PONG"):
        name, _, version = rest.partition(" ")
        if name == PROTOCOL:
            return (head.lower(), version.strip())
    elif head == "OK":
        try:
            return ("ok", int(rest))
        except ValueError:
            pass
    elif head == "ERR":
        return ("err", rest)
    elif head == "TOUCH":
        parts = rest.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            return ("touch", int(parts[0]), int(parts[1]))
    return ("other", text)


# ---------------------------------------------------------------------------- transport seam
class Port(Protocol):
    """What the service needs from a link to the board. `serialport.SerialPort`, `bleport.BlePort`
    and the tests' fake all fit."""

    def read(self, max_bytes: int = 4096, timeout: float = READ_TIMEOUT) -> bytes: ...
    def write(self, data: bytes, timeout: float = 1.0) -> None: ...
    def close(self) -> None: ...


Scanner = Callable[[], list[str]]      # candidate port paths right now
OpenPort = Callable[[str], Port]       # raises OSError when the port cannot be opened


def unavailable_reason(mode: str = "auto") -> str | None:
    """Why there can be no CYD on this host for `--cyd` *mode*, or None."""
    if mode.startswith("ble:"):
        from . import bleport
        try:
            bleport.parse_address(mode[4:])
        except ValueError as exc:
            return str(exc)
        return bleport.unavailable_reason()
    try:
        from . import serialport  # noqa: F401
    except ImportError as exc:
        return f"USB serial needs a Linux host ({exc})"
    return None


def service_for(mode: str) -> CydService | None:
    """A service for `--cyd` *mode*: "off", "auto", a port path (which need not exist yet), or
    "ble:ADDRESS" for a board reached over Bluetooth LE."""
    if mode == "off" or unavailable_reason(mode):
        return None
    if mode.startswith("ble:"):
        from . import bleport
        address = bleport.parse_address(mode[4:])
        stop = threading.Event()
        # A BLE board never greets on connect (only a reboot over USB does), the link has
        # its own flow control, and the connect itself watches the stop flag.
        return CydService(lambda: [f"ble:{address}"],
                          lambda _path: bleport.BlePort(address, cancelled=stop.is_set),
                          hello_wait=0.0, write_chunk=1 << 16, write_gap=0.0,
                          rescan_seconds=BLE_RESCAN_SECONDS, stop=stop)
    from . import serialport
    scanner: Scanner = serialport.find_ports if mode == "auto" else (lambda: [mode])
    return CydService(scanner, serialport.SerialPort)


# ---------------------------------------------------------------------------- service
class CydService:
    """Finds the board, keeps it showing the right frame, and copes with it going away.

    `tick()` does one step and never blocks for more than a fraction of a second; `run()`
    loops over it until `stop()` or *cancelled* says otherwise. The clock, the monotonic
    timer and the sleep are injectable so the tests can drive it without waiting.
    """

    def __init__(self, scanner: Scanner, open_port: OpenPort, *,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None,
                 cycle_seconds: float = CYCLE_SECONDS, hello_wait: float = HELLO_WAIT,
                 write_chunk: int = WRITE_CHUNK, write_gap: float = WRITE_GAP,
                 rescan_seconds: float = RESCAN_SECONDS, stop: threading.Event | None = None) -> None:
        self._scanner = scanner
        self._open_port = open_port
        self._clock = clock
        self._mono = monotonic
        self._stop = stop if stop is not None else threading.Event()
        self._hello_wait = hello_wait
        self._write_chunk = write_chunk
        self._write_gap = write_gap
        self._rescan_seconds = rescan_seconds
        self._idle: Callable[[float], None] = sleep if sleep is not None else self._stop.wait
        self._pause: Callable[[float], None] = sleep if sleep is not None else time.sleep
        self._cycle_seconds = cycle_seconds

        self.on_connect: Callable[[str], None] = lambda path: None
        self.on_disconnect: Callable[[str], None] = lambda reason: None
        self.on_error: Callable[[str], None] = lambda message: None

        self._lock = threading.Lock()
        self._snapshot: tuple[Headline, ...] = ()
        self.path: str | None = None
        self._port: Port | None = None
        self._rx = bytearray()
        self._seq = 0
        self._outstanding: tuple[tuple, float] | None = None   # (expected reply, deadline)
        self._misses = 0
        self._needs_redraw = False
        self._last_frame: Frame | None = None
        self._last_tx = 0.0
        self._current_key: str | None = None
        self._cycle_at = 0.0
        self._next_scan = 0.0
        self._backoff: dict[str, float] = {}
        self._reported: set[str] = set()

    # ---------------------------------------------------------------- UI-thread side
    def update(self, headlines: Iterable[Headline]) -> None:
        """Replace the candidate headlines. Cheap when nothing changed; safe from any thread."""
        snapshot = tuple(headlines)
        with self._lock:
            self._snapshot = snapshot

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- worker-thread side
    def run(self, cancelled: Callable[[], bool] = lambda: False) -> None:
        try:
            while not self._stop.is_set() and not cancelled():
                try:
                    self.tick()
                except Exception as exc:  # defensive: never kill the worker
                    self._report(f"CYD display: {type(exc).__name__}: {exc}")
                    self._close(f"{type(exc).__name__}: {exc}")
                    self._idle(IDLE_WAIT)
        finally:
            self._shutdown()

    def tick(self) -> None:
        """One step: look for a board, or talk to the connected one."""
        if self._port is None:
            self._tick_disconnected()
        else:
            try:
                self._tick_connected()
            except OSError as exc:
                self._close(str(exc))

    # ---------------------------------------------------------------- disconnected
    def _tick_disconnected(self) -> None:
        now = self._mono()
        if now < self._next_scan:
            self._idle(min(IDLE_WAIT, self._next_scan - now))
            return
        self._next_scan = now + self._rescan_seconds
        for path in self._scanner():
            if self._backoff.get(path, 0.0) > now:
                continue
            try:
                port = self._open_port(path)
            except OSError as exc:
                # Busy, no permission, or gone again: try at the next scan. A failure that
                # comes with a hint (a BLE link held by the desktop) is one the user can fix.
                hint = getattr(exc, "hint", None)
                if hint:
                    self._report(hint)
                continue
            try:
                answered = self._handshake(port, path)
            except OSError:
                self._close_port(port)
                continue
            if answered:
                self._connected(port, path)
                return
            self._close_port(port)
            self._backoff[path] = self._mono() + BACKOFF_SECONDS
            self._report(f"No worldterm display answered on {path} (is cyd/main.py deployed to the board?)")

    def _handshake(self, port: Port, path: str) -> bool:
        """True when the board on *port* speaks our protocol version."""
        self._rx = bytearray()
        # A board that rebooted when the port was opened says HELLO on its own; a running one
        # must be asked.
        if self._await_reply(port, path, self._hello_wait):
            return True
        for _ in range(PING_TRIES):
            self._write(port, b"PING\n")
            if self._await_reply(port, path, PING_WAIT):
                return True
        return False

    def _await_reply(self, port: Port, path: str, wait: float) -> bool:
        deadline = self._mono() + wait
        rescued = False
        while True:
            remaining = deadline - self._mono()
            if remaining <= 0:
                return False
            for msg in self._read_messages(port, min(READ_TIMEOUT, remaining)):
                if msg[0] in ("hello", "pong"):
                    if msg[1] == PROTOCOL_VERSION:
                        return True
                    self._report(f"The display on {path} speaks protocol version {msg[1]!r}; this worldterm "
                                 f"expects {PROTOCOL_VERSION}. Redeploy cyd/main.py.")
                    self._reported.add(f"No worldterm display answered on {path} (is cyd/main.py deployed to the board?)")
                    return False
                if (REPL_RESCUE and not rescued and msg[0] == "other"
                        and "NameError" in msg[1] and "PING" in msg[1]):
                    # MicroPython's REPL has just tried to evaluate our PING: the board is at its
                    # prompt, as it is after an mpremote session. A soft reset runs main.py again.
                    rescued = True
                    port.write(b"\x04")
                    deadline = self._mono() + HELLO_WAIT

    def _connected(self, port: Port, path: str) -> None:
        self._port, self.path = port, path
        self._seq = 0
        self._misses = 0
        self._outstanding = None
        self._needs_redraw = True
        self._last_frame = None
        self._current_key = None
        self._last_tx = self._mono()
        self.on_connect(path)

    # ---------------------------------------------------------------- connected
    def _tick_connected(self) -> None:
        port = self._port
        assert port is not None
        for msg in self._read_messages(port, READ_TIMEOUT):
            if msg[0] == "ok" and self._expecting(("ok", msg[1])):
                self._outstanding = None
                self._misses = 0
            elif msg[0] == "pong" and self._expecting(("pong",)):
                self._outstanding = None
                self._misses = 0
            elif msg[0] == "hello":
                # The board rebooted: whatever it was showing is gone.
                self._outstanding = None
                self._misses = 0
                self._needs_redraw = True
            # err, touch and anything else are ignored
        now = self._mono()
        if self._outstanding is not None and now >= self._outstanding[1]:
            self._outstanding = None
            self._misses += 1
            self._needs_redraw = True
            if self._misses >= MAX_MISSES:
                raise OSError("the display stopped answering")
        if self._outstanding is not None:
            return
        frame = self._frame(now)
        if self._needs_redraw or frame != self._last_frame:
            self._seq += 1
            self._send(encode(frame, self._seq))
            self._last_frame = frame
            self._needs_redraw = False
            self._outstanding = (("ok", self._seq), self._mono() + OK_WAIT)
        elif now - self._last_tx >= KEEPALIVE_SECONDS:
            self._send(b"PING\n")
            self._outstanding = (("pong",), self._mono() + OK_WAIT)

    def _expecting(self, reply: tuple) -> bool:
        return self._outstanding is not None and self._outstanding[0] == reply

    def _frame(self, now_mono: float) -> Frame:
        """The frame to show now: the current headline, the next one when its time is up, or idle."""
        now = self._clock()
        with self._lock:
            snapshot = self._snapshot
        today = todays(snapshot, now)
        if not today:
            self._current_key = None
            return render_idle(now)
        keys = [h.key for h in today]
        if self._current_key in keys:
            index = keys.index(self._current_key)
            if len(today) > 1 and now_mono >= self._cycle_at:
                index = (index + 1) % len(today)
                self._cycle_at = now_mono + self._cycle_seconds
        else:
            index = 0
            self._cycle_at = now_mono + self._cycle_seconds
        self._current_key = keys[index]
        return render_headline(today[index], index + 1, len(today), now)

    def _read_messages(self, port: Port, timeout: float) -> list[tuple]:
        data = port.read(4096, timeout)
        if not data:
            return []
        self._rx += data
        messages = []
        while True:
            end = self._rx.find(b"\n")
            if end < 0:
                break
            messages.append(parse_line(bytes(self._rx[:end])))
            del self._rx[:end + 1]
        if len(self._rx) > RX_LIMIT:
            self._rx = bytearray()
        return messages

    def _send(self, data: bytes) -> None:
        assert self._port is not None
        self._write(self._port, data)
        self._last_tx = self._mono()

    def _write(self, port: Port, data: bytes) -> None:
        """Write one command, paced for the board's small serial input buffer.

        The leading newline ends whatever half line the board may still be holding (from
        an overflow, or an earlier host), so the command itself is read cleanly; the board
        ignores the empty line.
        """
        data = b"\n" + data
        for start in range(0, len(data), self._write_chunk):
            if start:
                self._pause(self._write_gap)
            port.write(data[start:start + self._write_chunk])

    # ---------------------------------------------------------------- teardown
    def _close(self, reason: str) -> None:
        port, self._port = self._port, None
        if port is not None:
            self._close_port(port)
        self.path = None
        self._outstanding = None
        self._last_frame = None
        self._current_key = None
        self._next_scan = self._mono() + self._rescan_seconds
        if port is not None:
            self.on_disconnect(reason)

    @staticmethod
    def _close_port(port: Port) -> None:
        try:
            port.close()
        except OSError:
            pass

    def _shutdown(self) -> None:
        if self._port is not None:
            try:
                self._send(encode(render_offline(self._clock()), self._seq + 1))
            except OSError:
                pass
            self._close("stopped")

    def _report(self, message: str) -> None:
        """Tell the UI about a problem once; it repeats as long as the board stays unplugged."""
        if message not in self._reported:
            self._reported.add(message)
            self.on_error(message)
