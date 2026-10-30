import unittest
from datetime import timezone
from pathlib import Path

from worldterm import feed

FIX = Path(__file__).parent / "fixtures"


class ParseRssTests(unittest.TestCase):
    def setUp(self):
        self.articles = feed.parse(FIX.joinpath("bbc_sample.xml").read_bytes(), "fallback")

    def test_item_count_matches_fixture(self):
        raw = FIX.joinpath("bbc_sample.xml").read_text()
        self.assertEqual(len(self.articles), raw.count("<item>"))

    def test_source_is_channel_title(self):
        self.assertEqual(self.articles[0].source, "BBC News")

    def test_fields_populated(self):
        a = self.articles[0]
        self.assertTrue(a.title)
        self.assertTrue(a.summary)
        self.assertTrue(a.link.startswith("https://"))
        self.assertTrue(a.guid)
        self.assertIsNotNone(a.published)
        self.assertIsNotNone(a.published.tzinfo)

    def test_guids_unique(self):
        guids = [a.guid for a in self.articles]
        self.assertEqual(len(guids), len(set(guids)))

    def test_npr_content_encoded_not_used_when_description_present(self):
        arts = feed.parse(FIX.joinpath("npr_sample.xml").read_bytes())
        self.assertTrue(arts)
        for a in arts:
            self.assertNotIn("<", a.summary)
            self.assertNotIn("img src", a.summary)


class ParseAtomTests(unittest.TestCase):
    def setUp(self):
        self.articles = feed.parse(FIX.joinpath("atom_sample.xml").read_bytes())

    def test_count_and_source(self):
        self.assertEqual(len(self.articles), 3)
        self.assertEqual(self.articles[0].source, "Example Atom Wire")

    def test_alternate_link_preferred(self):
        self.assertEqual(self.articles[0].link, "https://example.org/news/quake")

    def test_html_summary_stripped(self):
        self.assertEqual(self.articles[0].summary, "A magnitude 7.1 quake hit at dawn.")

    def test_published_preferred_over_updated_and_tz_kept(self):
        dt = self.articles[0].published
        self.assertEqual(dt.astimezone(timezone.utc).hour, 10)

    def test_content_used_when_no_summary(self):
        self.assertIn("Inflation remains sticky", self.articles[1].summary)

    def test_category_terms(self):
        self.assertEqual(self.articles[0].categories, ["World"])

    def test_bad_date_and_missing_link(self):
        a = self.articles[2]
        self.assertIsNone(a.published)
        self.assertEqual(a.link, "")
        self.assertTrue(a.guid.startswith("urn:uuid:3"))


class HelperTests(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(feed.strip_html("<p>Hi&nbsp;<b>there</b></p><br>x"), "Hi there x")
        self.assertEqual(feed.strip_html(None), "")

    def test_parse_date_rfc822_and_iso(self):
        self.assertEqual(feed.parse_date("Sat, 10 Oct 2026 00:45:51 GMT").year, 2026)
        self.assertEqual(feed.parse_date("2026-10-09T11:30:00Z").minute, 30)
        self.assertIsNone(feed.parse_date("garbage"))

    def test_parse_error(self):
        with self.assertRaises(feed.FeedError):
            feed.parse(b"<html><body>nope</body></html>")
        with self.assertRaises(feed.FeedError):
            feed.parse(b"<<<")

    def test_guid_hashed_when_missing(self):
        arts = feed.parse(b"<rss><channel><title>T</title><item><title>A</title></item></channel></rss>")
        self.assertTrue(arts[0].guid.startswith("sha1:"))

    def test_fetch_file_url(self):
        data = feed.fetch(FIX.joinpath("atom_sample.xml").as_uri())
        self.assertIn(b"<feed", data)

    def test_fetch_error(self):
        with self.assertRaises(feed.FeedError):
            feed.fetch("http://127.0.0.1:9/", timeout=2)

    def test_fetch_with_type_reports_the_content_type(self):
        data, content_type = feed.fetch_with_type(FIX.joinpath("articles", "video_page.html").as_uri())
        self.assertIn(b"<html", data)
        self.assertEqual(content_type, "text/html")

    def test_fetch_with_type_refuses_a_reply_over_the_limit(self):
        url = FIX.joinpath("atom_sample.xml").as_uri()
        size = len(feed.fetch(url))
        self.assertEqual(len(feed.fetch_with_type(url, limit=size)[0]), size)
        with self.assertRaisesRegex(feed.FeedError, "more than 100 bytes"):
            feed.fetch_with_type(url, limit=100)


if __name__ == "__main__":
    unittest.main()
