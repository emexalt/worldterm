"""National Weather Service alerts (api.weather.gov) as Netcool-style events.

The NWS publishes every active watch, warning, advisory and statement as a CAP
alert. Each alert already carries a severity (Extreme / Severe / Moderate /
Minor / Unknown), an urgency, a certainty, the issuing office, the affected
zones and its own timing, so no keyword classifier is needed: the NWS severity
maps straight onto a Netcool level, and an alert that drops out of the active
feed becomes a CLEAR event, the way a resolution trap clears a problem.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .feed import Article, FeedError, fetch
from .severity import Severity
from .store import Event, Store

API_URL = "https://api.weather.gov/alerts/active"
STATE_FILE = "weather.json"
DEFAULT_AREA = "us"
OFF = "off"

# How long an alert that has left the active feed stays in the list as CLEAR.
CLEAR_RETENTION = timedelta(minutes=30)

SEVERITY_MAP: dict[str, Severity] = {
    "Extreme": Severity.CRITICAL,
    "Severe": Severity.MAJOR,
    "Moderate": Severity.MINOR,
    "Minor": Severity.WARNING,
    "Unknown": Severity.INDETERMINATE,
}
# Netcool level -> the NWS word it stands for, for labels on the weather pages.
NWS_LABEL: dict[Severity, str] = {sev: name for name, sev in SEVERITY_MAP.items()}
NWS_LABEL[Severity.CLEAR] = "Ended"

URGENCIES: tuple[str, ...] = ("Immediate", "Expected", "Future", "Past", "Unknown")
CERTAINTIES: tuple[str, ...] = ("Observed", "Likely", "Possible", "Unlikely", "Unknown")
RESPONSES: tuple[str, ...] = ("Shelter", "Evacuate", "Prepare", "Execute", "Avoid", "Monitor", "Assess",
                              "AllClear", "None")
KINDS: tuple[str, ...] = ("Warning", "Watch", "Advisory", "Statement", "Outlook", "Other")

# UGC zone prefixes that are marine areas rather than states.
MARINE_AREAS: dict[str, str] = {
    "AM": "Marine Atl", "AN": "Marine Atl", "GM": "Marine Gulf",
    "PZ": "Marine Pac", "PM": "Marine Pac", "PS": "Marine Pac",
    "PK": "Marine AK", "PH": "Marine HI",
    "LS": "Marine Lakes", "LM": "Marine Lakes", "LH": "Marine Lakes", "LE": "Marine Lakes",
    "LO": "Marine Lakes", "LC": "Marine Lakes", "SL": "Marine Lakes",
}

_AREA_CODE = re.compile(r"^[A-Z]{2}$")


@dataclass
class Alert:
    id: str
    event: str                 # "Flood Warning"
    nws_severity: str          # "Severe"
    urgency: str
    certainty: str
    headline: str
    description: str
    instruction: str
    area_desc: str
    sender: str                # "NWS Boston MA"
    sent: datetime | None
    effective: datetime | None
    onset: datetime | None
    expires: datetime | None
    ends: datetime | None
    status: str = "Actual"
    message_type: str = "Alert"   # Alert / Update / Cancel
    response: str = ""
    zones: tuple[str, ...] = ()           # UGC codes, e.g. MAZ014
    references: tuple[str, ...] = ()      # ids of the alerts this one supersedes
    nws_headline: str = ""
    vtec: str = ""
    link: str = ""

    # ------------------------------------------------------------------ derived
    @property
    def severity(self) -> Severity:
        if self.message_type in ("Cancel", "Expire"):
            return Severity.CLEAR
        return SEVERITY_MAP.get(self.nws_severity, Severity.INDETERMINATE)

    @property
    def office(self) -> str:
        """Issuing office without the 'NWS ' prefix: the Netcool Node."""
        name = re.sub(r"\s+", " ", self.sender).strip()
        return name[4:] if name.startswith("NWS ") else name or "NWS"

    @property
    def kind(self) -> str:
        last = self.event.rsplit(" ", 1)[-1] if self.event else ""
        return last if last in KINDS else "Other"

    @property
    def states(self) -> tuple[str, ...]:
        """Distinct states (or marine areas) covered by the alert's zones."""
        out: set[str] = set()
        for zone in self.zones:
            prefix = zone[:2].upper()
            if prefix in MARINE_AREAS:
                out.add(MARINE_AREAS[prefix])
            elif _AREA_CODE.match(prefix):
                out.add(prefix)
        return tuple(sorted(out))

    @property
    def active_until(self) -> datetime | None:
        return self.ends or self.expires

    @property
    def title(self) -> str:
        return f"{self.event}: {self.area_desc}" if self.area_desc else self.event

    def to_article(self) -> Article:
        return Article(
            guid=self.id,
            title=self.title,
            summary=self.description,
            link=self.link,
            published=self.sent,
            source=self.office,
            categories=[self.event, self.nws_severity, self.urgency, self.certainty],
        )


