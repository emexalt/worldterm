"""worldterm's display program for the Cheap Yellow Display. Deploy it to the board as main.py.

It runs at boot and turns the board into a dumb text display: worldterm sends it frames, it
draws them and answers. The same lines arrive over the USB serial port or over Bluetooth LE,
where the board offers the Nordic UART Service (NUS) as "worldterm-cyd"; each reply goes back
the way its command came. It needs board.py, ili9341.py and xpt2046.py on the board,
deployed from the cyd-micropython repository. README.md next to this file has the deploy
steps and the protocol.

    host -> board                                        board -> host
    PING                                                 PONG worldterm-cyd 1
    SHOW {"seq":n,"rows":[[row,text,fg,bg],...],"led":x}  OK n          (drawn)
    CLEAR                                                OK 0
    any other line: a message, shown for 20 s            OK 0
                                                         HELLO worldterm-cyd 1   once, at boot (USB)
                                                         ERR message             a line it could not use
                                                         TOUCH x y               a tap; worldterm ignores it

Rows are 0..22 (row 23 is this program's own notice line), text is at most 40 printable
ASCII characters, fg and bg are palette letters (w r y g b d k). Rows not in a frame are
blank. Ctrl-C still works, so mpremote can interrupt this program to deploy a new one.
"""
import gc
import json
import select
import sys
import time

import board
from ili9341 import color565
from micropython import const

try:
    import bluetooth
except ImportError:      # a firmware built without Bluetooth: USB only
    bluetooth = None

PROTO = "worldterm-cyd 1"
COLS, ROWS, PITCH = 40, 24, 10          # 8x8 font on a 10 px row pitch: 40 x 24 cells on 320 x 240
NOTICE_ROW = ROWS - 1                   # the host never writes this row
STALE_MS = 30_000                       # this long without a line from the host, say so
TOUCH_CONFIRM = 2                       # consecutive readings that make a tap
TOUCH_LOCKOUT_MS = 500
MAX_LINE = 4096
MESSAGE_MS = 20_000                     # a message stays this long, then the last frame returns
MESSAGE_FIRST, MESSAGE_LAST = 2, NOTICE_ROW - 2   # rows 2..21 hold message text

BLE_NAME = "worldterm-cyd"
BLE_MTU = 247
_CONNECT, _DISCONNECT, _WRITE, _MTU = const(1), const(2), const(3), const(21)

PALETTE = {
    "w": color565(255, 255, 255),
    "r": color565(255, 0, 0),
    "y": color565(255, 220, 0),
    "g": color565(0, 200, 0),
    "b": color565(0, 90, 220),
    "d": color565(110, 110, 110),
    "k": 0,
}
LEDS = {"r": (1, 0, 0), "g": (0, 1, 0), "b": (0, 0, 1), "off": (0, 0, 0)}


def pad(text):
    """*text* cut or space-filled to the width of the screen (MicroPython has no ljust)."""
    text = text[:COLS]
    return text + " " * (COLS - len(text))


def printable(text):
    """*text* with anything the 8x8 font cannot draw replaced by '?'."""
    return "".join(c if " " <= c <= "~" else "?" for c in text)


def text_of(raw):
    """A received line as text; bytes that are not UTF-8 become '?' rather than an exception."""
    try:
        return raw.decode().strip()
    except UnicodeError:
        return "".join(chr(b) if 32 <= b < 127 else "?" for b in raw).strip()


def wrap(text, width=COLS):
    """*text* as rows of at most *width* characters, broken at spaces where it can be."""
    rows, line = [], ""
    for word in text.split():
        while len(word) > width:
            if line:
                rows.append(line)
                line = ""
            rows.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            rows.append(line)
            line = word
    if line or not rows:
        rows.append(line)
    return rows


def send_usb(line):
    sys.stdout.write(line + "\r\n")


