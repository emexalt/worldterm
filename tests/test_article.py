"""Article text: the fixtures copy page structures measured on live sources (DESIGN.md 6.4)."""
import json
import tempfile
import unittest
from pathlib import Path

from worldterm import article
from worldterm.article import Block, decode, extract, load
from worldterm.feed import FeedError

FIXTURES = Path(__file__).parent / "fixtures"
PAGES = FIXTURES / "articles"

# Two paragraphs long enough between them to clear MIN_TEXT, for the inline cases.
FILLER = (
    "The committee met on Tuesday to discuss the proposal, which has divided members of the "
    "council for most of the year, and agreed to delay a vote until the spring.",
    "Supporters say the plan would bring jobs to the district, while opponents argue that the "
    "costs have been underestimated and that residents were not consulted, a charge officials deny.",
    "A spokesperson for the council said the delay would give time for a public consultation, "
    "and that a revised plan would be published before the vote takes place in March.",
)


def texts(body) -> list[str]:
    return [b.text for b in body.blocks]


def story(*paragraphs: str, before: str = "", after: str = "") -> str:
    """A page with one story container holding *paragraphs* (raw HTML allowed)."""
    inner = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<html><body><nav><a href='/'>Home</a></nav>{before}<div class='story'>{inner}</div>{after}</body></html>"


class FixtureTests(unittest.TestCase):
    def test_article_split_into_sibling_blocks_is_reassembled(self):
        body = extract((PAGES / "split_blocks.html").read_text(encoding="utf-8"), url="https://news.example/1")
        self.assertEqual(body.method, "html")
        self.assertEqual([b.kind for b in body.blocks], ["p", "p", "p", "p", "h", "p", "p", "p"])
        got = texts(body)
        self.assertTrue(got[0].startswith("Recordings released on Thursday"))
        self.assertEqual(got[4], "What the recordings show")
        self.assertTrue(got[-1].startswith("Services on the line"))
        joined = "\n".join(got)
        for furniture in ("By a staff reporter", "Rescue workers searched", "What we know about the collision",
                          "Related topics", "Rail transport", "More on this story", "Survivor describes",
                          "We use cookies", "Copyright", "Script text", "Home"):
            self.assertNotIn(furniture, joined)
        self.assertEqual(body.host, "news.example")
        self.assertEqual(body.words, sum(len(t.split()) for t in got))

    def test_single_container_drops_links_promos_and_asides(self):
        body = extract((PAGES / "single_container.html").read_text(encoding="utf-8"))
        self.assertEqual(body.method, "html")
        got = texts(body)
        self.assertEqual(got[0], "Growers say the harvest is fine; the market is not.")   # the standfirst stays
        self.assertTrue(got[1].startswith("When Ruth Calder"))
        self.assertIn("now faces a growing debt.", got[3])            # an inline link is part of the text
        self.assertEqual([b for b in body.blocks if b.kind != "p"], [
            Block("quote", "We did everything right this year, and it still might not be enough to keep the farm, she said."),
            Block("li", "Soybean prices are down 18 percent since March"),
            Block("li", "Farm loan applications rose by a third"),
        ])
        self.assertTrue(got[-1].startswith("The agriculture department"))
        joined = "\n".join(got)
        for furniture in ("Read more", "Sign up for our newsletter", "Most read", "Published on", "Example World"):
            self.assertNotIn(furniture, joined)

    def test_page_drawn_by_script_uses_the_jsonld_text(self):
        body = extract((PAGES / "jsonld_only.html").read_text(encoding="utf-8"))
        self.assertEqual(body.method, "jsonld")
        got = texts(body)
        self.assertEqual(len(got), 4)    # one paragraph per line, blank lines skipped
        self.assertTrue(got[0].startswith("The harbour authority on Thursday banned fishing from the outer piers"))
        self.assertEqual(got[3], "Anglers' groups called the decision heavy-handed & said they had offered "
                                 "to pay for better lights themselves.")

    def test_video_page_is_not_an_article(self):
        self.assertIsNone(extract((PAGES / "video_page.html").read_text(encoding="utf-8")))


