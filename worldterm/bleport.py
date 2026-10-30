"""A Bluetooth LE link to the CYD display, with the standard library.

The board (cyd/main.py) offers the Nordic UART Service (NUS), the usual "serial port over
BLE": the host writes bytes to its RX characteristic and the board answers with
notifications on TX. This module is the host side. It speaks ATT, the protocol under GATT,
over Linux's L2CAP socket for the ATT channel, so it needs neither BlueZ's D-Bus API nor a
library: connect, raise the MTU, find the two characteristics, subscribe, then write and
read. `BlePort` has the read/write/close of `serialport.SerialPort`, so `CydService` drives
either one, and the board speaks the same line protocol on both.

Linux only, and only by address: finding a device by name needs a scan, which needs root or
BlueZ. The board shows its address on its screen while it waits for worldterm.

Run as a program it sends text to the display: `python3 -m worldterm.bleport ADDRESS hello`.
"""
from __future__ import annotations

import errno
import os
import re
import select
import socket
import struct
import sys
import time
from typing import Callable

NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"     # the host writes here
NUS_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"     # the board notifies here

ATT_CID = 4               # the fixed L2CAP channel that carries ATT on an LE link
DEFAULT_MTU = 23          # every ATT link starts at this
WANT_MTU = 247            # the most the board's stack takes; a 1.3 KB frame then needs 6 writes
CONNECT_TIMEOUT = 5.0     # an advertising board answers in well under a second
ATT_TIMEOUT = 3.0         # a request unanswered this long means the link is gone (the spec allows 30)
POLL = 0.2                # the longest any wait blocks between checks for cancellation
CCCD = 0x2902             # the descriptor a client writes to subscribe to notifications

# ATT opcodes (Bluetooth Core, Vol 3, Part F)
ERROR_RSP = 0x01
MTU_REQ, MTU_RSP = 0x02, 0x03
FIND_INFO_REQ, FIND_INFO_RSP = 0x04, 0x05
READ_BY_TYPE_REQ, READ_BY_TYPE_RSP = 0x08, 0x09
WRITE_REQ, WRITE_RSP = 0x12, 0x13
NOTIFY, INDICATE, CONFIRM = 0x1B, 0x1D, 0x1E
# Requests a server may send its client. This side has no attributes to offer, so it answers
# them with an error (or, for the MTU, its own), which keeps the link from timing out.
SERVER_REQUESTS = frozenset({0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x12, 0x16, 0x18, 0x20})
RESPONSES = frozenset({ERROR_RSP, MTU_RSP, FIND_INFO_RSP, READ_BY_TYPE_RSP, WRITE_RSP})
ATTRIBUTE_NOT_FOUND = 0x0A
REQUEST_NOT_SUPPORTED = 0x06

_ADDRESS = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class LinkError(OSError):
    """A failure the user can fix; `hint` says how, in a sentence fit for a notification."""

    def __init__(self, err: int, message: str, hint: str) -> None:
        super().__init__(err, message)
        self.hint = hint


def busy_hint(address: str) -> str:
    return (f"The CYD display {address} is busy: another program on this computer is connected to "
            "it over Bluetooth, usually the desktop's own after pairing it in Settings. Disconnect it "
            f"there, or run: bluetoothctl disconnect {address}")


class AttError(OSError):
    """The board answered a request with an ATT error."""

    def __init__(self, opcode: int, handle: int, code: int) -> None:
        super().__init__(errno.EPROTO, f"ATT error 0x{code:02x} on request 0x{opcode:02x}, handle {handle}")
        self.code = code


def parse_address(text: str) -> str:
    """A Bluetooth address such as a4:f0:0f:5b:8b:2a, upper-cased; ValueError otherwise."""
    text = text.strip()
    if not _ADDRESS.match(text):
        raise ValueError(f"expected a Bluetooth address such as A4:F0:0F:5B:8B:2A, not {text!r}")
    return text.upper()


def unavailable_reason() -> str | None:
    """Why this host cannot reach a BLE display, or None."""
    if not all(hasattr(socket, name) for name in ("AF_BLUETOOTH", "BTPROTO_L2CAP", "BDADDR_LE_PUBLIC")):
        return "Bluetooth LE needs Linux and Python 3.14 or newer"
    return None


def _uuid_bytes(uuid: str) -> bytes:
    """A 128-bit UUID as ATT carries it: little-endian."""
    return bytes(reversed(bytes.fromhex(uuid.replace("-", ""))))


