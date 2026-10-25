import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worldterm.feed import Article
from worldterm.severity import Severity
from worldterm.store import Store


def art(guid, title="Earthquake kills dozens", link="https://x/1"):
    return Article(guid, title, "summary", link, datetime.now(timezone.utc), "Src")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir)
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_new_and_tally(self):
        fresh = self.store.merge([art("a"), art("b")])
        self.assertEqual({e.guid for e in fresh}, {"a", "b"})
        self.assertTrue(all(e.new for e in fresh))
        self.assertEqual(self.store.events["a"].severity, Severity.CRITICAL)
        fresh2 = self.store.merge([art("a")])
        self.assertEqual(fresh2, [])
        self.assertEqual(self.store.events["a"].tally, 2)
        self.assertEqual(self.store.events["b"].tally, 1)
        self.assertIn("b", self.store.events)  # kept even if absent from feed

    def test_round_trip_state(self):
        self.store.merge([art("a")])
        self.store.toggle_ack("a")
        self.store.mark_read("a")
        self.store.save()
        self.assertTrue((self.dir / "state.json").exists())

        s2 = Store(self.dir)
        s2.load()
        fresh = s2.merge([art("a")])
        self.assertEqual(fresh, [])
        ev = s2.events["a"]
        self.assertTrue(ev.acknowledged)
        self.assertTrue(ev.read)
        self.assertEqual(ev.tally, 2)
        self.assertFalse(ev.new)

    def test_prune_old_absent_records(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        (self.dir / "state.json").write_text(json.dumps({
            "old": {"last_seen": old, "tally": 1},
            "recent": {"last_seen": recent, "tally": 1},
        }))
        s = Store(self.dir)
        s.load()
        s.merge([art("z")])
        s.save()
        data = json.loads((self.dir / "state.json").read_text())
        self.assertNotIn("old", data)
        self.assertIn("recent", data)
        self.assertIn("z", data)

    def test_corrupt_state_file_ignored(self):
        (self.dir / "state.json").write_text("{not json")
        s = Store(self.dir)
        s.load()
        self.assertEqual(s.merge([art("a")])[0].guid, "a")

    def test_notes(self):
        self.store.merge([art("a")])
        ev = self.store.events["a"]
        self.assertIsNone(self.store.read_note(ev))
        path = self.store.write_note(ev, "20261010-x.md", "# x\nhello")
        self.assertTrue(path.exists())
        self.assertEqual(ev.note, "notes/20261010-x.md")
        self.assertEqual(self.store.read_note(ev), "# x\nhello")
        self.store.save()
        s2 = Store(self.dir)
        s2.load()
        s2.merge([art("a")])
        self.assertEqual(s2.read_note(s2.events["a"]), "# x\nhello")


if __name__ == "__main__":
    unittest.main()
