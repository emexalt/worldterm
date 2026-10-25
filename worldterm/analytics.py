"""Aggregations behind the dashboard: counts by severity, by source, and by trigger.

Everything here is a pure function over Event objects so it can be unit-tested
without a terminal. Events carry their classifier result (`Event.match`), so the
trigger statistics are a straight aggregation rather than a re-classification.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from .nws import CERTAINTIES, KINDS, RESPONSES, URGENCIES
from .severity import Severity
from .store import Event

SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.WARNING,
    Severity.INDETERMINATE, Severity.CLEAR,
)


@dataclass
class SeverityRow:
    severity: Severity
    total: int = 0
    acknowledged: int = 0
    read: int = 0

    @property
    def outstanding(self) -> int:
        return self.total - self.acknowledged


@dataclass
class SourceRow:
    node: str
    total: int = 0
    by_severity: dict[Severity, int] = field(default_factory=lambda: {s: 0 for s in Severity})
    acknowledged: int = 0
    read: int = 0
    noted: int = 0

    @property
    def ack_pct(self) -> int:
        return round(100 * self.acknowledged / self.total) if self.total else 0


@dataclass
class TriggerRow:
    keyword: str
    level: Severity
    events: int = 0        # events whose text contains this keyword
    deciding: int = 0      # ...of which this keyword's level set the base severity

    @property
    def outranked(self) -> int:
        """Events where the keyword matched but a more severe keyword decided."""
        return self.events - self.deciding


@dataclass
class Summary:
    total: int = 0
    sources: int = 0
    acknowledged: int = 0
    read: int = 0
    noted: int = 0
    by_rule: int = 0          # base severity came from a keyword in the text
    by_category: int = 0      # ...or from a feed category tag
    by_default: int = 0       # nothing matched: default Warning
    empty: int = 0            # no text at all: Indeterminate
    bumped_up: int = 0        # Live/Breaking modifier applied
    bumped_down: int = 0      # Analysis/Opinion/question modifier applied
    distinct_triggers: int = 0

    def pct(self, value: int) -> int:
        return round(100 * value / self.total) if self.total else 0


def severity_rows(events: Iterable[Event]) -> list[SeverityRow]:
    """One row per severity in Netcool order (most severe first). Zero rows for
    CLEAR/INDETERMINATE are dropped so the chart doesn't show permanently-empty bars."""
    rows = {sev: SeverityRow(sev) for sev in Severity}
    for ev in events:
        row = rows[ev.severity]
        row.total += 1
        row.acknowledged += ev.acknowledged
        row.read += ev.read
    out = [rows[sev] for sev in SEVERITY_ORDER]
    return [r for r in out if r.total or r.severity >= Severity.WARNING]


SOURCE_SORTS: tuple[str, ...] = ("total", "critical", "outstanding", "name")


def source_rows(events: Iterable[Event], sort: str = "total") -> list[SourceRow]:
    return group_rows(events, lambda ev: (ev.node,), sort)


def group_rows(events: Iterable[Event], keys: Callable[[Event], Iterable[str]], sort: str = "total") -> list[SourceRow]:
    """Count events per group; *keys* names the group(s) an event belongs to,
    so an event may be counted under several (a weather alert spanning states)."""
    rows: dict[str, SourceRow] = {}
    for ev in events:
        for key in keys(ev):
            row = rows.get(key)
            if row is None:
                row = rows[key] = SourceRow(key)
            row.total += 1
            row.by_severity[ev.severity] += 1
            row.acknowledged += ev.acknowledged
            row.read += ev.read
            row.noted += bool(ev.note)
    out = list(rows.values())
    if sort == "name":
        out.sort(key=lambda r: r.node.lower())
    elif sort == "critical":
        out.sort(key=lambda r: (-r.by_severity[Severity.CRITICAL], -r.by_severity[Severity.MAJOR], -r.total,
                                r.node.lower()))
    elif sort == "outstanding":
        out.sort(key=lambda r: (-(r.total - r.acknowledged), -r.total, r.node.lower()))
    else:
        out.sort(key=lambda r: (-r.total, r.node.lower()))
    return out


TRIGGER_SORTS: tuple[str, ...] = ("events", "deciding", "level")


