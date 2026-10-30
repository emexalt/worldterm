"""The BLE link to the CYD: ATT over a socket pair standing in for the L2CAP channel. No radio."""
import errno
import json
import socket
import struct
import threading
import unittest

from worldterm import bleport, cyd
from worldterm.bleport import BlePort, parse_address, send_lines

RX_UUID = bleport._uuid_bytes(bleport.NUS_RX)
TX_UUID = bleport._uuid_bytes(bleport.NUS_TX)
OTHER_UUID = bleport._uuid_bytes("0000FEED-0000-1000-8000-00805F9B34FB")


class FakeBoard(threading.Thread):
    """An ATT server with the board's attribute table, on one end of a SEQPACKET socket pair.

    Handles: 0x10 service, 0x11 TX declaration, 0x12 TX value, 0x13 its CCCD,
    0x14 RX declaration, 0x15 RX value; *uart=False* leaves out both characteristics.
    `answer` maps a received line to the reply the board notifies.
    """

    def __init__(self, sock, *, mtu=100, uart=True, answer=None, mute_writes=False):
        super().__init__(daemon=True)
        self.sock, self.mtu, self.answer = sock, mtu, answer
        self.mute_writes = mute_writes
        self.chars = [(0x11, 0x12, TX_UUID), (0x14, 0x15, RX_UUID)] if uart else [(0x11, 0x12, OTHER_UUID)]
        self.requests = []          # opcodes received, in order
        self.writes = []            # payloads written to RX, one per ATT write
        self.subscribed = False
        self.confirmed = 0
        self.client_answers = []    # what the client said to our own requests
        self._line = b""

    def run(self):
        try:
            while True:
                pdu = self.sock.recv(1024)
                if not pdu:
                    return
                self.requests.append(pdu[0])
                self._handle(pdu)
        except OSError:
            return
        finally:
            self.sock.close()

    def send(self, pdu):
        self.sock.send(pdu)

    def notify(self, data, op=bleport.NOTIFY):
        self.send(struct.pack("<BH", op, 0x12) + data)

    def _handle(self, pdu):
        op = pdu[0]
        if op == bleport.MTU_REQ:
            self.send(struct.pack("<BH", bleport.MTU_RSP, self.mtu))
        elif op == bleport.READ_BY_TYPE_REQ:
            start = struct.unpack_from("<H", pdu, 1)[0]
            entries = [c for c in self.chars if c[0] >= start][:1]   # one per response, to test paging
            if not entries:
                self.send(struct.pack("<BBHB", bleport.ERROR_RSP, op, start, bleport.ATTRIBUTE_NOT_FOUND))
                return
            body = b"".join(struct.pack("<HBH", d, 0x12, v) + u for d, v, u in entries)
            self.send(bytes((bleport.READ_BY_TYPE_RSP, 21)) + body)
        elif op == bleport.FIND_INFO_REQ:
            start, end = struct.unpack_from("<HH", pdu, 1)
            if start <= 0x13 <= end and self.chars[0][2] == TX_UUID:
                self.send(struct.pack("<BBHH", bleport.FIND_INFO_RSP, 1, 0x13, bleport.CCCD))
            else:
                self.send(struct.pack("<BBHB", bleport.ERROR_RSP, op, start, bleport.ATTRIBUTE_NOT_FOUND))
        elif op == bleport.WRITE_REQ:
            handle = struct.unpack_from("<H", pdu, 1)[0]
            value = pdu[3:]
            if handle == 0x13:
                self.subscribed = value == b"\x01\x00"
            elif handle == 0x15:
                self.writes.append(value)
                if self.mute_writes:
                    return
                self._line += value
            self.send(bytes((bleport.WRITE_RSP,)))
            while b"\n" in self._line:
                line, self._line = self._line.split(b"\n", 1)
                reply = self.answer(line) if self.answer and line else None
                if reply:
                    size = self.mtu - 3
                    for i in range(0, len(reply), size):
                        self.notify(reply[i:i + size])
        elif op == bleport.CONFIRM:
            self.confirmed += 1
        elif op in (bleport.MTU_RSP, bleport.ERROR_RSP):
            self.client_answers.append(pdu)


def board_answers(line):
    """Replies like cyd/main.py's."""
    if line == b"PING":
        return b"PONG worldterm-cyd 1\r\n"
    if line.startswith(b"SHOW "):
        return b"OK %d\r\n" % json.loads(line[5:])["seq"]
    if line == b"CLEAR":
        return b"OK 0\r\n"
    return b"OK 0\r\n"      # any other line is a message


def linked(**kwargs):
    """A BlePort connected to a running FakeBoard made with *kwargs*."""
    host, device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    fake = FakeBoard(device, **kwargs)
    fake.start()
    return BlePort("A4:F0:0F:5B:8B:2A", sock=host, att_timeout=1.0), fake


