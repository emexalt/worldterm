import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worldterm import nws
from worldterm.feed import FeedError
from worldterm.severity import Severity
from worldterm.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "nws_sample.json"
NOW = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)


def load_fixture() -> list[nws.Alert]:
    return nws.parse(FIXTURE.read_bytes())


def by_event(alerts, event):
    return [a for a in alerts if a.event == event]


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.alerts = load_fixture()

    def test_parses_every_feature(self):
        self.assertEqual(len(self.alerts), 23)
        self.assertEqual(len({a.id for a in self.alerts}), 23, "ids are unique")

    def test_nws_severity_maps_to_netcool_levels(self):
        levels = {a.event: a.severity for a in self.alerts}
        self.assertEqual(levels["Tornado Warning"], Severity.CRITICAL)      # Extreme
        self.assertEqual(levels["Flood Warning"], Severity.MAJOR)           # Severe
        self.assertEqual(levels["Gale Warning"], Severity.MINOR)            # Moderate
        self.assertEqual(levels["Small Craft Advisory"], Severity.WARNING)  # Minor
        self.assertEqual(levels["Air Quality Alert"], Severity.INDETERMINATE)  # Unknown
        cancel = replace(self.alerts[0], message_type="Cancel")
        self.assertEqual(cancel.severity, Severity.CLEAR)

    def test_fields(self):
        tor = by_event(self.alerts, "Tornado Warning")[0]
        self.assertEqual(tor.urgency, "Immediate")
        self.assertEqual(tor.certainty, "Observed")
        self.assertEqual(tor.response, "Shelter")
        self.assertEqual(tor.sender, "NWS Tallahassee FL")
        self.assertEqual(tor.office, "Tallahassee FL")
        self.assertEqual(tor.kind, "Warning")
        self.assertEqual(tor.states, ("FL",))
        self.assertTrue(tor.title.startswith("Tornado Warning: Taylor, FL"))
        self.assertTrue(tor.link.startswith("https://api.weather.gov/alerts/"))
        self.assertIsNotNone(tor.sent)
        self.assertEqual(tor.sent.utcoffset(), timedelta(hours=-4))
        self.assertIn("TORNADO WARNING", tor.nws_headline)
        self.assertIn("TO.W", tor.vtec)
        self.assertIn("TAKE COVER NOW", tor.instruction)

    def test_text_is_unwrapped_but_keeps_paragraphs(self):
        ffw = by_event(self.alerts, "Flash Flood Warning")[0]
        self.assertNotIn("\n", ffw.description.replace("\n\n", ""), "single newlines are unwrapped")
        self.assertIn("\n\n", ffw.description, "paragraph breaks survive")

    def test_office_name_is_normalised(self):
        outlook = by_event(self.alerts, "Hydrologic Outlook")[0]
        self.assertEqual(outlook.sender, "NWS Pago Pago AS", "double space collapsed")
        self.assertEqual(outlook.office, "Pago Pago AS")
        self.assertEqual(outlook.kind, "Outlook")
        self.assertEqual(by_event(self.alerts, "Air Quality Alert")[0].kind, "Other")

    def test_marine_zones_become_marine_areas(self):
        gale = by_event(self.alerts, "Gale Warning")[0]
        self.assertEqual(gale.states, ("Marine Lakes",))
        tsw = by_event(self.alerts, "Tropical Storm Watch")[0]
        self.assertEqual(tsw.states, ("Marine Pac",))
        self.assertIsNone(tsw.ends)
        self.assertEqual(tsw.active_until, tsw.expires, "ends falls back to expires")
        self.assertEqual(len(tsw.references), 1)
        self.assertEqual(tsw.message_type, "Update")

    def test_article_view(self):
        art = by_event(self.alerts, "Wind Advisory")[0].to_article()
        self.assertEqual(art.source, "Pittsburgh PA")
        self.assertTrue(art.title.startswith("Wind Advisory: Monongalia"))
        self.assertIn("Wind Advisory", art.categories)
        self.assertIn("Moderate", art.categories)

    def test_bad_documents_raise_feed_error(self):
        with self.assertRaises(FeedError):
            nws.parse(b"not json")
        with self.assertRaises(FeedError):
            nws.parse(b'{"type": "Point"}')
        with self.assertRaises(FeedError) as ctx:
            nws.parse(b'{"title": "Unavailable", "detail": "upstream timeout"}')
        self.assertIn("upstream timeout", str(ctx.exception))
        self.assertEqual(nws.parse(b'{"features": []}'), [])

    def test_alerts_url(self):
        self.assertIsNone(nws.alerts_url("off"))
        self.assertIsNone(nws.alerts_url("OFF"))
        us = nws.alerts_url("us")
        self.assertTrue(us.startswith("https://api.weather.gov/alerts/active?"))
        self.assertIn("status=actual", us)
        self.assertNotIn("area=", us)
        self.assertEqual(nws.alerts_url(None), us)
        self.assertTrue(nws.alerts_url("ma, ri,an").endswith("&area=MA,RI,AN"))
        with self.assertRaises(ValueError):
            nws.alerts_url("Massachusetts")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), filename="weather.json")
        self.store.load()
        self.alerts = load_fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_apply_creates_events_with_alerts_attached(self):
        fresh, cleared = nws.apply_alerts(self.store, self.alerts, now=NOW)
        self.assertEqual(len(fresh), 23)
        self.assertEqual(cleared, [])
        for ev in self.store.events.values():
            self.assertIsNotNone(ev.alert)
            self.assertEqual(ev.severity, ev.alert.severity)
            self.assertTrue(ev.new)
            self.assertEqual(ev.tally, 1)
        self.assertEqual(self.store.events[by_event(self.alerts, "Tornado Warning")[0].id].severity,
                         Severity.CRITICAL)

    def test_missing_alerts_clear_then_age_out(self):
        nws.apply_alerts(self.store, self.alerts, now=NOW)
        keep = self.alerts[3:]
        gone = {a.id for a in self.alerts[:3]}
        fresh, cleared = nws.apply_alerts(self.store, keep, now=NOW + timedelta(minutes=5))
        self.assertEqual(fresh, [])
        self.assertEqual({ev.guid for ev in cleared}, gone)
        for guid in gone:
            ev = self.store.events[guid]
            self.assertEqual(ev.severity, Severity.CLEAR)
            self.assertIsNotNone(ev.cleared_at)
            self.assertIsNotNone(ev.alert, "the alert text stays readable while it is Clear")
        self.assertEqual(self.store.events[keep[0].id].tally, 2)
        # Still there within the retention window, gone after it.
        nws.apply_alerts(self.store, keep, now=NOW + timedelta(minutes=20))
        self.assertTrue(gone <= set(self.store.events))
        nws.apply_alerts(self.store, keep, now=NOW + nws.CLEAR_RETENTION + timedelta(minutes=6))
        self.assertFalse(gone & set(self.store.events))
        self.assertEqual(len(self.store.events), 20)

    def test_cleared_alert_that_returns_is_live_again(self):
        nws.apply_alerts(self.store, self.alerts, now=NOW)
        nws.apply_alerts(self.store, self.alerts[1:], now=NOW + timedelta(minutes=1))
        self.assertEqual(self.store.events[self.alerts[0].id].severity, Severity.CLEAR)
        nws.apply_alerts(self.store, self.alerts, now=NOW + timedelta(minutes=2))
        ev = self.store.events[self.alerts[0].id]
        self.assertEqual(ev.severity, self.alerts[0].severity)
        self.assertIsNone(ev.cleared_at)

    def test_update_takes_over_the_state_of_the_alert_it_references(self):
        nws.apply_alerts(self.store, self.alerts, now=NOW)
        old = by_event(self.alerts, "Flood Watch")[0]
        self.store.ack(old.id, True)
        self.store.mark_read(old.id)
        self.store.set_note(old.id, "notes/x.md")
        first_seen = self.store.events[old.id].first_seen

        update = replace(old, id=old.id + ".update", message_type="Update", references=(old.id,),
                         headline="Flood Watch extended")
        others = [a for a in self.alerts if a.id != old.id]
        fresh, cleared = nws.apply_alerts(self.store, others + [update], now=NOW + timedelta(hours=1))
        self.assertEqual(fresh, [], "an update is the same event continuing, not a new one")
        self.assertEqual(cleared, [], "the superseded alert is replaced, not cleared")
        self.assertNotIn(old.id, self.store.events)
        ev = self.store.events[update.id]
        self.assertTrue(ev.acknowledged and ev.read)
        self.assertEqual(ev.note, "notes/x.md")
        self.assertEqual(ev.first_seen, first_seen)
        self.assertEqual(ev.tally, 2)
        self.assertIs(ev.alert, update)

    def test_update_that_escalates_drops_the_acknowledgement(self):
        nws.apply_alerts(self.store, self.alerts, now=NOW)
        old = by_event(self.alerts, "Flood Watch")[0]  # Severe -> Major
        self.store.ack(old.id, True)
        for ev in self.store.events.values():
            ev.new = False
        update = replace(old, id=old.id + ".u", message_type="Update", references=(old.id,),
                         event="Flood Warning", nws_severity="Extreme")
        others = [a for a in self.alerts if a.id != old.id]
        nws.apply_alerts(self.store, others + [update], now=NOW + timedelta(hours=1))
        ev = self.store.events[update.id]
        self.assertEqual(ev.severity, Severity.CRITICAL)
        self.assertFalse(ev.acknowledged, "a higher severity needs a fresh acknowledgement")
        self.assertTrue(ev.new)

    def test_state_survives_a_restart(self):
        nws.apply_alerts(self.store, self.alerts, now=NOW)
        guid = self.alerts[5].id
        self.store.ack(guid, True)
        self.store.save()
        self.assertTrue((Path(self.tmp.name) / "weather.json").exists())
        self.assertFalse((Path(self.tmp.name) / "state.json").exists(), "weather has its own file")

        store2 = Store(Path(self.tmp.name), filename="weather.json")
        store2.load()
        nws.apply_alerts(store2, self.alerts, now=NOW + timedelta(minutes=10))
        self.assertTrue(store2.events[guid].acknowledged)
        self.assertEqual(store2.events[guid].tally, 2)
        self.assertFalse(store2.events[guid].new)


if __name__ == "__main__":
    unittest.main()