# --------------------------------------------------------------------------- fetch / parse

def alerts_url(area: str | None = DEFAULT_AREA) -> str | None:
    """The active-alerts URL for *area*: 'us' (everything), 'off' (None), or a
    comma-separated list of state / marine area codes such as 'MA,RI,AN'."""
    area = (area or DEFAULT_AREA).strip()
    if area.lower() == OFF:
        return None
    url = f"{API_URL}?status=actual&message_type=alert,update"
    if area.lower() in ("us", "all", ""):
        return url
    codes = [c.strip().upper() for c in area.split(",") if c.strip()]
    bad = [c for c in codes if not _AREA_CODE.match(c)]
    if bad:
        raise ValueError(f"not a state or marine area code: {', '.join(bad)}")
    return url + "&area=" + ",".join(codes)


def load(url: str, timeout: float = 15.0) -> list[Alert]:
    return parse(fetch(url, timeout=timeout))


def parse(data: bytes | str) -> list[Alert]:
    try:
        doc = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise FeedError(f"NWS response is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or "features" not in doc:
        if isinstance(doc, dict) and doc.get("detail"):
            raise FeedError(f"NWS API: {doc.get('title', '')} {doc['detail']}".strip())
        raise FeedError("NWS response has no 'features'")
    alerts: list[Alert] = []
    for feature in doc.get("features") or []:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        alert = _alert_from_properties(props, feature.get("id") or "")
        if alert is not None:
            alerts.append(alert)
    return alerts


def _alert_from_properties(p: dict, fallback_id: str) -> Alert | None:
    alert_id = p.get("id") or fallback_id
    if not alert_id:
        return None
    params = p.get("parameters") or {}
    geocode = p.get("geocode") or {}
    refs = tuple(r.get("identifier") for r in (p.get("references") or []) if isinstance(r, dict) and r.get("identifier"))
    return Alert(
        id=str(alert_id),
        event=(p.get("event") or "Alert").strip(),
        nws_severity=p.get("severity") or "Unknown",
        urgency=p.get("urgency") or "Unknown",
        certainty=p.get("certainty") or "Unknown",
        headline=_clean(p.get("headline")),
        description=_paragraphs(p.get("description")),
        instruction=_paragraphs(p.get("instruction")),
        area_desc=_clean(p.get("areaDesc")),
        sender=_clean(p.get("senderName")) or "NWS",
        sent=_when(p.get("sent")),
        effective=_when(p.get("effective")),
        onset=_when(p.get("onset")),
        expires=_when(p.get("expires")),
        ends=_when(p.get("ends")),
        status=p.get("status") or "Actual",
        message_type=p.get("messageType") or "Alert",
        response=p.get("response") or "",
        zones=tuple(str(z) for z in (geocode.get("UGC") or [])),
        references=refs,
        nws_headline=_clean(" ".join(params.get("NWSheadline") or [])),
        vtec=(params.get("VTEC") or [""])[0],
        link=p.get("@id") or f"{API_URL.rsplit('/', 1)[0]}/{alert_id}",
    )


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _paragraphs(text: str | None) -> str:
    """NWS text is hard-wrapped; keep the blank-line paragraph breaks, unwrap the rest."""
    if not text:
        return ""
    paras = [re.sub(r"\s+", " ", para).strip() for para in re.split(r"\n\s*\n", text)]
    return "\n\n".join(para for para in paras if para)


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- store integration

def apply_alerts(store: Store, alerts: list[Alert], now: datetime | None = None) -> tuple[list[Event], list[Event]]:
    """Fold a fetch of active alerts into *store*.

    - An Update that references an alert already in the list takes over that
      alert's state (ack, read, note, first seen, tally): it is the same event
      continuing, not a new one. If its severity went up, the acknowledgement
      is dropped so it demands attention again.
    - Alerts no longer in the feed become CLEAR, and are dropped once they have
      been clear for CLEAR_RETENTION.
    Returns (new events, events cleared by this call).
    """
    by_id = {a.id: a for a in alerts}
    previous: dict[str, Severity] = {}
    for alert in alerts:
        for ref in alert.references:
            if ref in store.events and ref not in by_id:
                previous[alert.id] = max(previous.get(alert.id, Severity.CLEAR), store.events[ref].severity)
                store.transfer(ref, alert.id)

    fresh = store.merge([a.to_article() for a in alerts],
                        classify=lambda art: (by_id[art.guid].severity, None), now=now)
    for alert in alerts:
        ev = store.events[alert.id]
        ev.alert = alert
        old = previous.get(alert.id)
        if old is not None and ev.severity > old and ev.acknowledged:
            ev.acknowledged = False
            ev.new = True
    cleared = store.clear_missing(set(by_id), now=now)
    store.drop_cleared(CLEAR_RETENTION, now=now)
    return fresh, cleared
