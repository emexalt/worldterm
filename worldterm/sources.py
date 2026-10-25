"""Named news sources and preset groups.

Each source has a short label that is shown in the Node column instead of the
feed's own channel title, which is often long ("International: Top News And
Analysis"). Presets are either a single source key or a group of them.

Sources were picked for broad, mostly wire-style or public-broadcaster
coverage, and every URL was checked to serve a parseable, current feed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str
    label: str          # short Node name, max ~12 chars
    url: str
    description: str = ""


_S = Source
SOURCES: dict[str, Source] = {s.key: s for s in (
    # --- world news -------------------------------------------------------
    _S("bbc", "BBC", "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC News, World"),
    _S("bbc-top", "BBC", "https://feeds.bbci.co.uk/news/rss.xml", "BBC News, Top Stories"),
    _S("npr", "NPR", "https://feeds.npr.org/1001/rss.xml", "NPR News"),
    _S("npr-world", "NPR", "https://feeds.npr.org/1004/rss.xml", "NPR, World"),
    _S("pbs", "PBS", "https://www.pbs.org/newshour/feeds/rss/world", "PBS NewsHour, World"),
    _S("abc-us", "ABC News", "https://abcnews.go.com/abcnews/internationalheadlines", "ABC News (US), International"),
    _S("cbs", "CBS News", "https://www.cbsnews.com/latest/rss/world", "CBS News, World"),
    _S("csmonitor", "CS Monitor", "https://rss.csmonitor.com/feeds/world", "Christian Science Monitor, World"),
    _S("dw", "DW", "https://rss.dw.com/xml/rss-en-top", "Deutsche Welle, Top Stories"),
    _S("france24", "France 24", "https://www.france24.com/en/rss", "France 24, International"),
    _S("rfi", "RFI", "https://www.rfi.fr/en/rss", "Radio France Internationale"),
    _S("euronews", "Euronews", "https://www.euronews.com/rss", "Euronews, Latest"),
    _S("aljazeera", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera English"),
    _S("abc-au", "ABC AU", "https://www.abc.net.au/news/feed/51120/rss.xml", "ABC Australia, Just In"),
    _S("japantimes", "Japan Times", "https://www.japantimes.co.jp/feed/", "The Japan Times, Latest"),
    _S("scmp", "SCMP", "https://www.scmp.com/rss/91/feed", "South China Morning Post, World"),
    _S("un", "UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "UN News"),
    # --- topics -------------------------------------------------------------
    _S("bbc-business", "BBC", "https://feeds.bbci.co.uk/news/business/rss.xml", "BBC News, Business"),
    _S("npr-business", "NPR", "https://feeds.npr.org/1006/rss.xml", "NPR, Business"),
    _S("cnbc", "CNBC", "https://www.cnbc.com/id/100727362/device/rss/rss.html", "CNBC, International"),
    _S("bbc-tech", "BBC", "https://feeds.bbci.co.uk/news/technology/rss.xml", "BBC News, Technology"),
    _S("bbc-science", "BBC", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "BBC News, Science"),
    _S("npr-science", "NPR", "https://feeds.npr.org/1007/rss.xml", "NPR, Science"),
)}

GROUPS: dict[str, tuple[str, ...]] = {
    "us": ("npr", "pbs", "abc-us", "cbs", "csmonitor"),
    "europe": ("bbc", "dw", "france24", "rfi", "euronews"),
    "asia-pacific": ("abc-au", "japantimes", "scmp"),
    "intl": ("bbc", "dw", "france24", "aljazeera", "un"),
    "business": ("bbc-business", "npr-business", "cnbc"),
    "science": ("bbc-science", "npr-science", "bbc-tech"),
    # Every world-news source. bbc-top and the topic feeds are left out:
    # they mostly repeat stories already covered here.
    "all": ("bbc", "npr", "pbs", "abc-us", "cbs", "csmonitor", "dw", "france24", "rfi",
            "euronews", "aljazeera", "abc-au", "japantimes", "scmp", "un"),
}

DEFAULT_PRESET = "all"


def preset_names() -> list[str]:
    return sorted(GROUPS) + sorted(SOURCES)


def resolve(spec: str) -> list[Source]:
    """Turn 'europe' or 'dw,france24,us' into a de-duplicated list of sources."""
    out: list[Source] = []
    seen: set[str] = set()
    for name in (part.strip().lower() for part in spec.split(",")):
        if not name:
            continue
        if name in GROUPS:
            keys = GROUPS[name]
        elif name in SOURCES:
            keys = (name,)
        else:
            raise KeyError(name)
        for key in keys:
            if key not in seen:
                seen.add(key)
                out.append(SOURCES[key])
    if not out:
        raise KeyError(spec)
    return out


def parse_feed_arg(value: str) -> Source:
    """`--feed URL` or `--feed LABEL=URL` (label is used as the Node name)."""
    label, sep, rest = value.partition("=")
    if sep and label and "://" in rest and "/" not in label and ":" not in label:
        return Source(key=label.lower(), label=label.strip(), url=rest.strip())
    return Source(key=value, label="", url=value)


def describe_presets() -> str:
    """Human-readable listing for --list-presets."""
    lines = ["Groups:"]
    for name in sorted(GROUPS):
        marker = " (default)" if name == DEFAULT_PRESET else ""
        lines.append(f"  {name:14}{', '.join(GROUPS[name])}{marker}")
    lines.append("")
    lines.append("Sources:")
    for key in sorted(SOURCES):
        s = SOURCES[key]
        lines.append(f"  {key:14}{s.description}")
    lines.append("")
    lines.append("Combine with commas, e.g.  --preset europe,npr,cnbc")
    return "\n".join(lines)
