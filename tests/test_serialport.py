"""The standard-library serial port: sysfs port discovery, and raw I/O on a pseudo-terminal."""
import os
import pty
import select
import tempfile
import unittest
from pathlib import Path

from worldterm.serialport import USB_SERIAL_IDS, SerialPort, find_ports, usb_id


def make_sysfs(root: Path) -> Path:
    """A fake /sys with a CH340 on ttyUSB0, a native-USB board on ttyACM0 and a motherboard ttyS0.

    Returns the fake /sys/class/tty directory. The layout copies a real sysfs: the class
    entry's `device` link points at the port, and the USB ids live on the USB device above it.
    """
    class_tty = root / "class" / "tty"
    class_tty.mkdir(parents=True)

    def usb_device(bus_path: str, vid: str, pid: str) -> Path:
        dev = root / "devices" / bus_path
        dev.mkdir(parents=True)
        (dev / "idVendor").write_text(vid + "\n")
        (dev / "idProduct").write_text(pid + "\n")
        return dev

    def class_entry(name: str, target: Path) -> None:
        (class_tty / name).mkdir()
        os.symlink(target, class_tty / name / "device")

    ch340 = usb_device("pci0000:00/0000:00:14.0/usb1/1-3", "1a86", "7523")
    port = ch340 / "1-3:1.0" / "ttyUSB0"            # usb-serial: interface, then the port
    port.mkdir(parents=True)
    class_entry("ttyUSB0", port)

    native = usb_device("pci0000:00/0000:00:14.0/usb1/1-4", "303a", "1001")
    iface = native / "1-4:1.0"                      # CDC: the interface is the port's device
    iface.mkdir()
    class_entry("ttyACM0", iface)

    platform = root / "devices" / "platform" / "serial8250"
    platform.mkdir(parents=True)
    class_entry("ttyS0", platform)

    (class_tty / "tty0").mkdir()                    # a console: no device link at all
    return class_tty


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.class_tty = make_sysfs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_usb_id_climbs_to_the_usb_device(self):
        self.assertEqual(usb_id(self.class_tty / "ttyUSB0"), (0x1A86, 0x7523))
        self.assertEqual(usb_id(self.class_tty / "ttyACM0"), (0x303A, 0x1001))
        self.assertIsNone(usb_id(self.class_tty / "ttyS0"))
        self.assertIsNone(usb_id(self.class_tty / "tty0"))
        self.assertIsNone(usb_id(self.class_tty / "ttyNONE"))

    def test_find_ports_filters_by_id(self):
        self.assertEqual(find_ports(sys_tty=self.class_tty), ["/dev/ttyUSB0"])
        wider = USB_SERIAL_IDS | {(0x303A, 0x1001)}
        self.assertEqual(find_ports(ids=wider, sys_tty=self.class_tty, dev_dir="/fake"),
                         ["/fake/ttyACM0", "/fake/ttyUSB0"])

    def test_find_ports_without_sysfs(self):
        self.assertEqual(find_ports(sys_tty=self.root / "missing"), [])


def readable(fd: int, timeout: float = 1.0) -> bool:
    ready, _, _ = select.select([fd], [], [], timeout)
    return bool(ready)


class SerialPortTests(unittest.TestCase):
    """A pseudo-terminal stands in for the USB port: termios works on it, modem lines do not."""

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.path = os.ttyname(self.slave)
        self.port = SerialPort(self.path)

    def tearDown(self):
        self.port.close()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_bytes_pass_through_untouched(self):
        os.write(self.master, b"HELLO worldterm-cyd 1\r\n")
        self.assertEqual(self.port.read(timeout=1.0), b"HELLO worldterm-cyd 1\r\n")
        self.port.write(b"PING\n")
        self.assertTrue(readable(self.master))
        self.assertEqual(os.read(self.master, 100), b"PING\n")   # no echo, no LF to CRLF

    def test_read_times_out_empty(self):
        self.assertEqual(self.port.read(timeout=0.05), b"")

    def test_second_open_is_refused(self):
        with self.assertRaises(OSError):
            SerialPort(self.path)

    def test_lost_device_raises(self):
        os.close(self.master)
        with self.assertRaises(OSError):
            self.port.read(timeout=1.0)

    def test_closed_port_rejects_io_and_closes_once(self):
        self.port.close()
        self.port.close()
        with self.assertRaises(OSError):
            self.port.read()
        with self.assertRaises(OSError):
            self.port.write(b"x")

    def test_not_a_serial_device(self):
        with self.assertRaises(OSError):
            SerialPort(os.devnull)


if __name__ == "__main__":
    unittest.main()
