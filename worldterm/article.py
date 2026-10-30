"""Article text from a news page, with the standard library: fetch, decode, find the body.

The feeds carry only a summary, so the reader fetches the article's link and looks for the
body in the HTML. `extract` is a pure function: a light element tree built with
`html.parser`, Readability's paragraph scoring, two rules that put back articles split into
sibling blocks, and the JSON-LD `articleBody` for pages drawn by script. `load` fetches,
decodes and extracts one page. DESIGN.md section 6.4 explains each step and where it was
measured.
"""
from __future__ import annotations

import codecs
import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterator
from urllib.parse import urlsplit

from .feed import FeedError, fetch_with_type

MAX_PAGE = 5_000_000      # bytes; the largest page measured was 1.2 MB (SCMP)
MIN_TEXT = 500            # characters of paragraphs shown, below which a page is not an article
MIN_PARAGRAPH = 25        # shorter paragraphs are not scored (Readability's threshold)
JSONLD_MARGIN = 1.2       # the JSON-LD text wins only when it is this much longer than the HTML's
HTML_TYPES = ("text/html", "application/xhtml+xml")

# Elements whose contents never belong to the text: code, media, form controls, menus,
# asides and image captions.
SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas",
    "select", "button", "textarea", "nav", "footer", "aside", "figure", "figcaption",
})
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
})
# The start of any of these closes an open <p>, as it does in a browser.
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre",
    "section", "table", "ul",
})
# A candidate's starting score, from Readability.
TAG_WEIGHT = {
    "article": 10, "div": 5, "main": 5, "section": 3, "pre": 3, "td": 3, "blockquote": 3,
    "ol": -3, "ul": -3, "li": -3, "dl": -3, "form": -3,
    "h1": -5, "h2": -5, "h3": -5, "h4": -5, "h5": -5, "h6": -5, "th": -5,
}
# Words in a class, id, data-component or role that mark content, or page furniture.
POS = re.compile(r"article|body|content|entry|main|page|post|text|blog|story|paragraph|wysiwyg", re.I)
NEG = re.compile(
    r"comment|footnote|masthead|outbrain|taboola|promo|teaser|related|sidebar|sponsor|share|social"
    r"|newsletter|subscri|signup|caption|byline|advert|adblock|banner|cookie|consent|breadcrumb"
    r"|recommend|popular|trending|most-?read|modal|links-block|widget", re.I)
# Elements that become blocks of text, and their kind.
EMIT = {"p": "p", "pre": "p", "li": "li", "blockquote": "quote",
        "h2": "h", "h3": "h", "h4": "h", "h5": "h", "h6": "h"}

_WS = re.compile(r"\s+")
_BREAK = object()   # a <br> in the stream of strings


@dataclass(frozen=True)
class Block:
    """One block of article text: "p" a paragraph, "h" a subheading, "li" a list item, "quote"."""
    kind: str
    text: str            # plain text, never markup; a <br> survives as a line break


@dataclass(frozen=True)
class Body:
    blocks: tuple[Block, ...]
    url: str = ""
    method: str = "html"     # "html", or "jsonld" when the text came from the page's JSON-LD

    @property
    def words(self) -> int:
        return sum(len(block.text.split()) for block in self.blocks)

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


# ---------------------------------------------------------------------------- tree
class _Node:
    __slots__ = ("tag", "parent", "kids", "cls", "names", "score", "chars")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag = tag
        self.parent = parent
        self.kids: list[_Node | str] = []
        self.cls = attrs.get("class", "")
        self.names = " ".join(v for k in ("class", "id", "data-component", "role") if (v := attrs.get(k)))
        self.score: float | None = None     # None: not a candidate
        self.chars = 0                      # characters of qualifying paragraph text inside

    def elements(self) -> list[_Node]:
        return [kid for kid in self.kids if isinstance(kid, _Node)]