def trigger_rows(events: Iterable[Event], sort: str = "events") -> list[TriggerRow]:
    rows: dict[str, TriggerRow] = {}
    for ev in events:
        match = ev.match
        if match is None:
            continue
        deciding = set(match.deciding)
        for hit in match.hits:
            row = rows.get(hit.keyword)
            if row is None:
                row = rows[hit.keyword] = TriggerRow(hit.keyword, hit.level)
            row.events += 1
            if hit.keyword in deciding:
                row.deciding += 1
    out = list(rows.values())
    if sort == "deciding":
        out.sort(key=lambda r: (-r.deciding, -r.events, r.keyword))
    elif sort == "level":
        out.sort(key=lambda r: (-int(r.level), -r.events, r.keyword))
    else:
        out.sort(key=lambda r: (-r.events, -r.deciding, r.keyword))
    return out


def level_totals(triggers: Iterable[TriggerRow]) -> dict[Severity, tuple[int, int]]:
    """Per rule level: (distinct keywords that fired, total deciding events)."""
    keywords: Counter[Severity] = Counter()
    deciding: Counter[Severity] = Counter()
    for row in triggers:
        keywords[row.level] += 1
        deciding[row.level] += row.deciding
    return {sev: (keywords[sev], deciding[sev]) for sev in SEVERITY_ORDER if keywords[sev]}


def summary(events: Iterable[Event]) -> Summary:
    s = Summary()
    nodes: set[str] = set()
    keywords: set[str] = set()
    for ev in events:
        s.total += 1
        nodes.add(ev.node)
        s.acknowledged += ev.acknowledged
        s.read += ev.read
        s.noted += bool(ev.note)
        match = ev.match
        if match is None:
            continue
        if match.origin == "text":
            s.by_rule += 1
        elif match.origin == "category":
            s.by_category += 1
        elif match.origin == "default":
            s.by_default += 1
        else:
            s.empty += 1
        if match.modifier > 0:
            s.bumped_up += 1
        elif match.modifier < 0:
            s.bumped_down += 1
        keywords.update(h.keyword for h in match.hits)
    s.sources = len(nodes)
    s.distinct_triggers = len(keywords)
    return s


# =========================================================================== weather
# Aggregations over NWS alert events (Event.alert is set). The alert carries
# its own severity, urgency, certainty, event type, office, zones and timing,
# so these are plain counts, no classifier involved.

def _alerts(events: Iterable[Event]) -> list[Event]:
    return [ev for ev in events if ev.alert is not None]


@dataclass
class TypeRow:
    """One NWS event type (Flood Warning, Small Craft Advisory, ...)."""

    event: str
    total: int = 0
    by_severity: dict[Severity, int] = field(default_factory=lambda: {s: 0 for s in Severity})
    acknowledged: int = 0
    states: set[str] = field(default_factory=set)
    offices: set[str] = field(default_factory=set)

    @property
    def level(self) -> Severity:
        """The level most of this type's alerts carry (ties go to the higher level)."""
        return max(Severity, key=lambda s: (self.by_severity[s], int(s)))


TYPE_SORTS: tuple[str, ...] = ("count", "level", "name")


def type_rows(events: Iterable[Event], sort: str = "count") -> list[TypeRow]:
    rows: dict[str, TypeRow] = {}
    for ev in _alerts(events):
        a = ev.alert
        row = rows.get(a.event)
        if row is None:
            row = rows[a.event] = TypeRow(a.event)
        row.total += 1
        row.by_severity[ev.severity] += 1
        row.acknowledged += ev.acknowledged
        row.states.update(a.states)
        row.offices.add(a.office)
    out = list(rows.values())
    if sort == "name":
        out.sort(key=lambda r: r.event.lower())
    elif sort == "level":
        out.sort(key=lambda r: (-int(r.level), -r.total, r.event.lower()))
    else:
        out.sort(key=lambda r: (-r.total, -int(r.level), r.event.lower()))
    return out


def state_rows(events: Iterable[Event], sort: str = "total") -> list[SourceRow]:
    """Alerts per state or marine area. An alert spanning several counts in each."""
    return group_rows(_alerts(events), lambda ev: ev.alert.states or ("—",), sort)


def office_rows(events: Iterable[Event], sort: str = "total") -> list[SourceRow]:
    return group_rows(_alerts(events), lambda ev: (ev.alert.office,), sort)


def kind_counts(events: Iterable[Event]) -> list[tuple[str, int]]:
    """Warnings / Watches / Advisories / Statements / Outlooks / Other, non-zero only."""
    counts: Counter[str] = Counter(ev.alert.kind for ev in _alerts(events))
    return [(kind, counts[kind]) for kind in KINDS if counts[kind]]


