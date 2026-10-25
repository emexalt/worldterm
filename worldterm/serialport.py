"""USB serial ports with the standard library, for the CYD display (Linux).

`find_ports` lists the ports whose USB vendor and product ids look like an ESP32 board's
serial bridge, read from sysfs. `SerialPort` is a raw 8N1 port with bounded reads, the
termios settings pyserial uses, and a close order that does not reset an ESP32.
"""
from __future__ import annotations

import errno
import fcntl
import os
import select
import struct
import termios
import time
from pathlib import Path

# USB vendor:product ids of the serial bridges found on ESP32 boards: CH340, CH9102, CP210x.
USB_SERIAL_IDS: frozenset[tuple[int, int]] = frozenset({(0x1A86, 0x7523), (0x1A86, 0x55D4), (0x10C4, 0xEA60)})
SYS_TTY = Path("/sys/class/tty")
PORT_PREFIXES = ("ttyUSB", "ttyACM")

_MODEM_IGNORED = (errno.ENOTTY, errno.EINVAL)  # pseudo-terminals have no modem lines


def _flags(*names: str) -> int:
    return sum(getattr(termios, name, 0) for name in names)


# ---------------------------------------------------------------------------- discovery
def usb_id(tty_dir: Path) -> tuple[int, int] | None:
    """The (vendor, product) ids of the USB device behind a /sys/class/tty entry, or None.

    The entry's `device` link points at the port; the USB device with the ids is one
    parent up for a CDC port (ttyACM) and two for a usb-serial port (ttyUSB).
    """
    try:
        dev = (tty_dir / "device").resolve(strict=True)
    except OSError:
        return None
    for _ in range(4):
        vendor = dev / "idVendor"
        if vendor.is_file():
            try:
                return int(vendor.read_text(), 16), int((dev / "idProduct").read_text(), 16)
            except (OSError, ValueError):
                return None
        dev = dev.parent
    return None


def find_ports(ids: frozenset[tuple[int, int]] = USB_SERIAL_IDS, sys_tty: Path = SYS_TTY,
               dev_dir: str = "/dev") -> list[str]:
    """Device paths of the USB serial ports whose ids are in *ids*, sorted. Empty without sysfs."""
    try:
        entries = sorted(p for p in sys_tty.iterdir() if p.name.startswith(PORT_PREFIXES))
    except OSError:
        return []
    return [f"{dev_dir}/{p.name}" for p in entries if usb_id(p) in ids]


# ---------------------------------------------------------------------------- port
class SerialPort:
    """A raw 8N1 serial port. `read` waits at most its timeout; an unplugged device raises OSError."""

    def __init__(self, path: str, baud: int = 115200) -> None:
        self.path = path
        self.fd: int | None = None
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # The advisory lock pyserial takes for exclusive=True, so mpremote and worldterm
            # refuse each other's port instead of corrupting the stream.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self._configure(fd, baud)
                # Keep DTR and RTS asserted together: on an ESP32 only DTR low with RTS high
                # resets the chip, and the kernel raised both when it opened the port.
                _modem(fd, termios.TIOCMBIS, termios.TIOCM_DTR | termios.TIOCM_RTS)
                termios.tcflush(fd, termios.TCIFLUSH)
            except termios.error as exc:
                raise OSError(errno.EIO, f"{path}: not a serial port ({exc})") from exc
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    @staticmethod
    def _configure(fd: int, baud: int) -> None:
        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(fd)
        iflag &= ~_flags("IGNBRK", "BRKINT", "PARMRK", "ISTRIP", "INLCR", "IGNCR", "ICRNL",
                         "IXON", "IXOFF", "IXANY", "INPCK", "IUCLC")
        oflag &= ~_flags("OPOST", "ONLCR", "OCRNL")
        cflag &= ~_flags("CSIZE", "PARENB", "PARODD", "CSTOPB", "CRTSCTS")
        cflag |= _flags("CS8", "CLOCAL", "CREAD")
        lflag &= ~_flags("ICANON", "ECHO", "ECHOE", "ECHOK", "ECHONL", "ECHOCTL", "ECHOKE", "ISIG", "IEXTEN")
        cc = list(cc)
        cc[termios.VMIN] = 0   # reads never block; timeouts are done with select
        cc[termios.VTIME] = 0
        speed = getattr(termios, f"B{baud}")
        termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])

    def read(self, max_bytes: int = 4096, timeout: float = 0.2) -> bytes:
        """Up to *max_bytes* bytes, or b"" when nothing arrived within *timeout* seconds."""
        fd = self._open_fd()
        ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        try:
            data = os.read(fd, max_bytes)
        except BlockingIOError:
            return b""
        if not data:
            # Linux reports a vanished USB device as readable, then returns nothing.
            raise OSError(errno.EIO, f"{self.path}: device disconnected")
        return data

    def write(self, data: bytes, timeout: float = 1.0) -> None:
        """Write all of *data*, raising OSError if the port does not take it within *timeout*."""
        fd = self._open_fd()
        deadline = time.monotonic() + timeout
        view = memoryview(data)
        while view:
            _, ready, _ = select.select([], [fd], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                raise OSError(errno.ETIMEDOUT, f"{self.path}: write timed out")
            try:
                n = os.write(fd, view)
            except BlockingIOError:
                continue
            view = view[n:]

    def close(self) -> None:
        """Drop RTS, then DTR, then close: the order that leaves an ESP32 running (mpremote does the same)."""
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            for line in (termios.TIOCM_RTS, termios.TIOCM_DTR):
                _modem(fd, termios.TIOCMBIC, line, strict=False)
        finally:
            os.close(fd)

    def _open_fd(self) -> int:
        if self.fd is None:
            raise OSError(errno.EBADF, f"{self.path}: port is closed")
        return self.fd


def _modem(fd: int, op: int, bits: int, strict: bool = True) -> None:
    """Set (TIOCMBIS) or clear (TIOCMBIC) modem control lines. Pseudo-terminals have none."""
    try:
        fcntl.ioctl(fd, op, struct.pack("I", bits))
    except OSError as exc:
        if strict and exc.errno not in _MODEM_IGNORED:
            raise
