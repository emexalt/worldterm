# worldterm on the Cheap Yellow Display

`main.py` in this directory runs on the CYD (an ESP32-2432S028 with a 320x240 LCD, running
MicroPython) and turns it into a display for worldterm: today's unacknowledged **Critical**
headlines, one at a time, eight seconds each, with the board's RGB LED red while one is on
screen. worldterm finds the board by itself when it is plugged in, and carries on without
it when it is not. Nothing is acknowledged from the board. Taps are reported, and ignored.

## Deploy

The board needs MicroPython plus `board.py`, `ili9341.py` and `xpt2046.py` from the
[cyd-micropython](https://github.com/emexalt/cyd-micropython) repository, which also covers
flashing MicroPython and calibrating the touchscreen. Its `make deploy` puts those files on
the board along with a touch-drawing `main.py`, which this one replaces (so running
`make deploy` again brings the demo back until you redeploy this one). With worldterm not running, or running
with `--cyd off`:

```sh
mpremote connect /dev/ttyUSB0 cp cyd/main.py :main.py
mpremote connect /dev/ttyUSB0 reset
```

The screen shows *worldterm CYD display / waiting for worldterm*. Then:

```sh
python3 main.py                      # --cyd auto is the default: the board is used whenever it is plugged in
python3 main.py --cyd /dev/ttyUSB1   # this port only (it need not exist yet)
python3 main.py --cyd off            # never open a serial port
```

Within a few seconds a toast says *CYD display connected on /dev/ttyUSB0* and the `CYD`
chip in worldterm's header lights up. Unplug the board and the chip dims; plug it back in
and the display resumes.

## What it shows

```
 CRITICAL 2/4            Fri 24 Oct 21:30     <- white on red; the clock is the PC's
 BBC News                          20:58      <- source and the headline's time
 Life-threatening Hurricane Isaias makes
 landfall in Florida
 ...
 4 unacknowledged critical today
```

- A headline is shown when it is Critical, not acknowledged, not cleared, and dated
  today in the PC's timezone. Weather alerts count too (NWS "Extreme"), with the issuing
  office as the source.
- Acknowledge it in worldterm and it leaves the display within a second. Acknowledge it
  again (undo) and it comes back.
- With nothing to show: *No unacknowledged critical headlines today*, LED off.
- When worldterm quits: *worldterm stopped HH:MM*. If worldterm dies without saying so,
  the board writes *no update from worldterm* on its bottom row after 30 seconds.
- Accents are folded to ASCII (the board's font has nothing else); a headline in another
  script becomes *(headline not displayable in ASCII)*, with its source and time.

## Sharing the port with mpremote

worldterm and mpremote take the same exclusive lock on the port, so whichever opens it
second fails cleanly. Quit worldterm, or run it with `--cyd off`, before `mpremote cp`,
`run` or `repl`. After an mpremote session the board is usually left at its REPL prompt;
worldterm notices (the REPL complains about its `PING`) and soft-resets the board so this
program runs again. Pressing the board's reset button does the same.

## Permissions

`/dev/ttyUSB0` belongs to group `dialout`. Either `sudo usermod -aG dialout $USER` (then
log out and in), or rely on the udev rule that grants the port to whoever is logged in
at the desktop. Without access, worldterm keeps trying every two seconds and says nothing.

## Protocol

Newline-terminated ASCII at 115200 baud, 8N1. One command is outstanding at a time: the
host sends the next only after the reply, or after two seconds. Three unanswered commands
in a row and the host drops the connection and looks for the board again.

| host → board | board → host | meaning |
|---|---|---|
| | `HELLO worldterm-cyd 1` | the program started; `1` is the protocol version |
| `PING` | `PONG worldterm-cyd 1` | keepalive, every 5 s while nothing changes |
| `SHOW {"seq":n,"rows":[[row,text,fg,bg],...],"led":"r"}` | `OK n` | draw a whole frame |
| `CLEAR` | `OK 0` | blank the screen |
| | `ERR text` | a line the board could not use |
| | `TOUCH x y` | a tap, in screen pixels (ignored by worldterm) |

A frame lists rows 0..22 (row 23 is the board's notice line); text is at most 40 printable
ASCII characters; `fg` and `bg` are palette letters: `w` white, `r` red, `y` yellow, `g`
green, `b` blue, `d` dim grey, `k` black. Rows not listed are blank. The board redraws only
rows that changed. `led` is `r`, `g`, `b` or `off`.

The host writes in 64-byte chunks with a 20 ms pause (`WRITE_CHUNK`, `WRITE_GAP` in
`worldterm/cyd.py`) because the board reads a byte at a time from a buffer of about 256
bytes. Measured on 2026-10-24 with this board: a 1.3 KB frame sent at full wire speed is
lost (the board never sees its newline), 64-byte chunks every 10 ms arrive intact, so the
20 ms gap leaves a two-fold margin. A full frame then takes 0.4 s to send and 0.33 s to
draw (14 ms a row); a frame that changes one row draws in 0.05 s, and an unchanged one is
acknowledged in 0.03 s. If frames arrive torn (the board answers `ERR`), raise the gap.
Opening and closing the port from worldterm does not reset the board.

When the frame format changes, bump `PROTOCOL_VERSION` in `worldterm/cyd.py` and `PROTO`
here together. A board that answers with another version is refused, with a toast that
says to redeploy.

## Hand test

With worldterm not running:

```sh
python3 -m serial.tools.miniterm /dev/ttyUSB0 115200     # or: screen /dev/ttyUSB0 115200
```

Type `PING` and Enter: the board answers `PONG worldterm-cyd 1`. Paste a frame:

```
SHOW {"seq":1,"rows":[[0,"hello from the terminal","w","b"]],"led":"g"}
```

The row appears, the LED turns green and the board answers `OK 1`. If a `HELLO` line
appeared when the terminal opened the port, opening it reset the board; worldterm copes
with that either way (it waits for the greeting before it starts asking).