class AddressTests(unittest.TestCase):
    def test_parse_address(self):
        self.assertEqual(parse_address(" a4:f0:0f:5b:8b:2a "), "A4:F0:0F:5B:8B:2A")
        for bad in ("", "A4:F0:0F:5B:8B", "A4-F0-0F-5B-8B-2A", "G4:F0:0F:5B:8B:2A", "/dev/ttyUSB0"):
            with self.assertRaises(ValueError):
                parse_address(bad)

    def test_this_python_can_reach_ble(self):
        self.assertIsNone(bleport.unavailable_reason())


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.port, self.fake = linked(answer=board_answers)

    def tearDown(self):
        self.port.close()

    def test_setup_negotiates_discovers_and_subscribes(self):
        self.assertEqual(self.port.mtu, 100)
        self.assertTrue(self.fake.subscribed)
        self.assertEqual(self.port._rx_handle, 0x15)
        self.assertEqual(self.port._tx_handle, 0x12)
        self.assertEqual(self.fake.requests[0], bleport.MTU_REQ)

    def test_writes_are_cut_to_the_mtu_and_each_is_acknowledged(self):
        data = bytes(range(256)) * 2
        self.port.write(data)
        self.assertEqual(b"".join(self.fake.writes), data)
        self.assertTrue(all(len(w) <= 97 for w in self.fake.writes))
        self.assertEqual(len(self.fake.writes), 6)

    def test_reads_return_notifications_and_time_out_empty(self):
        self.assertEqual(self.port.read(timeout=0.05), b"")
        self.port.write(b"\nPING\n")
        got = b""
        for _ in range(20):
            got += self.port.read(timeout=0.05)
            if got.endswith(b"\r\n"):
                break
        self.assertEqual(got, b"PONG worldterm-cyd 1\r\n")

    def test_indications_are_read_and_confirmed(self):
        self.fake.notify(b"HELLO worldterm-cyd 1\r\n", op=bleport.INDICATE)
        self.assertEqual(self.port.read(timeout=1.0), b"HELLO worldterm-cyd 1\r\n")
        self.port.read(timeout=0.1)
        self.assertEqual(self.fake.confirmed, 1)

    def test_requests_from_the_board_are_answered(self):
        self.fake.send(struct.pack("<BH", bleport.MTU_REQ, 185))
        self.fake.send(struct.pack("<BHHH", bleport.READ_BY_TYPE_REQ, 1, 0xFFFF, 0x2800))
        self.port.read(timeout=0.2)
        self.port.write(b"x\n")           # and the link still works
        self.assertEqual(self.fake.writes[-1], b"x\n")
        self.assertEqual(self.fake.client_answers[0][0], bleport.MTU_RSP)
        self.assertEqual(self.fake.client_answers[1],
                         struct.pack("<BBHB", bleport.ERROR_RSP, bleport.READ_BY_TYPE_REQ, 0,
                                     bleport.REQUEST_NOT_SUPPORTED))

    def test_the_board_going_away_is_an_oserror(self):
        self.fake.sock.shutdown(socket.SHUT_RDWR)   # close() alone would not wake its reader
        with self.assertRaises(OSError):
            for _ in range(20):
                self.port.read(timeout=0.05)
        with self.assertRaises(OSError):
            self.port.write(b"PING\n")


class SetupFailureTests(unittest.TestCase):
    def test_a_device_without_the_uart_is_refused(self):
        host, device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        FakeBoard(device, uart=False).start()
        with self.assertRaisesRegex(OSError, "Nordic UART") as caught:
            BlePort("A4:F0:0F:5B:8B:2A", sock=host, att_timeout=1.0)
        self.assertIn("cyd/main.py", caught.exception.hint)
        self.assertEqual(host.fileno(), -1, "the socket is closed on failure")

    def test_an_unanswered_write_times_out(self):
        port, fake = linked(answer=board_answers, mute_writes=True)
        try:
            with self.assertRaises(OSError) as caught:
                port.write(b"PING\n", timeout=0.2)
            self.assertEqual(caught.exception.errno, errno.ETIMEDOUT)
        finally:
            port.close()


class SendTextTests(unittest.TestCase):
    def test_each_line_is_sent_and_acknowledged(self):
        port, fake = linked(answer=board_answers)
        try:
            self.assertEqual(send_lines(port, ["hello   world", "", "second line"]), [])
            self.assertEqual(b"".join(fake.writes), b"\nhello world\n\nsecond line\n")
        finally:
            port.close()

    def test_a_board_error_is_reported(self):
        port, fake = linked(answer=lambda line: b"ERR ValueError: bad\r\n")
        try:
            problems = send_lines(port, ["SHOW {"])
            self.assertEqual(len(problems), 1)
            self.assertIn("ERR ValueError", problems[0])
        finally:
            port.close()


class ServiceOverBleTests(unittest.TestCase):
    def test_the_cyd_service_drives_the_board_over_ble(self):
        """The service, with the settings `service_for("ble:...")` uses, over a BlePort."""
        host, device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        fake = FakeBoard(device, answer=board_answers)
        fake.start()
        log = []
        svc = cyd.CydService(lambda: ["ble:A4:F0:0F:5B:8B:2A"],
                             lambda path: BlePort("A4:F0:0F:5B:8B:2A", sock=host, att_timeout=1.0),
                             hello_wait=0.0, write_chunk=1 << 16, write_gap=0.0)
        svc.on_connect = lambda path: log.append(("connect", path))
        svc.tick()
        self.assertEqual(log, [("connect", "ble:A4:F0:0F:5B:8B:2A")])
        svc.tick()
        sent = b"".join(fake.writes)
        self.assertIn(b"SHOW ", sent)
        self.assertIn(b"No unacknowledged critical", sent)
        svc._shutdown()


if __name__ == "__main__":
    unittest.main()
