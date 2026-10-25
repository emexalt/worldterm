"""Persistent per-article event state (ack/read/note/tally/first/last)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .feed import Article
from .severity import MatchDetails, Severity, match_details

if TYPE_CHECKING:  # pragma: no cover
    from .nws import Alert

STATE_FILE = "state.json"
NOTES_DIR = "notes"
PRUNE_AFTER = timedelta(days=7)

# (Article) -> (severity, classifier details or None)
Classifier = Callable[[Article], "tuple[Severity, MatchDetails | None]"]


def classify_article(art: Article) -> tuple[Severity, MatchDetails | None]:
    details = match_details(art.title, art.summary, art.categories)
    return details.severity, details


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "worldterm"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _from_iso(value: str | None) -> datetime:
    if not value:
        return _now()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return _now()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Event:
    """An Article plus its monitoring state. Netcool: one row in the event list."""

    article: Article
    severity: Severity
    first_seen: datetime
    last_seen: datetime
    tally: int = 1
    acknowledged: bool = False
    read: bool = False
    note: str | None = None          # path relative to data_dir
    new: bool = False                # first time seen this session
    match: MatchDetails | None = None  # classifier result; recomputed on load, not persisted
    alert: "Alert | None" = None     # the NWS alert behind a weather event
    cleared_at: datetime | None = None  # when the event left its feed and became CLEAR

    @property
    def guid(self) -> str:
        return self.article.guid

    @property
    def summary(self) -> str:
        return self.article.title

    @property
    def node(self) -> str:
        return self.article.source


class Store:
    def __init__(self, data_dir: Path | str | None = None, filename: str = STATE_FILE) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.state_path = self.data_dir / filename
        self.notes_dir = self.data_dir / NOTES_DIR
        self._records: dict[str, dict] = {}
        self.events: dict[str, Event] = {}

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        self._records = data if isinstance(data, dict) else {}

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for ev in self.events.values():
            self._records[ev.guid] = self._record_from_event(ev)
        self._prune()
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._records, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _prune(self) -> None:
        cutoff = _now() - PRUNE_AFTER
        for guid in list(self._records):
            if guid in self.events:
                continue
            if _from_iso(self._records[guid].get("last_seen")) < cutoff:
                del self._records[guid]

    @staticmethod
    def _record_from_event(ev: Event) -> dict:
        return {
            "acknowledged": ev.acknowledged,
            "read": ev.read,
            "note": ev.note,
            "first_seen": _iso(ev.first_seen),
            "last_seen": _iso(ev.last_seen),
            "tally": ev.tally,
            "title": ev.article.title,
            "link": ev.article.link,
            "source": ev.article.source,
        }

    # ------------------------------------------------------------------ merging
    def merge(self, articles: list[Article], classify: Classifier = classify_article,
              now: datetime | None = None) -> list[Event]:
        """Fold a fresh batch of articles into the event table.

        Existing events: tally += 1, last_seen = now, article and severity refreshed.
        Unknown guids: new Event (restoring ack/read/note from disk if seen before).
        Events no longer in the feed are kept (they age out via prune on save).
        Returns the list of events that are new this call.
        """
        now = now or _now()
        fresh: list[Event] = []
        for art in articles:
            sev, details = classify(art)
            ev = self.events.get(art.guid)
            if ev is not None:
                ev.article = art
                ev.severity = sev
                ev.match = details
                ev.cleared_at = None
                ev.tally += 1
                ev.last_seen = now
                continue
            rec = self._records.get(art.guid)
            if rec:
                ev = Event(
                    article=art,
                    severity=sev,
                    first_seen=_from_iso(rec.get("first_seen")),
                    last_seen=now,
                    tally=int(rec.get("tally", 0)) + 1,
                    acknowledged=bool(rec.get("acknowledged", False)),
                    read=bool(rec.get("read", False)),
                    note=rec.get("note"),
                    new=False,
                    match=details,
                )
            else:
                ev = Event(article=art, severity=sev, first_seen=now, last_seen=now, new=True, match=details)
                fresh.append(ev)
            self.events[art.guid] = ev
        return fresh

    def transfer(self, old_guid: str, new_guid: str) -> None:
        """Hand the state of *old_guid* (ack, read, note, first seen, tally) to
        *new_guid*, which the next merge will restore it for. The old event goes away."""
        ev = self.events.pop(old_guid, None)
        rec = self._record_from_event(ev) if ev is not None else self._records.get(old_guid)
        self._records.pop(old_guid, None)
        if rec is not None and new_guid not in self.events:
            self._records[new_guid] = dict(rec)

    def clear_missing(self, present: set[str], now: datetime | None = None) -> list[Event]:
        """Events not in *present* become CLEAR (Netcool: the problem went away).
        Returns the events cleared by this call."""
        now = now or _now()
        cleared: list[Event] = []
        for ev in self.events.values():
            if ev.guid in present or ev.severity == Severity.CLEAR:
                continue
            ev.severity = Severity.CLEAR
            ev.cleared_at = now
            cleared.append(ev)
        return cleared

    def drop_cleared(self, retention: timedelta, now: datetime | None = None) -> int:
        """Forget CLEAR events that have been clear for longer than *retention*."""
        now = now or _now()
        gone = [g for g, ev in self.events.items()
                if ev.severity == Severity.CLEAR and ev.cleared_at is not None and now - ev.cleared_at > retention]
        for guid in gone:
            del self.events[guid]
            self._records.pop(guid, None)
        return len(gone)

    # ------------------------------------------------------------------ mutations
    def ack(self, guid: str, value: bool = True) -> None:
        self.events[guid].acknowledged = value

    def toggle_ack(self, guid: str) -> bool:
        ev = self.events[guid]
        ev.acknowledged = not ev.acknowledged
        return ev.acknowledged

    def mark_read(self, guid: str) -> None:
        self.events[guid].read = True

    def set_note(self, guid: str, rel_path: str | None) -> None:
        self.events[guid].note = rel_path

    # ------------------------------------------------------------------ notes
    def note_path(self, ev: Event) -> Path | None:
        return (self.data_dir / ev.note) if ev.note else None

    def write_note(self, ev: Event, filename: str, text: str) -> Path:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        path = self.notes_dir / filename
        path.write_text(text, encoding="utf-8")
        ev.note = str(Path(NOTES_DIR) / filename)
        return path

    def read_note(self, ev: Event) -> str | None:
        path = self.note_path(ev)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
