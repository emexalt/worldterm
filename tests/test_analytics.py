import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worldterm import analytics
from worldterm.feed import Article
from worldterm.severity import Severity
from worldterm.store import Store

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def article(guid, title, summary="", source="Wire", categories=None):
    return Article(guid, title, summary, f"https://x/{guid}", NOW, source, categories or [])


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.load()
        self.store.merge([
            article("a", "Earthquake kills dozens in coastal city", source="Wire"),
            article("b", "Football fans killed in stadium collapse", source="Wire"),
            article("c", "Court ruling on tariffs expected Monday", source="Wire"),
            article("d", "Town council meets to discuss library hours", source="Local"),
            article("e", "Town council meets", source="Local", categories=["Sport"]),
            article("f", "Live: Court ruling on tariffs", source="Local"),
            article("g", "Analysis: Why the war matters", source="Local"),
            article("h", "", source="Local"),
        ])
        self.events = list(self.store.events.values())
        self.store.ack("a", True)
        self.store.ack("c", True)
        self.store.mark_read("a")
        self.store.set_note("b", "notes/x.md")

    def tearDown(self):
        self.tmp.cleanup()

    def test_severity_rows(self):
        rows = {r.severity: r for r in analytics.severity_rows(self.events)}
        self.assertEqual([r.severity for r in analytics.severity_rows(self.events)],
                         [Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.WARNING, Severity.INDETERMINATE])
        self.assertNotIn(Severity.CLEAR, rows, "empty CLEAR row is dropped")
        self.assertEqual(rows[Severity.CRITICAL].total, 3)       # a, b, f (Live: MAJOR -> CRITICAL)
        self.assertEqual(rows[Severity.CRITICAL].acknowledged, 1)
        self.assertEqual(rows[Severity.CRITICAL].outstanding, 2)
        self.assertEqual(rows[Severity.MAJOR].total, 2)          # c, g (Analysis: CRITICAL -> MAJOR)
        self.assertEqual(rows[Severity.MINOR].total, 1)          # e via category
        self.assertEqual(rows[Severity.WARNING].total, 1)        # d
        self.assertEqual(rows[Severity.INDETERMINATE].total, 1)  # h
        self.assertEqual(sum(r.total for r in rows.values()), 8)

    def test_source_rows_and_sorts(self):
        rows = analytics.source_rows(self.events)
        self.assertEqual([r.node for r in rows], ["Local", "Wire"])  # 5 vs 3, total desc
        local, wire = rows
        self.assertEqual(wire.total, 3)
        self.assertEqual(wire.by_severity[Severity.CRITICAL], 2)
        self.assertEqual(wire.by_severity[Severity.MAJOR], 1)
        self.assertEqual(wire.acknowledged, 2)
        self.assertEqual(wire.ack_pct, 67)
        self.assertEqual(wire.read, 1)
        self.assertEqual(wire.noted, 1)
        self.assertEqual(local.by_severity[Severity.INDETERMINATE], 1)
        self.assertEqual([r.node for r in analytics.source_rows(self.events, "name")], ["Local", "Wire"])
        self.assertEqual([r.node for r in analytics.source_rows(self.events, "critical")], ["Wire", "Local"])
        self.assertEqual([r.node for r in analytics.source_rows(self.events, "outstanding")], ["Local", "Wire"])

    def test_trigger_rows(self):
        rows = {r.keyword: r for r in analytics.trigger_rows(self.events)}
        self.assertEqual(rows["kill"].events, 2)          # a, b ("killed" maps to "kill")
        self.assertEqual(rows["kill"].deciding, 2)
        self.assertEqual(rows["kill"].level, Severity.CRITICAL)
        self.assertEqual(rows["earthquake"].events, 1)
        self.assertEqual(rows["collapse"].events, 1)      # b: matched but outranked by "kill"
        self.assertEqual(rows["collapse"].deciding, 0)
        self.assertEqual(rows["collapse"].outranked, 1)
        self.assertEqual(rows["football"].deciding, 0)
        self.assertEqual(rows["court"].events, 2)         # c, f
        self.assertEqual(rows["court"].deciding, 2)       # base is MAJOR for both; the Live bump is a modifier
        self.assertEqual(rows["wars?"].deciding, 1)        # rule entry text is the key; shown as "war"
        self.assertEqual(rows["sport"].events, 1)         # e, via category tag
        self.assertEqual(rows["sport"].deciding, 1)
        for r in rows.values():
            self.assertLessEqual(r.deciding, r.events)

    def test_trigger_sorts(self):
        by_events = analytics.trigger_rows(self.events, "events")
        counts = [r.events for r in by_events]
        self.assertEqual(counts, sorted(counts, reverse=True))
        # court, kill, ruling and tariff all have 2 events / 2 deciding: ties are alphabetical
        self.assertEqual([r.keyword for r in by_events[:4]], ["court", "kill", "ruling", "tariff"])
        by_level = analytics.trigger_rows(self.events, "level")
        self.assertEqual(by_level[0].level, Severity.CRITICAL)
        self.assertEqual(by_level[-1].level, Severity.MINOR)
        levels = [int(r.level) for r in by_level]
        self.assertEqual(levels, sorted(levels, reverse=True))
        by_deciding = analytics.trigger_rows(self.events, "deciding")
        self.assertEqual(by_deciding[-1].deciding, 0)

    def test_level_totals(self):
        totals = analytics.level_totals(analytics.trigger_rows(self.events))
        keywords, deciding = totals[Severity.CRITICAL]
        self.assertEqual(keywords, 3)      # earthquake, kill, war
        self.assertEqual(deciding, 4)      # a: earthquake+kill, b: kill, g: war
        self.assertEqual(totals[Severity.MINOR], (2, 1))  # football (outranked), sport (deciding)

    def test_summary(self):
        s = analytics.summary(self.events)
        self.assertEqual((s.total, s.sources), (8, 2))
        self.assertEqual((s.acknowledged, s.read, s.noted), (2, 1, 1))
        self.assertEqual(s.by_rule, 5)        # a b c f g
        self.assertEqual(s.by_category, 1)    # e
        self.assertEqual(s.by_default, 1)     # d
        self.assertEqual(s.empty, 1)          # h
        self.assertEqual(s.bumped_up, 1)      # f
        self.assertEqual(s.bumped_down, 1)    # g
        self.assertEqual(s.pct(s.by_rule), 62)
        # earthquake kill collapse football court ruling tariff war sport
        self.assertEqual(s.distinct_triggers, 9)

    def test_empty(self):
        self.assertEqual(analytics.summary([]).total, 0)
        self.assertEqual(analytics.trigger_rows([]), [])
        self.assertEqual(analytics.source_rows([]), [])
        self.assertEqual(len(analytics.severity_rows([])), 4)  # the four always-shown levels


