import unittest

from worldterm.severity import Severity
from worldterm.ui.styles import ACK_STYLE, SEVERITY_COLORS, SEVERITY_STYLE, darken, severity_style


def luminance(hex_color):
    v = hex_color.lstrip("#")
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(v[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class StyleTests(unittest.TestCase):
    def test_darken(self):
        self.assertEqual(darken("#ff8000", 0.5), "#804000")
        self.assertEqual(darken("#000000"), "#000000")

    def test_ack_is_darker_and_readable(self):
        for sev, (bg, _fg) in SEVERITY_COLORS.items():
            dark = darken(bg)
            self.assertLess(luminance(dark), luminance(bg), sev)
            self.assertGreaterEqual(contrast(dark, "#e4e4e4"), 4.0, f"{sev} acked text contrast")
            self.assertIn(dark, ACK_STYLE[sev])

    def test_severity_colours_are_distinct(self):
        backgrounds = [bg for bg, _ in SEVERITY_COLORS.values()]
        self.assertEqual(len(backgrounds), len(set(backgrounds)))

    def test_severity_style_flags(self):
        self.assertEqual(severity_style(Severity.MAJOR), SEVERITY_STYLE[Severity.MAJOR])
        self.assertEqual(severity_style(Severity.MAJOR, acknowledged=True), ACK_STYLE[Severity.MAJOR])
        self.assertIn("bold", severity_style(Severity.MINOR, new=True))
        self.assertNotIn("bold", severity_style(Severity.MINOR, acknowledged=True, new=True))
        self.assertIn("underline", severity_style(Severity.MINOR, selected=True))


if __name__ == "__main__":
    unittest.main()
