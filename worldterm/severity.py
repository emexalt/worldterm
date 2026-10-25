"""Classify articles into Netcool-style severity levels using keyword rules."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache


class Severity(IntEnum):
    CLEAR = 0
    INDETERMINATE = 1
    WARNING = 2
    MINOR = 3
    MAJOR = 4
    CRITICAL = 5

    @property
    def label(self) -> str:
        return self.name.title()

    @property
    def short(self) -> str:
        return {
            Severity.CLEAR: "CLR",
            Severity.INDETERMINATE: "IND",
            Severity.WARNING: "WRN",
            Severity.MINOR: "MIN",
            Severity.MAJOR: "MAJ",
            Severity.CRITICAL: "CRT",
        }[self]


# Each keyword is a regex fragment matched with word boundaries on the left and a
# loose right boundary so "killed" matches "kill", "evacuated" matches "evacuat", etc.
RULES: list[tuple[Severity, tuple[str, ...]]] = [
    (Severity.CRITICAL, (
        "wars?", "wartime", "warfare", "warplane", "warship", "warhead", "warlord",
        "invasion", "invade", "airstrike", "air strike", "strikes? on", "missile",
        "nuclear", "attack", "kill", "dead", "deadly", "deadliest", "deaths?", "death toll",
        "massacre", "explosion", "blast", "bomb", "shooting", "gunman", "terror", "earthquake",
        "tsunami", "hurricane", "typhoon", "cyclone", "eruption", "coups?",
        "assassinat", "pandemic", "epidemic", "outbreak", "ebola", "cholera", "virus",
        "crash", "evacuat", "state of emergency", "hostage", "famine", "genocide",
        "tornado", "wildfire", "landslide", "avalanche", "stampede",
    )),
    (Severity.MAJOR, (
        "election", "sanction", "ceasefire", "tariff", "recession", "inflation",
        "protest", "riot", "court", "ruling", "indict", "arrest", "resign", "impeach",
        "flood", "storm", "strikes?", "deal", "treaty", "summit", "outage",
        "cyberattack", "hack", "recall", "crisis", "emergency", "collapse", "warn",
        "threat", "troops", "military", "president", "prime minister", "parliament",
        "vote", "referendum", "elect", "elected", "election", "electoral", "sentenced",
        "convicted", "guilty", "trial", "murder", "steal", "stolen", "theft", "thieves",
        "robber", "fraud", "lawsuit", "sues?", "sued", "suing", "disease", "shortage",
    )),
    (Severity.MINOR, (
        "sport", "football", "soccer", "cricket", "tennis", "golf", "cup", "olympic",
        "celebrit", "film", "movie", "music", "album", "royal", "festival", "award",
        "fashion", "review", "recipe", "travel", "tourist", "wedding", "actor",
        "singer", "concert", "museum", "zoo", "pets?", "quiz", "tv show", "stars?",
    )),
]

_UP_PREFIXES = re.compile(r"^\s*(live|breaking|urgent)\b", re.IGNORECASE)
_DOWN_PREFIXES = re.compile(
    r"^\s*(analysis|opinion|explainer|explained|comment|in pictures|watch|listen|quiz)\b"
    r"|^\s*why\b.*\?\s*$"
    r"|^\s*what\b.*\?\s*$"
    r"|^\s*how\b.*\?\s*$",
    re.IGNORECASE,
)

# Keywords that must match a whole word even though their level normally
# matches prefixes. Each one was a measured false-positive source: "war" fired
# on warning/warranty/warehouse, "elect" on electric, "coup" on couple, "dead"
# on deadlock, "strike" on striker, "sue" on Suez.
WHOLE_WORDS: frozenset[str] = frozenset({"wars?", "coups?", "dead", "elect", "strikes?", "sues?"})

_WORDS: dict[Severity, tuple[str, ...]] = dict(RULES)


def _compile(sev: Severity, words: tuple[str, ...]) -> re.Pattern[str]:
    # Severe keywords match as prefixes ("kill" -> "killed"); MINOR ones are
    # whole words so "pet" never fires inside "petrol" or "competition".
    def term(word: str) -> str:
        whole = sev == Severity.MINOR or word in WHOLE_WORDS
        return word + (r"\b" if whole else "")

    # Longest alternative first: regex alternation is ordered, so otherwise
    # "deaths?" would always win over "death toll".
    ordered = sorted(words, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(term(w) for w in ordered) + ")", re.IGNORECASE)


_COMPILED: list[tuple[Severity, re.Pattern[str]]] = [(sev, _compile(sev, words)) for sev, words in RULES]


@dataclass(frozen=True)
class Hit:
    """One rule keyword found in an article's text."""

    keyword: str      # the RULES entry, e.g. "kill" (matched "killed") or "deaths?"
    level: Severity   # the rule level the keyword belongs to