if __name__ == "__main__":
    unittest.main()


class WeatherAnalyticsTests(unittest.TestCase):
    """Aggregations over NWS alert events (tests/fixtures/nws_sample.json: 23 alerts)."""

    NOW = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)

    def setUp(self):
        from worldterm import nws
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), filename="weather.json")
        self.store.load()
        self.alerts = nws.parse((Path(__file__).parent / "fixtures" / "nws_sample.json").read_bytes())
        nws.apply_alerts(self.store, self.alerts, now=self.NOW)
        self.events = list(self.store.events.values())
        tornado = next(a for a in self.alerts if a.event == "Tornado Warning")
        self.store.ack(tornado.id, True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_severity_rows_use_netcool_levels(self):
        rows = {r.severity: r for r in analytics.severity_rows(self.events)}
        self.assertEqual(rows[Severity.CRITICAL].total, 1)
        self.assertEqual(rows[Severity.CRITICAL].acknowledged, 1)
        self.assertEqual(rows[Severity.MAJOR].total, 8)
        self.assertEqual(rows[Severity.MINOR].total, 6)
        self.assertEqual(rows[Severity.WARNING].total, 6)
        self.assertEqual(rows[Severity.INDETERMINATE].total, 2)
        self.assertNotIn(Severity.CLEAR, rows, "no ended alerts yet")

    def test_type_rows(self):
        rows = analytics.type_rows(self.events)
        self.assertEqual(rows[0].event, "Small Craft Advisory")
        self.assertEqual(rows[0].total, 3)
        self.assertEqual(rows[0].level, Severity.WARNING)
        self.assertEqual(rows[0].states, {"Marine Lakes"})
        self.assertEqual(rows[0].offices, {"Green Bay WI", "Marquette MI"})
        self.assertEqual(len(rows), 20)
        by_level = analytics.type_rows(self.events, "level")
        self.assertEqual(by_level[0].event, "Tornado Warning")
        by_name = analytics.type_rows(self.events, "name")
        self.assertEqual([r.event for r in by_name], sorted(r.event for r in rows))

    def test_state_and_office_rows(self):
        states = {r.node: r for r in analytics.state_rows(self.events)}
        self.assertEqual(states["FL"].total, 6)
        self.assertEqual(states["FL"].by_severity[Severity.CRITICAL], 1)
        self.assertEqual(states["Marine Lakes"].total, 4)
        offices = analytics.office_rows(self.events)
        self.assertEqual(offices[0].node, "Tallahassee FL")
        self.assertEqual(offices[0].total, 3)
        self.assertEqual(len(offices), 18)

    def test_kind_counts(self):
        self.assertEqual(analytics.kind_counts(self.events), [
            ("Warning", 8), ("Watch", 2), ("Advisory", 8), ("Statement", 3), ("Outlook", 1), ("Other", 1),
        ])

    def test_issued_histogram(self):
        buckets, older = analytics.issued_histogram(self.events, now=self.NOW)
        self.assertEqual(len(buckets), 24)
        self.assertEqual(sum(buckets) + older, 23)
        self.assertEqual(older, 0, "every fixture alert was issued within the last day")
        self.assertGreater(buckets[-1] + buckets[-2], 0, "the newest alerts land in the latest buckets")
        buckets6, older6 = analytics.issued_histogram(self.events, hours=6, now=self.NOW)
        self.assertEqual(sum(buckets6) + older6, 23)
        self.assertGreater(older6, 0)

    def test_expiry_buckets(self):
        buckets = dict(analytics.expiry_buckets(self.events, now=self.NOW))
        self.assertEqual(sum(buckets.values()), 23)
        ended = sum(1 for a in self.alerts if a.active_until and a.active_until <= self.NOW)
        self.assertEqual(buckets["ended"], ended)
        self.assertGreater(buckets["≤ 24h"], 0)
        # An alert that has left the feed counts as ended whatever its timestamps say.
        self.store.clear_missing({a.id for a in self.alerts[1:]}, now=self.NOW)
        self.assertEqual(dict(analytics.expiry_buckets(self.events, now=self.NOW))["ended"], ended + 1)

    def test_urgency_certainty_and_response(self):
        urgencies, certainties, grid = analytics.urgency_certainty(self.events)
        self.assertEqual(urgencies[0], "Immediate")
        self.assertEqual(certainties[0], "Observed")
        self.assertEqual(grid[("Immediate", "Observed")], 2)
        self.assertEqual(sum(grid.values()), 23)
        self.assertIn(("Shelter", 1), analytics.response_counts(self.events))

    def test_weather_summary(self):
        s = analytics.weather_summary(self.events)
        self.assertEqual((s.total, s.active, s.ended), (23, 23, 0))
        self.assertEqual(s.offices, 18)
        self.assertEqual(s.types, 20)
        self.assertEqual(s.updates, 6)
        self.assertEqual(s.immediate, 3)
        self.assertEqual(s.observed, 3)
        self.assertEqual(s.warnings, 8)
        self.assertEqual(s.acknowledged, 1)
        self.assertEqual(s.pct(s.acknowledged), 4)


class ArrivalTests(unittest.TestCase):
    """News arrivals per hour, keyed on when worldterm first saw each item."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_arrival_histogram(self):
        now = datetime(2026, 10, 10, 16, 30, tzinfo=timezone.utc)
        self.store.merge([article("now", "Quiet day")], now=now)
        self.store.merge([article("hour-ago", "Quiet morning")], now=now - timedelta(hours=1, minutes=10))
        self.store.merge([article("yesterday", "Old news")], now=now - timedelta(hours=25))
        self.store.merge([article("future", "Clock skew")], now=now + timedelta(hours=1))
        buckets, older = analytics.arrival_histogram(self.store.events.values(), now=now)
        self.assertEqual(len(buckets), 24)
        self.assertEqual(buckets[-1], 2, "the current hour, including the item from the future")
        self.assertEqual(buckets[-2], 1)
        self.assertEqual(older, 1)
        self.assertEqual(sum(buckets) + older, 4)
        six, older6 = analytics.arrival_histogram(self.store.events.values(), hours=6, now=now)
        self.assertEqual((len(six), sum(six), older6), (6, 3, 1))