class _TreeBuilder(HTMLParser):
    """A light element tree, mended the way a browser would mend it, without the furniture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root
        self.skipping: list[str] | None = None   # tags open inside a skipped element, it first
        self.jsonld: list[str] = []
        self._ld: list[str] | None = None        # the JSON-LD script being read

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skipping is not None:
            if tag not in VOID_TAGS:
                self.skipping.append(tag)
            return
        a = {k: v or "" for k, v in attrs}
        if tag == "script" and a.get("type", "").strip().lower() == "application/ld+json":
            self._ld = []
            return
        if tag in SKIP_TAGS:
            self.skipping = [tag]
            return
        if self.cur.tag == "p" and tag in BLOCK_TAGS:
            self.cur = self.cur.parent    # type: ignore[assignment]
        elif self.cur.tag == "li" and tag == "li":
            self.cur = self.cur.parent    # type: ignore[assignment]
        node = _Node(tag, a, self.cur)
        self.cur.kids.append(node)
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_endtag(self, tag: str) -> None:
        if self._ld is not None:
            if tag == "script":
                self.jsonld.append("".join(self._ld))
                self._ld = None
            return
        if self.skipping is not None:
            if tag in self.skipping:
                i = len(self.skipping) - 1 - self.skipping[::-1].index(tag)
                del self.skipping[i:]
                if not self.skipping:
                    self.skipping = None
                return
            if self._open(tag) is None:
                return
            self.skipping = None      # an enclosing element ended, so the skipped one was never closed
        node = self._open(tag)
        if node is not None:
            self.cur = node.parent    # type: ignore[assignment]

    def handle_data(self, data: str) -> None:
        if self._ld is not None:
            self._ld.append(data)
        elif self.skipping is None:
            self.cur.kids.append(data)

    def _open(self, tag: str) -> _Node | None:
        """The innermost open element named *tag*, if any."""
        node: _Node | None = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        return node


def _parse(page: str) -> _TreeBuilder:
    builder = _TreeBuilder()
    try:
        builder.feed(page)
        builder.close()
    except Exception:  # html.parser is lenient; keep what was built before a pathological input
        pass
    return builder


def _elements(root: _Node) -> Iterator[_Node]:
    """Every element under *root*, in document order."""
    stack = list(reversed(root.elements()))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.elements()))


def _strings(node: _Node) -> Iterator[tuple[object, bool]]:
    """(string or _BREAK, inside a link) for everything under *node*, in document order."""
    stack: list[tuple[Iterator[_Node | str], bool]] = [(iter(node.kids), node.tag == "a")]
    while stack:
        kids, in_link = stack[-1]
        kid = next(kids, None)
        if kid is None:
            stack.pop()
        elif isinstance(kid, str):
            yield kid, in_link
        elif kid.tag == "br":
            yield _BREAK, in_link
        else:
            stack.append((iter(kid.kids), in_link or kid.tag == "a"))


def _text(node: _Node) -> tuple[str, float]:
    """The text under *node*, whitespace collapsed but <br> kept, and the share of it in links."""
    parts: list[str] = []
    linked = 0
    for s, in_link in _strings(node):
        if s is _BREAK:
            parts.append("\n")
            continue
        s = _WS.sub(" ", s)  # type: ignore[arg-type]
        parts.append(s)
        if in_link:
            linked += len(s.strip())
    text = re.sub(r" *\n[\n ]*", "\n", re.sub(r" {2,}", " ", "".join(parts))).strip()
    return text, (min(1.0, linked / len(text)) if text else 1.0)


# ---------------------------------------------------------------------------- choosing the body
def _name_weight(node: _Node) -> int:
    if not node.names:
        return 0
    return (25 if POS.search(node.names) else 0) - (25 if NEG.search(node.names) else 0)


def _score(root: _Node) -> list[_Node]:
    """Readability's scores: each paragraph credits its parent, half to its grandparent, less above."""
    candidates: list[_Node] = []
    for p in _elements(root):
        if p.tag not in ("p", "pre"):
            continue
        text, density = _text(p)
        if len(text) < MIN_PARAGRAPH:
            continue
        if density < 0.5:
            node: _Node | None = p
            while node is not None:
                node.chars += len(text)
                node = node.parent
        points = 1 + text.count(",") + min(len(text) // 100, 3)
        node, level = p.parent, 0
        while node is not None and node is not root and level < 5:
            if node.score is None:
                node.score = TAG_WEIGHT.get(node.tag, 0) + _name_weight(node)
                candidates.append(node)
            node.score += points / (1 if level == 0 else 2 if level == 1 else level * 3)
            node, level = node.parent, level + 1
    for node in candidates:
        node.score = (node.score or 0) * (1 - _text(node)[1])
    return candidates


def _choose(candidates: list[_Node], root: _Node) -> tuple[_Node, list[_Node]]:
    """The element holding the article, and it with the siblings that continue it."""
    top = max(candidates, key=lambda n: n.score or 0)
    best = top.score or 0
    # A parent that scores higher than the step below it wins; stop below a third of the best.
    last, parent = best, top.parent
    while parent is not None and parent is not root:
        if parent.score is not None:
            if parent.score < best / 3:
                break
            if parent.score > last:
                top = parent
                break
            last = parent.score
        parent = parent.parent
    # Wrappers that hold nothing else: their parent is where the siblings are.
    while top.parent is not None and top.parent is not root and len(top.parent.elements()) == 1:
        top = top.parent
    parent = top.parent
    if parent is None or parent is root:
        return top, [top]
    # Siblings of the same tag and class continue an article split into blocks (BBC); others
    # join on a score of their own.
    floor = max(10.0, (top.score or 0) * 0.2)
    return top, [n for n in parent.elements()
                 if n is top or (top.cls and n.tag == top.tag and n.cls == top.cls)
                 or (n.score is not None and n.score >= floor)]


def _emit(chosen: list[_Node], keep: _Node | None = None) -> list[Block]:
    """The chosen elements' blocks of text, in document order, without furniture or link lists.

    *keep*, the best-scoring element, is never dropped for its name; a joined sibling can be
    (a byline block shares its wrapper class with the text blocks).
    """
    total = sum(n.chars for n in chosen)
    blocks: list[Block] = []
    seen: set[str] = set()
    stack = list(reversed(chosen))
    while stack:
        node = stack.pop()
        # Furniture by name, unless it holds most of the text (ABC Australia wraps its body so).
        if node is not keep and node.names and NEG.search(node.names) and node.chars < total / 2:
            continue
        kind = EMIT.get(node.tag)
        if kind is None:
            stack.extend(reversed(node.elements()))
            continue
        text, density = _text(node)
        if text and density < 0.5 and text not in seen:
            seen.add(text)
            blocks.append(Block(kind, text))
    # A heading with no paragraph after it introduced something that was dropped ("Related topics").
    tidy: list[Block] = []
    for block in blocks:
        if block.kind == "h" and tidy and tidy[-1].kind == "h":
            tidy.pop()
        tidy.append(block)
    while tidy and tidy[-1].kind == "h":
        tidy.pop()
    return tidy


# ---------------------------------------------------------------------------- JSON-LD
def _jsonld_body(blobs: list[str]) -> str:
    """The longest `articleBody` in the page's JSON-LD, or ""."""
    best = ""
    for blob in blobs:
        try:
            data = json.loads(blob, strict=False)
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                body = item.get("articleBody")
                if isinstance(body, str) and len(body) > len(best):
                    best = body
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return best


def _jsonld_blocks(body: str) -> list[Block]:
    if re.search(r"<(p|br|div|h[2-6]|li)\b", body, re.I):   # some sites put HTML in it
        return _emit([_parse(body).root])
    return [Block("p", _WS.sub(" ", html.unescape(line)).strip()) for line in body.splitlines() if line.strip()]


# ---------------------------------------------------------------------------- public
def extract(page: str, url: str = "") -> Body | None:
    """The article in an HTML page, or None when the page has none (a video, a live-page shell)."""
    builder = _parse(page)
    candidates = _score(builder.root)
    blocks: list[Block] = []
    if candidates:
        top, chosen = _choose(candidates, builder.root)
        blocks = _emit(chosen, keep=top)
    # Judge by the paragraphs that will be shown: a video page's chosen element can hold an
    # ad-block notice and a channel guide, which are dropped above.
    chars = sum(len(b.text) for b in blocks if b.kind == "p" and len(b.text) >= MIN_PARAGRAPH)
    ld = _jsonld_body(builder.jsonld)
    if len(ld) >= MIN_TEXT and len(ld) > JSONLD_MARGIN * chars:
        ld_blocks = _jsonld_blocks(ld)
        if ld_blocks:
            return Body(tuple(ld_blocks), url, "jsonld")
    if chars < MIN_TEXT:
        return None
    return Body(tuple(blocks), url, "html")


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)


def decode(data: bytes, content_type: str = "") -> str:
    """Bytes to text: the Content-Type's charset, else a <meta> charset near the top, else UTF-8."""
    names: list[str] = []
    header = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", content_type, re.I)
    if header:
        names.append(header.group(1))
    meta = _META_CHARSET.search(data[:4096])
    if meta:
        names.append(meta.group(1).decode("ascii"))
    for name in names:
        try:
            codec = codecs.lookup(name)
        except LookupError:
            continue
        return data.decode(codec.name, errors="replace")
    return data.decode("utf-8", errors="replace")


def load(url: str, timeout: float = 15.0, *, limit: int = MAX_PAGE) -> Body:
    """Fetch *url* and extract its article. FeedError says why when there is none to be had."""
    data, content_type = fetch_with_type(url, timeout, limit=limit)
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind and kind not in HTML_TYPES:
        raise FeedError(f"not a web page ({kind})")
    body = extract(decode(data, content_type), url)
    if body is None:
        raise FeedError("no article text on the page")
    return body
