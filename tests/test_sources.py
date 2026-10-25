import unittest

from worldterm import sources
from worldterm.sources import GROUPS, SOURCES, Source, parse_feed_arg, resolve


class SourcesTests(unittest.TestCase):
    def test_catalogue_is_consistent(self):
        for key, src in SOURCES.items():
            self.assertEqual(key, src.key)
            self.assertTrue(src.url.startswith("https://"), key)
            self.assertTrue(src.label, key)
            self.assertLessEqual(len(src.label), 12, f"{key} label too long for Node column")
        for name, keys in GROUPS.items():
            self.assertNotIn(name, SOURCES, f"group {name} shadows a source")
            for key in keys:
                self.assertIn(key, SOURCES, f"group {name} references unknown {key}")
        self.assertIn(sources.DEFAULT_PRESET, GROUPS)

    def test_resolve_single_and_group(self):
        self.assertEqual([s.key for s in resolve("dw")], ["dw"])
        self.assertEqual([s.key for s in resolve("europe")], list(GROUPS["europe"]))

    def test_resolve_combines_and_dedupes_in_order(self):
        keys = [s.key for s in resolve("europe, npr ,bbc,CNBC")]
        self.assertEqual(keys, list(GROUPS["europe"]) + ["npr", "cnbc"])

    def test_resolve_unknown(self):
        with self.assertRaises(KeyError):
            resolve("europe,nope")
        with self.assertRaises(KeyError):
            resolve(" , ")

    def test_parse_feed_arg_plain_url(self):
        s = parse_feed_arg("https://example.org/rss?a=1&b=2")
        self.assertEqual((s.label, s.url), ("", "https://example.org/rss?a=1&b=2"))

    def test_parse_feed_arg_labelled(self):
        s = parse_feed_arg("Wire=https://example.org/rss?a=1")
        self.assertEqual((s.label, s.url), ("Wire", "https://example.org/rss?a=1"))

    def test_parse_feed_arg_file_url(self):
        s = parse_feed_arg("file:///tmp/x.xml")
        self.assertEqual((s.label, s.url), ("", "file:///tmp/x.xml"))

    def test_describe_lists_everything(self):
        text = sources.describe_presets()
        for name in list(GROUPS) + list(SOURCES):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