def _connect(address: str, timeout: float, cancelled: Callable[[], bool]) -> socket.socket:
    """An L2CAP socket on the ATT channel, connected to *address* (an LE public address).

    The connect is non-blocking and checked every POLL seconds, so a board that is off or out
    of range never holds up a caller that wants to stop.
    """
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    try:
        sock.bind((socket.BDADDR_ANY, 0, ATT_CID, socket.BDADDR_LE_PUBLIC))
        sock.setblocking(False)
        err = sock.connect_ex((address, 0, ATT_CID, socket.BDADDR_LE_PUBLIC))
        deadline = time.monotonic() + timeout
        while err in (errno.EINPROGRESS, errno.EAGAIN, errno.EALREADY):
            if cancelled():
                raise OSError(errno.ECANCELED, "cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError(errno.ETIMEDOUT, f"no answer from {address}")
            _, writable, _ = select.select([], [sock], [], min(POLL, remaining))
            if writable:
                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                break
        if err == errno.EBUSY:
            # The kernel allows one ATT channel per link, and someone else has it.
            raise LinkError(err, os.strerror(err), busy_hint(address))
        if err:
            raise OSError(err, os.strerror(err))
        return sock
    except BaseException:
        sock.close()
        raise


class BlePort:
    """The display's Nordic UART Service, as a byte stream.

    *sock* is an already connected socket on the ATT channel (the tests pass one end of a
    socket pair); without it the port connects to *address* itself.
    """

    def __init__(self, address: str, *, timeout: float = CONNECT_TIMEOUT,
                 cancelled: Callable[[], bool] = lambda: False, sock: socket.socket | None = None,
                 att_timeout: float = ATT_TIMEOUT) -> None:
        self.address = address
        self.mtu = DEFAULT_MTU
        self._att_timeout = att_timeout
        self._incoming = bytearray()      # notification payloads not yet read
        self._tx_handle = -1
        self._sock = sock if sock is not None else _connect(address, timeout, cancelled)
        self._sock.setblocking(False)
        try:
            self._setup()
        except BaseException:
            self._sock.close()
            raise

    # ---------------------------------------------------------------- the serial-port interface
    def read(self, max_bytes: int = 4096, timeout: float = POLL) -> bytes:
        """What the board has sent, waiting up to *timeout* for something to arrive."""
        deadline = time.monotonic() + timeout
        while not self._incoming:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._pump(remaining)     # a stray response with nothing outstanding is dropped
        data = bytes(self._incoming[:max_bytes])
        del self._incoming[:max_bytes]
        return data

    def write(self, data: bytes, timeout: float = 1.0) -> None:
        """Send *data* in MTU-sized writes, each acknowledged before the next (flow control)."""
        size = self.mtu - 3
        for start in range(0, len(data), size):
            self._request(struct.pack("<BH", WRITE_REQ, self._rx_handle) + data[start:start + size],
                          WRITE_RSP, max(timeout, self._att_timeout))

    def close(self) -> None:
        self._sock.close()

    # ---------------------------------------------------------------- setup
    def _setup(self) -> None:
        rsp = self._request(struct.pack("<BH", MTU_REQ, WANT_MTU), MTU_RSP)
        self.mtu = max(DEFAULT_MTU, min(WANT_MTU, struct.unpack_from("<H", rsp, 1)[0]))
        chars = self._characteristics()
        rx, tx = chars.get(_uuid_bytes(NUS_RX)), chars.get(_uuid_bytes(NUS_TX))
        if rx is None or tx is None:
            raise LinkError(errno.ENOENT, f"{self.address} offers no Nordic UART Service",
                            f"The Bluetooth device {self.address} offers no Nordic UART Service, so it "
                            "is not a worldterm display. Is worldterm's cyd/main.py deployed to the board?")
        self._rx_handle = rx[0]
        self._tx_handle, tx_end = tx
        cccd = self._find_descriptor(self._tx_handle + 1, tx_end, CCCD)
        if cccd is None:
            raise OSError(errno.ENOENT, f"{self.address}: the UART's TX characteristic cannot notify")
        self._request(struct.pack("<BHH", WRITE_REQ, cccd, 0x0001), WRITE_RSP)

    def _characteristics(self) -> dict[bytes, tuple[int, int]]:
        """Every characteristic: UUID bytes -> (value handle, last handle that belongs to it)."""
        found: list[tuple[int, int, bytes]] = []     # declaration handle, value handle, uuid
        start = 1
        while start <= 0xFFFF:
            try:
                rsp = self._request(struct.pack("<BHHH", READ_BY_TYPE_REQ, start, 0xFFFF, 0x2803),
                                    READ_BY_TYPE_RSP)
            except AttError as exc:
                if exc.code == ATTRIBUTE_NOT_FOUND:
                    break
                raise
            size = rsp[1]
            if size < 7:
                raise OSError(errno.EPROTO, "malformed characteristic list")
            for i in range(2, len(rsp) - size + 1, size):
                decl, _props, value = struct.unpack_from("<HBH", rsp, i)
                found.append((decl, value, bytes(rsp[i + 5:i + size])))
                start = decl + 1
        found.sort()
        chars = {}
        for n, (_decl, value, uuid) in enumerate(found):
            end = found[n + 1][0] - 1 if n + 1 < len(found) else 0xFFFF
            chars[uuid] = (value, end)
        return chars

    def _find_descriptor(self, start: int, end: int, uuid16: int) -> int | None:
        while start <= end:
            try:
                rsp = self._request(struct.pack("<BHH", FIND_INFO_REQ, start, end), FIND_INFO_RSP)
            except AttError as exc:
                if exc.code == ATTRIBUTE_NOT_FOUND:
                    return None
                raise
            size = 4 if rsp[1] == 1 else 18
            last = start
            for i in range(2, len(rsp) - size + 1, size):
                handle = struct.unpack_from("<H", rsp, i)[0]
                if size == 4 and struct.unpack_from("<H", rsp, i + 2)[0] == uuid16:
                    return handle
                last = handle
            start = last + 1
        return None

    # ---------------------------------------------------------------- ATT
    def _request(self, pdu: bytes, want: int, wait: float | None = None) -> bytes:
        """Send a request and return its response; notifications that arrive meanwhile are kept."""
        self._send(pdu)
        deadline = time.monotonic() + (self._att_timeout if wait is None else wait)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError(errno.ETIMEDOUT, f"{self.address} did not answer")
            rsp = self._pump(remaining)
            if rsp is None:
                continue
            if rsp[0] == ERROR_RSP and len(rsp) >= 5 and rsp[1] == pdu[0]:
                _, opcode, handle, code = struct.unpack_from("<BBHB", rsp)
                raise AttError(opcode, handle, code)
            if rsp[0] == want:
                return rsp
            # a response to something else (late, after a timeout) is dropped

    def _pump(self, timeout: float) -> bytes | None:
        """Take one PDU off the link: keep a notification, answer a request, return a response."""
        readable, _, _ = select.select([self._sock], [], [], min(timeout, POLL))
        if not readable:
            return None
        try:
            pdu = self._sock.recv(1024)
        except BlockingIOError:
            return None
        if not pdu:
            raise OSError(errno.ECONNRESET, "the display disconnected")
        op = pdu[0]
        if op in (NOTIFY, INDICATE) and len(pdu) >= 3:
            if struct.unpack_from("<H", pdu, 1)[0] == self._tx_handle:
                self._incoming += pdu[3:]
            if op == INDICATE:
                self._send(bytes((CONFIRM,)))
            return None
        if op in SERVER_REQUESTS:
            if op == MTU_REQ:
                self._send(struct.pack("<BH", MTU_RSP, WANT_MTU))
            else:
                self._send(struct.pack("<BBHB", ERROR_RSP, op, 0, REQUEST_NOT_SUPPORTED))
            return None
        if op in RESPONSES:
            return pdu
        return None     # a command or anything unknown needs no answer

    def _send(self, pdu: bytes) -> None:
        try:
            self._sock.send(pdu)
        except BlockingIOError:
            _, writable, _ = select.select([], [self._sock], [], self._att_timeout)
            if not writable:
                raise OSError(errno.ETIMEDOUT, f"{self.address}: the link is not taking data") from None
            self._sock.send(pdu)


# ---------------------------------------------------------------------------- the send-text program
def send_lines(port: BlePort, lines, wait: float = 3.0) -> list[str]:
    """Show each line on the display as a message; returns the problems the board reported."""
    problems = []
    for line in lines:
        text = " ".join(line.split())
        if not text:
            continue
        port.write(b"\n" + text.encode("utf-8") + b"\n")
        buf = bytearray()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            buf += port.read(4096, min(POLL, max(0.0, deadline - time.monotonic())))
            replies = [r.strip() for r in bytes(buf).decode("ascii", "replace").split("\n")]
            if "OK 0" in replies:
                break
            errors = [r for r in replies if r.startswith("ERR")]
            if errors:
                problems.append(f"{text!r}: {errors[0]}")
                break
        else:
            problems.append(f"{text!r}: no answer from the display")
    return problems


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python3 -m worldterm.bleport",
        description="Send text to the worldterm CYD display over Bluetooth LE. Each line is shown "
                    "on the screen for 20 seconds; with no TEXT, lines are read from standard input.")
    parser.add_argument("address", metavar="ADDRESS", help="the display's Bluetooth address, shown on its screen")
    parser.add_argument("text", metavar="TEXT", nargs="*", help="the message (words are joined by spaces)")
    args = parser.parse_args(argv)
    reason = unavailable_reason()
    if reason:
        parser.error(reason)
    try:
        address = parse_address(args.address)
    except ValueError as exc:
        parser.error(str(exc))
    lines = [" ".join(args.text)] if args.text else sys.stdin
    try:
        port = BlePort(address)
    except OSError as exc:
        print(f"cannot connect to {address}: {exc.strerror or exc}", file=sys.stderr)
        if exc.errno == errno.EBUSY:
            print("Another program on this computer holds the display's Bluetooth link: worldterm "
                  "(started with --cyd ble:...), or the desktop's Bluetooth after pairing the board in "
                  f"Settings (bluetoothctl disconnect {address}).", file=sys.stderr)
        elif getattr(exc, "hint", None):
            print(exc.hint, file=sys.stderr)
        return 1
    try:
        problems = send_lines(port, lines)
    except OSError as exc:
        print(f"lost the display: {exc.strerror or exc}", file=sys.stderr)
        return 1
    finally:
        port.close()
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