class InlineTests(unittest.TestCase):
    def test_entities_and_brackets_are_plain_text(self):
        body = extract(story("Prices rose 5% &amp; then fell [/b] again [video], &ldquo;sharply&rdquo;, traders said.", *FILLER))
        self.assertEqual(texts(body)[0], "Prices rose 5% & then fell [/b] again [video], “sharply”, traders said.")

    def test_br_is_a_line_break_and_other_whitespace_collapses(self):
        body = extract(story("Line   one,\n  wrapped<br>Line two<br/><br>  Line three", *FILLER))
        self.assertEqual(texts(body)[0], "Line one, wrapped\nLine two\nLine three")

    def test_unclosed_paragraphs_close_at_the_next_one(self):
        body = extract("<html><body><div class='story'>" + "".join(f"<p>{p}" for p in FILLER) + "</div></body></html>")
        self.assertEqual(texts(body), list(FILLER))

    def test_an_unclosed_figure_does_not_swallow_the_page(self):
        page = story(FILLER[0], f"x</p><div class='media'><figure><img src='a.jpg'>A caption</div><p>{FILLER[1]}",
                     FILLER[2])
        got = texts(extract(page))
        self.assertIn(FILLER[1], got)
        self.assertIn(FILLER[2], got)
        self.assertNotIn("A caption", "\n".join(got))

    def test_elements_inside_a_skipped_one_do_not_close_the_page_around_it(self):
        figure = "<figure><div><div><img src='a.jpg'></div></div><figcaption>A caption</figcaption></figure>"
        page = f"<html><body><div class='story'><p>{FILLER[0]}</p>{figure}<p>{FILLER[1]}</p><p>{FILLER[2]}</p></div></body></html>"
        self.assertEqual(texts(extract(page)), list(FILLER))

    def test_duplicate_and_link_heavy_blocks_are_dropped(self):
        link = "<a href='/x'>Read more: an unrelated story with a long headline of its own</a>"
        body = extract(story(FILLER[0], link, FILLER[1], FILLER[0], FILLER[2]))
        self.assertEqual(texts(body), list(FILLER))

    def test_too_little_text_is_not_an_article(self):
        boundary = "w" * article.MIN_TEXT
        self.assertIsNotNone(extract(story(boundary)))
        self.assertIsNone(extract(story(boundary[1:])))
        self.assertIsNone(extract(""))
        self.assertIsNone(extract("not html at all"))

    def test_jsonld_body_with_markup_is_parsed_as_html(self):
        ld = {"@type": "NewsArticle", "articleBody": "".join(f"<p>{p}</p>" for p in FILLER) + "<p>[Updated]</p>"}
        page = f"<html><head><script type='application/ld+json'>{json.dumps(ld)}</script></head><body><p>Teaser.</p></body></html>"
        body = extract(page)
        self.assertEqual(body.method, "jsonld")
        self.assertEqual(texts(body), list(FILLER) + ["[Updated]"])

    def test_jsonld_shorter_than_the_page_text_loses(self):
        ld = {"@type": "NewsArticle", "articleBody": " ".join(FILLER[:2]) * 2}   # long enough, but not 1.2x
        page = story(*FILLER, *(p.upper() for p in FILLER),
                     before=f"<script type='application/ld+json'>{json.dumps(ld)}</script>")
        self.assertEqual(extract(page).method, "html")

    def test_malformed_jsonld_is_ignored(self):
        page = story(*FILLER, before="<script type='application/ld+json'>{\"articleBody\": broken</script>")
        self.assertEqual(texts(extract(page)), list(FILLER))


class DecodeTests(unittest.TestCase):
    def test_charset_from_the_header(self):
        self.assertEqual(decode("café".encode("latin-1"), "text/html; charset=ISO-8859-1"), "café")

    def test_charset_from_a_meta_tag(self):
        data = b'<html><head><meta charset="windows-1252"></head>' + "caf\xe9 “quoted”".encode("cp1252")
        self.assertIn("café “quoted”", decode(data, "text/html"))
        data = b'<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">' + "na\xefve".encode("latin-1")
        self.assertIn("naïve", decode(data))

    def test_the_header_wins_over_the_meta_tag(self):
        data = b'<meta charset="iso-8859-1">' + "café".encode("utf-8")
        self.assertIn("café", decode(data, "text/html; charset=utf-8"))

    def test_unknown_charset_and_bad_bytes_fall_back_to_utf8(self):
        self.assertEqual(decode("café".encode("utf-8"), "text/html; charset=x-no-such-codec"), "café")
        self.assertEqual(decode(b"caf\xff"), "caf�")


class LoadTests(unittest.TestCase):
    def test_load_a_page(self):
        url = (PAGES / "split_blocks.html").as_uri()
        body = load(url)
        self.assertEqual(body.url, url)
        self.assertEqual(body.host, "")
        self.assertEqual(len(body.blocks), 8)

    def test_load_decodes_with_the_page_charset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latin1.html"
            page = story(*(p.replace("council", "conseil général") for p in FILLER))
            path.write_bytes(page.replace("<html>", '<html><head><meta charset="iso-8859-1"></head>').encode("latin-1"))
            self.assertIn("conseil général", load(path.as_uri()).blocks[0].text)

    def test_load_refuses_what_is_not_a_web_page(self):
        with self.assertRaisesRegex(FeedError, r"not a web page \((application|text)/xml\)"):
            load((FIXTURES / "bbc_sample.xml").as_uri())

    def test_load_reports_a_page_without_an_article(self):
        with self.assertRaisesRegex(FeedError, "no article text on the page"):
            load((PAGES / "video_page.html").as_uri())

    def test_load_reports_fetch_failures_and_oversized_pages(self):
        with self.assertRaises(FeedError):
            load((PAGES / "no_such_page.html").as_uri())
        with self.assertRaisesRegex(FeedError, "more than 100 bytes"):
            load((PAGES / "split_blocks.html").as_uri(), limit=100)


if __name__ == "__main__":
    unittest.main()