class Panel:
    """The screen as 24 rows of 40 cells. Only rows that changed are redrawn, so frames do not flicker."""

    def __init__(self, display):
        self.d = display
        self.rows = {}      # row -> (text, fg, bg) as last drawn

    def _draw(self, row, text, fg, bg):
        y = row * PITCH
        previous_bg = self.rows.get(row, (None, None, "k"))[2]
        if bg != "k" or previous_bg != "k" or not text.strip():
            # The text box is only 8 px tall; the band between rows needs the background too.
            self.d.fill_rectangle(0, y, COLS * 8, PITCH, PALETTE[bg])
        if text.strip():
            self.d.draw_text8x8(0, y + 1, text, PALETTE[fg], PALETTE[bg])
        self.rows[row] = (text, fg, bg)

    def show(self, rows):
        wanted = {}
        for item in rows:
            row, text = int(item[0]), str(item[1])
            fg = item[2] if len(item) > 2 and item[2] in PALETTE else "w"
            bg = item[3] if len(item) > 3 and item[3] in PALETTE else "k"
            if 0 <= row < NOTICE_ROW:
                wanted[row] = (pad(text), fg, bg)
        for row in range(NOTICE_ROW):
            cell = wanted.get(row, (" " * COLS, "k", "k"))
            if self.rows.get(row) != cell:
                self._draw(row, *cell)

    def notice(self, text):
        self._draw(NOTICE_ROW, pad(text), "d", "k")

    def clear_notice(self):
        if self.rows.get(NOTICE_ROW, ("", "", ""))[0].strip():
            self._draw(NOTICE_ROW, " " * COLS, "k", "k")


class Screen:
    """What is on the panel: worldterm's last frame, or for a while a message on top of it."""

    def __init__(self, panel, boot_rows):
        self.panel = panel
        self.frame = (boot_rows, "off")     # what worldterm last asked for
        self.messages = []
        self.message_until = None
        self._show(*self.frame)

    def _show(self, rows, led):
        self.panel.show(rows)
        board.led(*LEDS.get(led, LEDS["off"]))

    def show_frame(self, rows, led):
        """A frame from worldterm. It also ends a message early."""
        self.frame = (rows, led)
        self.messages = []
        self.message_until = None
        self._show(rows, led)

    def message(self, text, via):
        """Show *text* (and the messages before it, while they last) for MESSAGE_MS."""
        self.messages.append(printable(text))
        body = []
        for m in self.messages:
            if body:
                body.append("")
            body.extend(wrap(m))
        body = body[-(MESSAGE_LAST - MESSAGE_FIRST + 1):]
        rows = [(0, "MESSAGE via " + via, "k", "y")]
        rows.extend((MESSAGE_FIRST + i, line, "w", "k") for i, line in enumerate(body))
        self._show(rows, "b")
        self.message_until = time.ticks_add(time.ticks_ms(), MESSAGE_MS)

    def tick(self, now):
        if self.message_until is not None and time.ticks_diff(now, self.message_until) >= 0:
            self.messages = []
            self.message_until = None
            self._show(*self.frame)


class Ble:
    """The Nordic UART Service: what a central writes to RX arrives here, replies go out on TX.

    The interrupt handler only queues what arrived; the main loop splits it into lines, so
    nothing it is working on changes under it.
    """

    def __init__(self):
        nus = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
        rx = (bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
              bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE)
        tx = (bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"),
              bluetooth.FLAG_NOTIFY | bluetooth.FLAG_READ)
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        self.ble.config(gap_name=BLE_NAME, mtu=BLE_MTU)
        ((self.tx, self.rx),) = self.ble.gatts_register_services(((nus, (tx, rx)),))
        self.ble.gatts_set_buffer(self.rx, 512, True)
        self.address = ":".join("%02x" % b for b in self.ble.config("mac")[1])
        self.payload = {}       # connection -> largest notification payload
        self.partial = {}       # connection -> bytes of a line not yet ended
        self.arrived = []       # (connection, bytes) queued by the interrupt handler
        name = BLE_NAME.encode()
        self._adv = b"\x02\x01\x06" + bytes((len(name) + 1, 0x09)) + name
        self._resp = bytes((17, 0x07)) + bytes(reversed(bytes.fromhex(
            "6E400001B5A3F393E0A9E50E24DCCA9E")))
        self.ble.irq(self._irq)
        self._advertise()

    def _advertise(self):
        try:
            self.ble.gap_advertise(100_000, adv_data=self._adv, resp_data=self._resp)
        except OSError:
            pass        # no room for another connection: it advertises again on a disconnect

    def _irq(self, event, data):
        if event == _CONNECT:
            self.payload[data[0]] = 20
            self._advertise()           # stay visible, so a phone can connect beside worldterm
        elif event == _DISCONNECT:
            self.payload.pop(data[0], None)
            self.partial.pop(data[0], None)
            self._advertise()
        elif event == _WRITE:
            conn, handle = data
            if handle == self.rx:
                self.arrived.append((conn, self.ble.gatts_read(self.rx)))
        elif event == _MTU:
            self.payload[data[0]] = data[1] - 3

    def lines(self):
        """The complete lines received since the last call, as (connection, bytes)."""
        out = []
        while self.arrived:
            conn, data = self.arrived.pop(0)
            data = self.partial.get(conn, b"") + data
            if b"\n" not in data and b"\r" not in data:
                self.partial[conn] = data if len(data) <= MAX_LINE else b""
                continue
            parts = data.replace(b"\r", b"\n").split(b"\n")
            self.partial[conn] = parts.pop()
            out.extend((conn, part) for part in parts)
        return out

    def send(self, conn, line):
        data = (line + "\r\n").encode()
        size = self.payload.get(conn)
        if size is None:
            return          # disconnected meanwhile
        try:
            for start in range(0, len(data), size):
                self.ble.gatts_notify(conn, self.tx, data[start:start + size])
        except OSError:
            pass            # the central left; its disconnect event follows

    def broadcast(self, line):
        for conn in list(self.payload):
            self.send(conn, line)


