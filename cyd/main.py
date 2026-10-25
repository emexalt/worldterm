"""worldterm's display program for the Cheap Yellow Display. Deploy it to the board as main.py.

It runs at boot and turns the board into a dumb text display: worldterm sends it frames
over the USB serial port, it draws them and answers. It needs board.py, ili9341.py and
xpt2046.py on the board, deployed from the cyd-micropython repository. README.md next
to this file has the deploy steps and the protocol.

    host -> board                                        board -> host
    PING                                                 PONG worldterm-cyd 1
    SHOW {"seq":n,"rows":[[row,text,fg,bg],...],"led":x}  OK n          (drawn)
    CLEAR                                                OK 0
                                                         HELLO worldterm-cyd 1   once, at boot
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

PROTO = "worldterm-cyd 1"
COLS, ROWS, PITCH = 40, 24, 10          # 8x8 font on a 10 px row pitch: 40 x 24 cells on 320 x 240
NOTICE_ROW = ROWS - 1                   # the host never writes this row
STALE_MS = 30_000                       # this long without a line from the host, say so
TOUCH_CONFIRM = 2                       # consecutive readings that make a tap
TOUCH_LOCKOUT_MS = 500
MAX_LINE = 4096

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
BOOT_FRAME = ((0, "worldterm CYD display", "w", "b"), (4, "waiting for worldterm", "d", "k"))


def pad(text):
    """*text* cut or space-filled to the width of the screen (MicroPython has no ljust)."""
    text = text[:COLS]
    return text + " " * (COLS - len(text))


def send(line):
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


def handle(line, panel):
    if line == "PING":
        send("PONG " + PROTO)
    elif line.startswith("SHOW "):
        frame = json.loads(line[5:])
        panel.show(frame.get("rows", ()))
        board.led(*LEDS.get(frame.get("led", "off"), LEDS["off"]))
        send("OK %d" % int(frame.get("seq", 0)))
    elif line == "CLEAR":
        panel.show(())
        board.led(0, 0, 0)
        send("OK 0")
    elif line:
        send("ERR unknown command")


def main():
    display = board.init_display()
    touch = board.init_touch()
    board.led(0, 0, 0)
    panel = Panel(display)
    panel.show(BOOT_FRAME)

    stdin = sys.stdin.buffer
    poller = select.poll()
    poller.register(stdin, select.POLLIN)
    buf = bytearray()
    last_rx = time.ticks_ms()
    stale = False
    touching = 0
    lockout_until = last_rx
    send("HELLO " + PROTO)

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
                line = bytes(buf).decode().strip()
                buf = bytearray()
                if not line:
                    continue
                try:
                    handle(line, panel)
                except Exception as exc:  # a bad frame costs an ERR, not the display
                    send("ERR %s: %s" % (type(exc).__name__, exc))
                gc.collect()
                last_rx = time.ticks_ms()
                if stale:
                    stale = False
                    panel.clear_notice()
                break   # answer before reading on; the host sends one command at a time

        now = time.ticks_ms()
        raw = touch.raw_touch()
        if raw:
            touching += 1
            if touching == TOUCH_CONFIRM and time.ticks_diff(now, lockout_until) >= 0:
                x, y = touch.normalize(*raw)
                send("TOUCH %d %d" % (x, y))
                lockout_until = time.ticks_add(now, TOUCH_LOCKOUT_MS)
        else:
            touching = 0

        if not stale and time.ticks_diff(now, last_rx) > STALE_MS:
            stale = True
            panel.notice("no update from worldterm")
            board.led(0, 0, 0)


main()