@dataclass(frozen=True)
class MatchDetails:
    """Everything the classifier decided for one article."""

    severity: Severity           # final severity, after modifiers
    base: Severity               # severity before modifiers
    origin: str                  # "text" | "category" | "default" | "empty"
    hits: tuple[Hit, ...]        # distinct keywords found, in rule order
    prefix: str | None = None    # "up" (Live/Breaking) or "down" (Analysis/...) if present
    modifier: int = 0            # effect of the prefix on the level: +1, -1 or 0

    @property
    def deciding(self) -> tuple[str, ...]:
        """Keywords whose level set the base severity (the others were outranked)."""
        if self.origin not in ("text", "category"):
            return ()
        return tuple(h.keyword for h in self.hits if h.level == self.base)


@lru_cache(maxsize=4096)
def _canonical(level: int, matched: str) -> str:
    """Map matched text back to the RULES entry that produced it ("killed" -> "kill")."""
    for word in sorted(_WORDS[Severity(level)], key=len, reverse=True):
        if re.fullmatch(word, matched, re.IGNORECASE):
            return word
    return matched


def _hits(text: str) -> list[Hit]:
    found: list[Hit] = []
    seen: set[str] = set()
    for sev, pattern in _COMPILED:
        for m in pattern.finditer(text):
            keyword = _canonical(int(sev), m.group(0).lower())
            if keyword not in seen:
                seen.add(keyword)
                found.append(Hit(keyword, sev))
    return found


def match_details(title: str, summary: str = "", categories: list[str] | None = None) -> MatchDetails:
    """Classify an article and report which keywords and modifiers did it."""
    text = f"{title} {summary}".strip()
    if not text:
        return MatchDetails(Severity.INDETERMINATE, Severity.INDETERMINATE, "empty", ())

    hits = _hits(text)
    origin = "default"
    base = Severity.WARNING
    if hits:
        # Rules are ordered most severe first; the most severe hit wins.
        base = max(h.level for h in hits)
        origin = "text"
    elif categories:
        hits = _hits(" ".join(categories))
        if hits:
            base = max(h.level for h in hits)
            origin = "category"

    prefix: str | None = None
    level = int(base)
    if _UP_PREFIXES.search(title):
        prefix = "up"
        level = min(level + 1, Severity.CRITICAL)
    elif _DOWN_PREFIXES.search(title):
        prefix = "down"
        level = max(level - 1, Severity.WARNING)
    return MatchDetails(Severity(level), base, origin, tuple(hits), prefix, level - int(base))


def classify(title: str, summary: str = "", categories: list[str] | None = None) -> Severity:
    """Return the severity for an article given its title/summary text."""
    return match_details(title, summary, categories).severity


def display_keyword(keyword: str) -> str:
    """Human form of a RULES entry: "deaths?" -> "death", "strikes? on" -> "strike on"."""
    return re.sub(r"\w\?", "", keyword)


def matched_keywords(title: str, summary: str = "") -> list[str]:
    """Debug helper: which keywords fired for this text (used by the reader view)."""
    text = f"{title} {summary}"
    hits: list[str] = []
    for _sev, pattern in _COMPILED:
        hits.extend(m.group(0).lower() for m in pattern.finditer(text))
    seen: set[str] = set()
    return [h for h in hits if not (h in seen or seen.add(h))]