def handle(line, screen, reply, via):
    if line == "PING":
        reply("PONG " + PROTO)
    elif line.startswith("SHOW "):
        frame = json.loads(line[5:])
        screen.show_frame(frame.get("rows", ()), frame.get("led", "off"))
        reply("OK %d" % int(frame.get("seq", 0)))
    elif line == "CLEAR":
        screen.show_frame((), "off")
        reply("OK 0")
    else:
        screen.message(line, via)
        reply("OK 0")


def main():
    display = board.init_display()
    touch = board.init_touch()
    board.led(0, 0, 0)
    panel = Panel(display)
    boot_rows = [(0, "worldterm CYD display", "w", "b"), (4, "waiting for worldterm", "d", "k")]
    ble = None
    if bluetooth is not None:
        try:
            ble = Ble()
            boot_rows += [(6, "Bluetooth LE: " + BLE_NAME, "d", "k"), (7, "  " + ble.address, "d", "k")]
        except Exception as exc:  # the display works over USB without it
            boot_rows.append((6, "no Bluetooth: %s" % exc, "d", "k"))
    screen = Screen(panel, boot_rows)

    stdin = sys.stdin.buffer
    poller = select.poll()
    poller.register(stdin, select.POLLIN)
    buf = bytearray()
    state = {"last_rx": time.ticks_ms(), "stale": False}
    touching = 0
    lockout_until = state["last_rx"]
    send_usb("HELLO " + PROTO)

    def process(raw, reply, via):
        line = text_of(raw)
        if not line:
            return
        try:
            handle(line, screen, reply, via)
        except Exception as exc:  # a bad frame costs an ERR, not the display
            reply("ERR %s: %s" % (type(exc).__name__, exc))
        gc.collect()
        state["last_rx"] = time.ticks_ms()
        if state["stale"]:
            state["stale"] = False
            panel.clear_notice()

    while True:
        # Wait up to 50 ms for input, then take what is there one byte at a time: a read
        # of more than one byte would block until all of them arrived.
        if poller.poll(50):
            while poller.poll(0):
                c = stdin.read(1)
                if not c:
                    break
                if c != b"\n" and c != b"\r":   # either ends a line, so a terminal works too
                    buf += c
                    if len(buf) > MAX_LINE:
                        buf = bytearray()
                    continue
                raw = bytes(buf)
                buf = bytearray()
                if raw.strip():
                    process(raw, send_usb, "USB")
                    break   # answer before reading on; the host sends one command at a time

        if ble is not None:
            for conn, raw in ble.lines():
                process(raw, lambda reply, conn=conn: ble.send(conn, reply), "BLE")

        now = time.ticks_ms()
        screen.tick(now)
        raw_touch = touch.raw_touch()
        if raw_touch:
            touching += 1
            if touching == TOUCH_CONFIRM and time.ticks_diff(now, lockout_until) >= 0:
                x, y = touch.normalize(*raw_touch)
                send_usb("TOUCH %d %d" % (x, y))
                if ble is not None:
                    ble.broadcast("TOUCH %d %d" % (x, y))
                lockout_until = time.ticks_add(now, TOUCH_LOCKOUT_MS)
        else:
            touching = 0

        if not state["stale"] and time.ticks_diff(now, state["last_rx"]) > STALE_MS:
            state["stale"] = True
            panel.notice("no update from worldterm")
            board.led(0, 0, 0)


main()