def _hour_buckets(times: Iterable[datetime | None], hours: int, now: datetime) -> tuple[list[int], int]:
    """Count *times* by the hour before *now*, oldest bucket first, plus those older than the window.
    A time in the future lands in the newest bucket; None is skipped."""
    buckets = [0] * hours
    older = 0
    for when in times:
        if when is None:
            continue
        age = max(0.0, (now - when).total_seconds() / 3600)
        if age >= hours:
            older += 1
            continue
        buckets[hours - 1 - int(age)] += 1
    return buckets, older


def issued_histogram(events: Iterable[Event], hours: int = 24, now: datetime | None = None) -> tuple[list[int], int]:
    """Alerts by issue hour over the last *hours*, oldest bucket first, plus the
    number issued before the window."""
    now = now or datetime.now(timezone.utc)
    return _hour_buckets((ev.alert.sent for ev in _alerts(events)), hours, now)


def arrival_histogram(events: Iterable[Event], hours: int = 24, now: datetime | None = None) -> tuple[list[int], int]:
    """Events by the hour worldterm first saw them (`Event.first_seen`), oldest bucket first,
    plus the number first seen before the window. `first_seen` is restored from the state
    file across restarts, so this is the inbound rate; a cold start without a state file puts
    everything in the current hour until the next day has gone by."""
    now = now or datetime.now(timezone.utc)
    return _hour_buckets((ev.first_seen for ev in events), hours, now)


EXPIRY_BUCKETS: tuple[tuple[str, float], ...] = (
    ("ended", 0), ("≤ 1h", 1), ("≤ 6h", 6), ("≤ 24h", 24), ("> 24h", float("inf")),
)


def expiry_buckets(events: Iterable[Event], now: datetime | None = None) -> list[tuple[str, int]]:
    """How soon the active alerts end: ended already, within 1h, 6h, 24h, later."""
    now = now or datetime.now(timezone.utc)
    counts = [0] * len(EXPIRY_BUCKETS)
    for ev in _alerts(events):
        if ev.severity == Severity.CLEAR:
            counts[0] += 1
            continue
        until = ev.alert.active_until
        if until is None:
            counts[-1] += 1
            continue
        left = (until - now).total_seconds() / 3600
        for i, (_, limit) in enumerate(EXPIRY_BUCKETS):
            if left <= limit:
                counts[i] += 1
                break
    return [(label, n) for (label, _), n in zip(EXPIRY_BUCKETS, counts)]


def urgency_certainty(events: Iterable[Event]) -> tuple[list[str], list[str], dict[tuple[str, str], int]]:
    """Cross-tab of urgency against certainty for the active alerts; only the
    values that occur are returned, in CAP order."""
    grid: Counter[tuple[str, str]] = Counter()
    for ev in _alerts(events):
        if ev.severity != Severity.CLEAR:
            grid[(ev.alert.urgency, ev.alert.certainty)] += 1
    urgencies = [u for u in URGENCIES if any(k[0] == u for k in grid)]
    certainties = [c for c in CERTAINTIES if any(k[1] == c for k in grid)]
    return urgencies, certainties, dict(grid)


def response_counts(events: Iterable[Event]) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter(ev.alert.response for ev in _alerts(events) if ev.severity != Severity.CLEAR)
    return [(r, counts[r]) for r in RESPONSES if counts[r]]


@dataclass
class WeatherSummary:
    total: int = 0
    active: int = 0
    ended: int = 0
    states: int = 0
    offices: int = 0
    types: int = 0
    acknowledged: int = 0
    read: int = 0
    noted: int = 0
    updates: int = 0        # alerts that are updates of an earlier message
    immediate: int = 0      # urgency Immediate
    observed: int = 0       # certainty Observed
    warnings: int = 0       # kind Warning

    def pct(self, value: int) -> int:
        return round(100 * value / self.total) if self.total else 0


def weather_summary(events: Iterable[Event]) -> WeatherSummary:
    s = WeatherSummary()
    states: set[str] = set()
    offices: set[str] = set()
    types: set[str] = set()
    for ev in _alerts(events):
        a = ev.alert
        s.total += 1
        if ev.severity == Severity.CLEAR:
            s.ended += 1
        else:
            s.active += 1
        states.update(a.states)
        offices.add(a.office)
        types.add(a.event)
        s.acknowledged += ev.acknowledged
        s.read += ev.read
        s.noted += bool(ev.note)
        s.updates += a.message_type == "Update"
        s.immediate += a.urgency == "Immediate"
        s.observed += a.certainty == "Observed"
        s.warnings += a.kind == "Warning"
    s.states, s.offices, s.types = len(states), len(offices), len(types)
    return s
