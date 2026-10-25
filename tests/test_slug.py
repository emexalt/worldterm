import unittest
from datetime import datetime

from worldterm.slug import note_filename, slugify


class SlugTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Trump announces deal for Russian diesel"), "trump-announces-deal-for-russian-diesel")

    def test_punctuation_and_unicode(self):
        self.assertEqual(slugify("Zelensky: it's a 'gift' — to Putin! Café"), "zelensky-it-s-a-gift-to-putin-cafe")

    def test_empty(self):
        self.assertEqual(slugify(""), "untitled")
        self.assertEqual(slugify("!!!"), "untitled")

    def test_truncates_on_word_boundary(self):
        s = slugify("a" * 30 + " " + "b" * 30 + " " + "c" * 30, max_len=40)
        self.assertEqual(s, "a" * 30)
        self.assertLessEqual(len(s), 40)

    def test_note_filename(self):
        self.assertEqual(note_filename("Hello World", datetime(2026, 10, 10)), "20261010-hello-world.md")


if __name__ == "__main__":
    unittest.main()
