# worldterm design

This document is for the people who will maintain and extend worldterm. It explains how
the program is built and why: its parts, the data that moves between them, the rules each
part keeps, and where it is fragile. It describes the code as of 2026-10-29, running on
Python 3.14.4 with Textual 2.1.2 and Rich 13.9.4.

Read the [README](../README.md) first for what the program does from a user's point of
view. Section 3 is a one-page map of the code. The other sections stand alone. Section 16
lists known defects, several of which are worth fixing before anything else.

## Contents

1. [What worldterm is](#1-what-worldterm-is)
2. [Principles](#2-principles)
3. [Architecture at a glance](#3-architecture-at-a-glance)
4. [Runtime behavior](#4-runtime-behavior)
5. [Data model](#5-data-model)
6. [News ingest](#6-news-ingest)
7. [News severity](#7-news-severity)
8. [Weather alerts](#8-weather-alerts)
9. [The event store](#9-the-event-store)
10. [The event lists](#10-the-event-lists)
11. [The dashboard](#11-the-dashboard)
12. [Configuration and files](#12-configuration-and-files)
13. [Failure handling](#13-failure-handling)
14. [Testing](#14-testing)
15. [Making common changes](#15-making-common-changes)
16. [Known issues and technical debt](#16-known-issues-and-technical-debt)
17. [Design decisions](#17-design-decisions)
18. [Glossary](#18-glossary)

---

## 1. What worldterm is

worldterm shows news headlines and US weather alerts the way IBM Netcool/OMNIbus shows
network faults. Every item is an *event*: one row in a dense, color-coded list, with a
severity, the *node* it came from, a one-line summary, a *tally* of how often it has been
seen, and the times it was first and last seen. The operator *acknowledges* events, reads
their details and attaches *journal* notes.

The metaphor is the product. Most design choices follow from keeping Netcool's meaning
consistent across both kinds of event:

| Netcool/OMNIbus | News list | Weather list |
|---|---|---|
| Event, deduplicated into one row | An article, identified by its feed GUID | An NWS alert, identified by its CAP id and carried forward through updates |
| Node | Short source label such as `BBC`, or the feed's own title | The issuing forecast office, such as `Tallahassee FL` |
| Summary | The headline | `<event type>: <areas>` |
| Severity | Keyword rules over headline and summary (section 7) | The alert's NWS severity, mapped (section 8) |
| Tally | Refreshes in which the article was in its feed | The same, for an alert and its updates |
| First and last occurrence | `First` and `Last` columns | Tracked, but the list shows `Issued` and `Expires` |
| Acknowledge | `A` flag; the row turns a darker shade | Same |
| Journal | A Markdown note file | Same |
| Clear | Not used: an article never resolves | The alert has left the active feed |
| Severity lamps | Per-level counters in the header | Same, plus `CLR` |

Users spend their time in three screens: the news list, the weather list, and a dashboard
with a page for each list. Everything else (the reader, the note editor, the context menu
and help) opens over a list and returns to it.

Two optional displays show the same events outside the terminal, and neither can change
them: a CYD desk display on USB that cycles today's unacknowledged Critical headlines
(section 4.4), and a wall page in a browser that rotates the unacknowledged Critical and
Major headlines beside summary statistics (section 4.5). Acknowledging happens in the
terminal only.

There is no database, and the only server is the optional read-only wall page (section
4.5). Each running copy keeps its state in JSON files in the user's data directory.

## 2. Principles

The code keeps the following rules. A change that breaks one should do so on purpose,
and say so.

1. **The data layer is plain Python.** Modules outside `worldterm/ui/` use only the
   standard library and never import Textual, Rich or `worldterm.ui`. Parsing,
   classification, state and aggregation are ordinary functions over ordinary objects,
   tested without a terminal and reusable by another front end. Even the CYD display's
   serial port is driven with `termios` rather than a library (D14), and its Bluetooth LE
   link with a plain socket (D17).
2. **Textual is the only third-party dependency**, and only the UI uses it. Rich comes
   with Textual.
3. **Both lists behave alike.** They share one screen class, one row-styling rule, one
   store class and one set of chart helpers. A feature added to one list belongs in the
   other unless its data makes no sense there.
4. **Color is never the only signal.** Every colored element also carries text: the
   `Sev` column, chip labels, counts and legends.
5. **Fail soft.** A dead feed costs a notification, not the other feeds or the session.
   A failed save keeps the in-memory state. State files are replaced atomically.
6. **Keep it injectable.** Feed URLs accept `file://`, the data directory is a parameter,
   functions that depend on the clock take `now`, and the article loader is a parameter of
   the app. The tests depend on all four.
7. **The UI thread owns all state.** Worker threads download and parse. Every change to a
   store or a widget happens on Textual's event loop (section 4.2). The CYD worker and the
   wall server never see a store: the UI thread hands them snapshots (sections 4.4, 4.5).
8. **Text from outside is never markup.** Headlines, summaries, article and alert text
   reach widgets as `rich.text.Text`, not as plain strings, because Textual parses plain
   strings as markup (section 10.10). Two labels in the note editor still break this rule;
   see section 16.

## 3. Architecture at a glance

The news and weather pipelines run side by side and meet at the same abstractions: an
`Event` in a `Store`, shown by an `EventListScreen` and summarized by `analytics`.

```
             NEWS                                        WEATHER
  sources.py: which feeds                      nws.alerts_url(): which area
            │                                              │
  feed.fetch + feed.parse                      feed.fetch + nws.parse
            │  list[Article]                               │  list[Alert]
            ▼                                              ▼
  Store.merge(articles)                        nws.apply_alerts(store, alerts)
    severity from severity.match_details         severity from the NWS table;
            │                                    continue updates, merge, clear, forget
            ▼                                              ▼
  app.store.events                             app.weather_store.events
            │          ┌──── analytics.py ────┐            │
            ├─────────►│  pure aggregations   │◄───────────┤
            │          └──────────┬───────────┘            │
            ▼                     ▼                        ▼
       MainScreen          DashboardScreen           WeatherScreen
    (EventListScreen)     news / weather page      (EventListScreen)
            │                                              │
            ▼                                              ▼
       state.json          notes/*.md (shared)        weather.json
```

Both stores also feed the two optional displays. `WorldTerm.save()`, which every change of
state passes through, builds a snapshot for each and hands it to a thread that owns the
device:

```
  app.store + app.weather_store
            │  WorldTerm.save()
            ├─► cyd.select_critical ──► CydService (thread) ──► USB serial ──► CYD board
            └─► wall.build_snapshot ──► WallServer (thread) ──► HTTP GET ──► browsers
```

The reader is the one place that fetches on demand: opening a news event downloads the
article's page on a thread of its own and shows the text `article.extract` finds in it
(sections 4.6 and 6.4).

| Module | Responsibility | Imports from worldterm |
|---|---|---|
| `main.py` | Command line, preset resolution, option checks, terminal check, starts the app | `article`, `cyd`, `nws`, `sources`, `store`, `wall`, `ui.app` |
| `worldterm/sources.py` | Catalog of named feeds and preset groups | nothing |
| `worldterm/feed.py` | HTTP and `file://` fetch, RSS and Atom parsing; `Article`, `FeedError` | nothing |
| `worldterm/article.py` | Article text: fetch a page, decode it, and find the body in it (`extract`); `Body`, `Block` | `feed` |
| `worldterm/severity.py` | `Severity` levels and the news keyword classifier | nothing |
| `worldterm/slug.py` | Titles to file-name slugs, for notes | nothing |
| `worldterm/store.py` | `Event` and `Store`: merging, tally, flags, notes, JSON persistence | `feed`, `severity` |
| `worldterm/nws.py` | NWS alert parsing, `Alert`, the severity table, the alert lifecycle | `feed`, `severity`, `store` |
| `worldterm/analytics.py` | Pure aggregations behind the dashboard, for news and weather | `nws`, `severity`, `store` |
| `worldterm/cyd.py` | The CYD display: `Headline` snapshots, frames, the line protocol, and `CydService`, which finds the board, keeps it current and copes with it leaving | `severity`, `store`, `serialport` and `bleport` (lazily) |
| `worldterm/serialport.py` | USB serial ports with the standard library: discovery by USB id through `/sys`, raw 8N1 I/O. Linux only | nothing |
| `worldterm/bleport.py` | The CYD over Bluetooth LE: an ATT client on an L2CAP socket that uses the board's Nordic UART Service as a byte stream (`BlePort`), and the send-text program (`python3 -m worldterm.bleport`). Linux, Python 3.14+ | nothing |
| `worldterm/palette.py` | The severity colours as hex values, shared by the terminal UI and the wall page | `severity` |
| `worldterm/wall.py` | The wall page: `build_snapshot` (the JSON the page shows) and `WallServer`, a read-only `http.server` on a daemon thread. `wall.html` is the page itself | `analytics`, `cyd`, `nws`, `palette`, `severity`, `store` |
| `ui/app.py` | `WorldTerm` app, fetch and CYD workers, article downloads and their cache, the wall server's start and stop, `MainScreen`, `WeatherScreen` | most of the above |
| `ui/listscreen.py` | `EventListScreen`, the Netcool list; `HelpScreen`; `note_template` | `severity`, `slug`, `store`, `ui.*` |
| `ui/table.py` | `EventTable`: a DataTable with full-row background colors | nothing |
| `ui/styles.py` | Row styles, lamps, time and sort helpers; re-exports the palette | `palette`, `severity`, `store` |
| `ui/menu.py` | `ContextMenu`, a modal menu placed at the pointer | nothing |
| `ui/reader.py` | `ReaderScreen`: details of an article or alert, and the article's text | `article`, `severity`, `store`, `ui.styles` |
| `ui/editor.py` | `NoteEditorScreen`, `ConfirmDiscard`, hand-off to `$EDITOR` | nothing |
| `ui/dashboard.py` | `DashboardScreen`: layout, pages, refresh; the news trigger panels | `analytics`, `severity`, `ui.*` |
| `ui/charts.py` | Generic Rich renderables: `StackedBar`, chips, lamp tables, sparklines | `analytics`, `severity`, `ui.styles` |
| `ui/weatherpanels.py` | Renderables for the dashboard's weather page | `analytics`, `nws`, `severity`, `ui.*` |

Dependency rules to keep true:

- Nothing outside `worldterm/ui/` imports Textual, Rich or `worldterm.ui`.
- `ui/table.py`, `ui/menu.py` and `ui/editor.py` import nothing from worldterm. They are
  generic widgets.
- `ui/listscreen.py` refers to `ui/app.py`, and `store.py` to `nws.py`, only under
  `TYPE_CHECKING`, which avoids import cycles.
- `serialport.py` and `bleport.py` are the Linux-specific modules. `cyd.py` imports them
  only inside `service_for` and `unavailable_reason`, so everything else imports on any
  platform.

A good reading order for newcomers is `store.py` (the `Event` and `Store.merge`), then
`ui/listscreen.py` (how an event becomes a row), then `ui/app.py` (where events come
from), then `nws.py` and `analytics.py`.

```
main.py                 command-line entry point
pyproject.toml
README.md               user guide
docs/DESIGN.md          this document
worldterm/              data layer, standard library only
  sources.py  feed.py  article.py  severity.py  slug.py  store.py  nws.py  analytics.py
  cyd.py  serialport.py  bleport.py  palette.py  wall.py  wall.html
  ui/                   Textual front end
    app.py  listscreen.py  table.py  styles.py  menu.py  reader.py  editor.py
    dashboard.py  charts.py  weatherpanels.py
tests/                  unittest suite
  fixtures/             captured feeds and alerts
    articles/           small pages copying the structure of real news sites
cyd/                    the board side of the CYD display (MicroPython), with its own README
```

## 4. Runtime behavior

### 4.1 Startup

1. `main.main()` parses the command line. `--feed` values become `Source` objects through
   `sources.parse_feed_arg`. Without them, `--preset` is expanded by `sources.resolve`.
   `nws.alerts_url` validates `--weather`, `wall.parse_wall` validates `--wall`, and a
   `--cyd` port is refused on a host without `termios`. If stdin or stdout is not a
   terminal, the program prints a message and exits with status 2.
2. `WorldTerm.__init__` wraps plain URLs as unlabeled sources, raises the interval to at
   least 15 seconds, creates the news and weather stores, and works out the alerts URL.
   `--weather-url` takes precedence over `--weather`. The value `off` produces `None`,
   which disables the weather fetch, the WX lamp and the weather list.
3. `WorldTerm.on_mount` loads both stores' records from disk and pushes `MainScreen`.
4. `MainScreen.on_mount` runs the shared `EventListScreen.on_mount`, which builds the
   columns, starts a one-second header timer and focuses the empty table. It then starts
   the refresh timer and the first fetch.
5. Unless `--cyd` is `off`, `WorldTerm.on_mount` also starts the display worker
   (section 4.4), which looks for the board on its own from then on.
6. Unless `--wall` is `off`, it then binds and starts the wall page's server (section 4.5).
   A port that cannot be taken costs an error toast; everything else carries on.

The first fetch starts from the screen's `on_mount` rather than the app's because, in an
early version, results arrived before the screen existed and crashed the app.

Records loaded from disk are not events. An item becomes an event only when a fetch
contains it, and `Store.merge` then restores its flags from the record. The list stays
empty until the first feed arrives.

### 4.2 A refresh

```
 UI thread (Textual event loop)                   worker threads
 ──────────────────────────────                   ──────────────
 refresh timer, or R
   └─ WorldTerm.start_fetch()
        ├─ fetching = True ──────────────────────► fetch_feeds    (group "fetch")
        └─ weather_fetching = True ──────────────► fetch_alerts   (group "weather")

                                                  fetch_feeds: a pool of up to 8
                                                  threads runs feed.load(url) for
                                                  every source
 _apply_articles(url, articles)  ◄──────────────  as each source finishes
   Store.merge, rebuild the news list,            (or _feed_error on failure)
   refresh the dashboard, save
 _fetch_done()                   ◄──────────────  after the last source
   clear the flag, notify, redraw the headers

                                                  fetch_alerts: nws.load(url)
 _apply_alerts(alerts)           ◄──────────────
   nws.apply_alerts, rebuild the weather list,
   redraw the WX lamp, refresh the dashboard, save
 _weather_done()                 ◄──────────────
```

Every arrow pointing left is a `call_from_thread`. These rules make it safe:

- Workers only download and parse. Stores and widgets change only inside the `_apply_*`
  and `_*_done` methods, which run on the UI thread. `Store` and `Event` are not
  thread-safe and, under this rule, do not need to be.
- `call_from_thread` waits for the UI thread to run the callback. Feeds are therefore
  merged one at a time, in the order they finish, while the other downloads continue in
  the pool.
- `start_fetch` skips a pipeline whose busy flag is still set, so a slow refresh never
  stacks up. News and weather have separate flags and worker groups, so neither waits for
  the other.
- The refresh timer belongs to `MainScreen`, which is never removed. Both headers count
  down to `next_refresh_at`, which is reset when the news fetch completes. Section 16
  notes a drift in this countdown.
- Items that arrive during the first fetch of each pipeline have `new` cleared, so the
  opening screen is not entirely bold. Later arrivals stay bold until acknowledged or
  read, and are counted in the "N new events" notification.

### 4.3 How screens return results

Pushed screens hand results back through Textual's `push_screen(screen, callback)` and
`dismiss(value)`. The context menu returns the chosen item's key. The reader returns
`"note"`, `"ack"`, `"open"` or `"back"`. The note editor returns the text, or `None` when
canceled. The list screen that pushed them acts on the result.

None of the pushed screens changes any state itself. The list marks an event read before
it opens the reader, and writes the note file after the editor closes. Every change of
state is followed by `WorldTerm.save()`. The reader asks the app for the article's text
(section 4.6), which changes no event.

### 4.4 The CYD display

A third worker, `run_cyd` (group `cyd`), runs `CydService.run()` for the whole session. It
is the only thread that touches the board's link, and the UI thread never waits for it.

```
 UI thread (Textual event loop)                    display worker
 ──────────────────────────────                    ──────────────
 WorldTerm.save()
   └─ _sync_cyd() ──► CydService.update(snapshot)
                                                   tick(), forever: look for a board,
                                                   handshake, send a frame when it
                                                   changes, PING, notice it leaving
 _cyd_connected / _cyd_disconnected  ◄──────────── callbacks, via call_from_thread
```

- The snapshot is a tuple of frozen `Headline` records that `cyd.select_critical` builds on
  the UI thread from both stores: Critical, unacknowledged, not cleared. `update()` only
  stores it; the service applies the "today" rule itself on every step, so the display
  empties at midnight without any UI event.
- `save()` is where the snapshot is pushed, because every change of state already passes
  through it. It runs many times per refresh, which is fine: an unchanged snapshot produces
  an unchanged frame, and an unchanged frame is not sent.
- Every blocking call in the service is bounded: a read waits at most 0.2 s, an idle wait
  0.25 s. This keeps `q` instant. Python's `asyncio.run` joins the executor's threads when
  the app ends, and Textual only flags a thread worker as cancelled; a thread that did not
  return would hold the exit, and every UI test, for minutes.
- `action_quit` calls `CydService.stop()` before `exit()`. The service's `finally` writes a
  "worldterm stopped" frame and closes the port with RTS dropped before DTR, the order that
  leaves an ESP32 running.
- Callbacks reach the UI through `_from_thread`, which swallows the `RuntimeError` that
  `call_from_thread` raises once the app has stopped. Article downloads use it too.

**Two transports, one protocol.** The board takes the same lines over USB serial and over
Bluetooth LE (`--cyd ble:ADDRESS`), so the service does not know which it is using: it
reads and writes a `Port`, and `service_for` picks the port and four settings.

| Setting | USB (`serialport.SerialPort`) | Bluetooth LE (`bleport.BlePort`) |
|---|---|---|
| Finding the board | every port with an ESP32 bridge's USB id, every 2 s | the one address given, every 10 s (`BLE_RESCAN_SECONDS`): an attempt keeps the radio busy |
| Waiting for `HELLO` after opening | 3 s, because opening the port may reset the board | none: a BLE connection never resets it, so the service pings at once |
| Pacing writes | 64-byte chunks, 20 ms apart, for the board's small serial buffer | none from the service: `BlePort` cuts writes to the link's MTU (247 bytes) and waits for each to be acknowledged |
| Opening | immediate | a non-blocking connect that checks the service's stop flag every 0.2 s, so `q` never waits for a board that is out of range |

`BlePort` speaks ATT, the protocol under GATT, directly on Linux's L2CAP socket for the ATT
channel (CID 4). After connecting it raises the MTU, finds the Nordic UART Service's RX and
TX characteristics by reading the characteristic declarations, and subscribes to TX. It
then writes with Write Requests, each acknowledged before the next, and keeps the payloads
of TX notifications as the bytes `read()` returns. Requests the board might send it are
answered with an error (or the MTU), so the link never times out on them. Every wait is
bounded by 0.2 s slices, as the serial port's are.

### 4.5 The wall page

`WallServer` (`wall.py`) is a `ThreadingHTTPServer` on a daemon thread. It serves two things,
the bundled `wall.html` and the latest JSON snapshot, and answers nothing but `GET` and
`HEAD`: every other method gets `405 Allow: GET, HEAD`, every other path `404`. There is no
endpoint that changes anything, which is what makes the page read-only.

```
 UI thread                                              server thread           browsers
 save(), _fetch_done, _weather_done, 60 s timer
   └─ _sync_wall() ─► wall.build_snapshot(...)
                      ─► WallServer.update(JSON bytes, under a lock)
                                                         serve_forever: GET /  ◄── page load
                                                                GET /api/state ◄── every 5 s
```

- The snapshot is built on the UI thread, which owns the stores (D12), with the pure
  functions in `analytics` and `cyd.select_headlines`. The server thread only ever copies
  bytes. `build_snapshot` is a pure function of the events and a clock, and is tested as one.
- The 60 s timer exists because the 24-hour arrivals window moves with the clock even when
  nothing is saved. Everything else that changes with time the page works out by itself,
  from the browser's clock and the absolute UTC times in the snapshot: the "19 min ago"
  labels and the countdown to the next refresh. The page also rotates the headlines itself, by key, so a poll never
  restarts the cycle.
- HTTP/1.0, the stdlib default, on purpose: every response closes its connection, so no
  handler thread outlives a request. `serve_forever` polls every 0.2 s, `shutdown()` is only
  called once the loop has started (it would wait forever otherwise), and the thread is a
  daemon; `q` stays instant.
- Feed text reaches the page as JSON and is written with `textContent`, never `innerHTML`,
  and the page's Content-Security-Policy allows nothing but itself and `/api/state`.
- The JSON carries titles, sources, times and counts, never notes or links.

### 4.6 Reading an article

Feeds carry a summary, not the article, so the reader fetches the page when it opens.
Section 6.4 describes how the text is found in it.

```
 UI thread                                              download thread "article"
 ReaderScreen.on_mount
   └─ WorldTerm.request_article(link, callback)
        cached?  yes ─► callback(link, Body) at once
        no: the first to ask starts ─────────────────► article.load(link):
                                                         fetch, decode, extract
 _article_done(link, Body or reason)  ◄─ _from_thread ──┘
   cache a Body (50 kept, the least recently read dropped first)
   └─ every waiting callback ─► ReaderScreen._article_ready
                                  closed, or the app quit? ignore it
                                  Body: replace the summary; "1,036 words from ..."
                                  reason: keep the summary; "unavailable (...)"
```

- There is one download per link. A reader opened again while its page is still loading
  joins the waiting callbacks rather than starting a second thread.
- Failures are not cached, so opening the reader again tries again. Nothing is written to
  disk.
- The download runs on a daemon thread, not a Textual worker. Textual runs thread workers
  in the event loop's default executor, which `asyncio.run` joins when the app ends
  (section 4.4), and a blocked `urlopen` cannot notice a cancel flag, so a page still
  loading would hold `q` for up to `--timeout`. A daemon thread is simply left behind. Its
  late report goes through `_from_thread`, which drops it once the app has stopped, and the
  reader checks `is_attached` before it touches a widget.
- The thread only downloads and parses. The cache, and every widget, change on the UI
  thread (D12).
- `WorldTerm(article_loader=None)`, the constructor's default, never fetches: the reader
  then shows the summary and no Article line. `main.py` passes
  `partial(article.load, timeout=--timeout)`. Only news is fetched; an alert's reader
  already shows the whole alert.

## 5. Data model

### 5.1 Severity

`severity.Severity` is an `IntEnum` with Netcool's numbering, so levels compare and sort
directly.

| Value | Level | Short | In the news list | In the weather list |
|---|---|---|---|---|
| 5 | Critical | `CRT` | A Critical keyword matched | NWS *Extreme* |
| 4 | Major | `MAJ` | A Major keyword matched | NWS *Severe* |
| 3 | Minor | `MIN` | A Minor keyword matched | NWS *Moderate* |
| 2 | Warning | `WRN` | No rule matched, the default | NWS *Minor* |
| 1 | Indeterminate | `IND` | The item had no text | NWS *Unknown* |
| 0 | Clear | `CLR` | Not used | The alert left the feed, or was canceled |

A news Warning means "no rule matched", not "a little important".

### 5.2 Article

`feed.Article` is the normalized feed item and the common currency between ingest and
the store: `guid`, `title`, `summary` (plain text), `link`, `published` (a timezone-aware
datetime, or `None`), `source` (which becomes the node) and `categories`.

### 5.3 Alert

`nws.Alert` holds the parts of a CAP alert that worldterm uses: the id, event name, NWS
severity, urgency, certainty, response, headline, description, instruction, area
description, sender, the five timestamps (sent, effective, onset, expires, ends), message
type, UGC zone codes, references, the NWS headline and the VTEC string. Its derived
properties are described in section 8.3. `Alert.to_article()` produces the `Article` the
store keeps.

### 5.4 Event

`store.Event` is an article plus its monitoring state: one row.

| Field | Persisted | Notes |
|---|---|---|
| `article` | in part | Replaced on every merge. Title, link and source are saved for people reading the file |
| `severity` | no | Recomputed on every merge |
| `first_seen`, `last_seen` | yes | Timezone-aware, stored in UTC |
| `tally` | yes | Incremented by every merge that contains the item |
| `acknowledged`, `read` | yes | |
| `note` | yes | Path relative to the data directory, such as `notes/20261010-slug.md` |
| `new` | no | Arrived this session after the first load; the row is bold |
| `match` | no | The news classifier's `MatchDetails`; `None` for alerts |
| `alert` | no | The `Alert` behind a weather event; `None` for news |
| `cleared_at` | no | When the event became Clear |

`guid`, `summary` (the title) and `node` (the source) are read-only shortcuts.
Throughout the code, an event whose `alert` is set is a weather event: `analytics`, the
reader and the note template all branch on it.

### 5.5 MatchDetails and Hit

`severity.MatchDetails` is a frozen record of what the classifier decided. It is kept on
`Event.match`, so the dashboard can explain severities without classifying again.

- `severity` is the final level, and `base` the level before modifiers.
- `origin` says where the base came from: `text`, `category`, `default` or `empty`.
- `hits` are the distinct keywords found, as `Hit(keyword, level)`.
- `prefix` is `up`, `down` or `None`, and `modifier` is its actual effect after clamping:
  `+1`, `-1` or `0`.
- `deciding` lists the hits whose level set the base severity. The other hits were
  *outranked*.

### 5.6 Body and Block

`article.Body` is what the reader shows in place of the summary. `blocks` is a tuple of
`Block(kind, text)`, where `kind` is `p` (a paragraph), `h` (a subheading), `li` (a list
item) or `quote`. `url` is the page it came from, and `method` says where the text was
found: `html`, or `jsonld` (section 6.4). `words` and `host` are derived. The text is
plain, never markup, and a `<br>` survives as a line break. Bodies live only in the app's
memory cache (section 4.6) and are never saved.

## 6. News ingest

### 6.1 Sources and presets

`sources.SOURCES` maps a key to a `Source(key, label, url, description)`. The label is the
node name and must fit the 12-character Node column. `tests/test_sources.py` checks that,
and also that every URL uses HTTPS, every group names real sources, and no group shadows a
source key. `GROUPS` maps preset names to source keys, and `resolve("europe,npr")` expands
and de-duplicates them in order. A `--feed` URL without a `LABEL=` prefix keeps the feed's
own title as its node.

Several sources share a label (five BBC feeds are all `BBC`), so the node names the
organization rather than the feed. The default preset, `all`, leaves out `bbc-top` and the
topic feeds because they mostly repeat stories from the world feeds.

### 6.2 Fetching

`feed.fetch(url, timeout)` uses `urllib.request` with a fixed User-Agent and handles both
`http(s)://` and `file://`. Every failure, whether an HTTP status, DNS, a refused
connection, a timeout or a malformed URL, becomes a `FeedError` with a one-line message.
`nws.load` fetches through the same function. `feed.fetch_with_type(url, timeout, limit=)`
is the same fetch, which also returns the `Content-Type` and refuses a reply over `limit`
bytes; the article loader uses it (section 6.4).

### 6.3 Parsing

`feed.parse(data, fallback_source)` decides the format from the root element. `<feed>` is
Atom, and `<rss>`, `<RDF>` or any document with a `<channel>` is RSS. Tag lookups ignore
XML namespaces. `_child_text` tries candidate child elements in priority order and returns
the first non-empty one.

| Field | RSS 2.0 and RDF | Atom |
|---|---|---|
| Title | `title` | `title` |
| Summary | `description`, then `summary`, then `content:encoded` | `summary`, then `content` |
| Link | `link` | the first `link` whose `rel` is `alternate` or absent |
| GUID | `guid`, then the link, then a `sha1:` hash of title and date | `id`, then the link, then the hash |
| Published | `pubDate`, `dc:date`, `published`, `updated` | `published`, then `updated` |
| Categories | text of each `category` | `term` of each `category` |
| Source | the channel `title`, else the URL | the feed `title`, else the URL |

`strip_html` reduces titles and summaries to plain text. It keeps character data, turns
block tags into spaces and collapses whitespace. Dates may be RFC 822 or ISO 8601, and
times without a zone are taken as UTC.

The GUID is the event's identity. A feed that issues a new GUID for the same story
produces a new event every time.

### 6.4 Article text

No configured feed carries an article's text: `content:encoded` is absent, or a copy of the
summary. `article.load(url, timeout)` therefore fetches the page, at most `MAX_PAGE`
(5 MB), with `fetch_with_type`. It refuses anything whose type is not HTML. It decodes the
bytes with `decode`: the header's charset, else a `<meta>` charset in the first 4 KB, else
UTF-8. Then it calls `extract`. Every way of failing is a `FeedError` with a one-line
reason, and the reader shows that reason.

`extract(page)` is a pure function that returns a `Body`, or `None` when the page holds no
article. It uses Mozilla Readability's scoring, plus two rules that the live sources needed,
and falls back to JSON-LD. Every step below was needed on a real page:

1. **Tree.** An `html.parser` subclass builds a light element tree.
   - It drops the contents of `SKIP_TAGS`: script, style, noscript, template, svg, math,
     iframe, object, canvas, form controls, nav, footer, aside, figure and figcaption.
   - Skipping ends at the element's own end tag, or at the end tag of an element around
     it, so an unclosed `<figure>` cannot swallow the rest of the page.
   - An open `<p>` closes when a block element starts, and `<li>` closes an open `<li>`,
     as in a browser. End tags pop to the nearest matching open element; strays are ignored.
   - `<script type="application/ld+json">` blocks are kept aside.
2. **Score** (Readability).
   - Each `<p>` or `<pre>` of at least 25 characters is worth `1 + commas + min(length/100, 3)`.
     It gives that in full to its parent, half to its grandparent, and `1/(3 × level)` to
     each of the next three ancestors.
   - An ancestor starts from its tag's weight in `TAG_WEIGHT`, then gains 25 when its class,
     id, `data-component` or `role` names content (`POS`) and loses 25 when it names
     furniture (`NEG`: promo, related, share, newsletter, caption, byline, advert, cookie,
     links-block and the like).
   - Each score is finally multiplied by (1 − link density).
3. **Choose.**
   - Take the best-scoring element, and climb to a parent that scores higher, stopping at
     a parent below a third of the best. Then climb past wrappers that are an only child.
   - Join the siblings that share its tag and a non-empty class, and siblings that score
     at least max(10, 20% of the best).
   - The join is what puts BBC articles back together. Their 38 paragraphs sit in separate
     `ComponentWrapper` blocks, and without the join only the largest block came through.
4. **Emit,** in document order: `p` and `pre` as paragraphs, `h2`–`h6` as subheadings, `li`
   as list items, `blockquote` as a quote.
   - A furniture-named block inside is skipped, unless it holds at least half the chosen
     text. ABC Australia wraps its body in such a block, so excluding by name alone lost it.
   - The best-scoring element itself is never skipped. A joined sibling can be: a byline
     shares its wrapper class with the text blocks.
   - Blocks that are at least half links are dropped, as are duplicates and headings with
     no paragraph after them (BBC's "Related topics").
5. **Decide.**
   - Under `MIN_TEXT` (500 characters) of paragraphs actually shown, the page is not an
     article and the result is `None`. This catches CBS video pages, France 24's ad-block
     notice and UN live pages.
   - An `articleBody` in the JSON-LD wins instead when it has at least 500 characters and is
     more than 1.2 times the HTML text. SCMP's pages are drawn by script, and only their
     JSON-LD carries the text, one paragraph per line. An `articleBody` that contains tags is
     parsed as HTML.

Measured on 2026-10-29 with the first three articles of each of the 16 sources:

| Sources | Result |
|---|---|
| bbc, abc-us, dw, euronews, aljazeera, cnbc | full text |
| npr, pbs, csmonitor, rfi, abc-au | full text, with the site's own lines kept: a newsletter plug, "Support trusted journalism", "Already a subscriber? Login", "Issued on", an "In short" box |
| cbs | full text, live blogs included (5,400 words); video pages have none |
| un | full text; live pages have none |
| scmp | full text, from the JSON-LD |
| japantimes | the free paragraphs only (paywall) |
| france24 | mostly video pages, which have none |

Of the 48 pages, 42 gave text: 39 from the HTML and 3 from the JSON-LD. The other six were
video pages, live pages and an NPR audio story, and they correctly fall back to the
summary. Extraction takes 10–60 ms a page, and about 200 ms for a 0.9 MB live blog.

The tables and thresholds are at the top of `article.py`. Each page in
`tests/fixtures/articles/` copies one structure found live, with invented text. To repeat
the measurement:

```sh
python3 -c "
from worldterm import article, feed, sources
for key, src in sources.SOURCES.items():
    for item in feed.load(src.url)[:3]:
        try: body = article.load(item.link); print(key, body.method, body.words, 'words')
        except feed.FeedError as exc: print(key, '--', exc)"
```

## 7. News severity

`severity.match_details(title, summary, categories)` returns a `MatchDetails`, and
`classify` returns only its severity. `Store.merge` calls it, through
`store.classify_article`, for every news item on every refresh.

### 7.1 Algorithm

1. Join the title and summary. If the result is empty, the item is Indeterminate with
   origin `empty`, and no modifier applies.
2. Find every rule keyword in the text. Each keyword counts once per item.
3. If anything matched, the base level is the highest level among the matches (origin
   `text`). Otherwise the same rules are tried on the feed's category tags (origin
   `category`). Otherwise the base is Warning (origin `default`).
4. Apply at most one modifier, read from the title:
   - A title that starts with *Live*, *Breaking* or *Urgent* goes up one level, to at
     most Critical.
   - A title that starts with *Analysis*, *Opinion*, *Explainer*, *Explained*, *Comment*,
     *In pictures*, *Watch*, *Listen* or *Quiz*, or is a *Why*, *What* or *How* question
     ending in `?`, goes down one level, to no lower than Warning.

### 7.2 Rules and matching

`RULES` is a list of `(level, keywords)` pairs, most severe first, for Critical, Major and
Minor. Each keyword is a regular-expression fragment such as `deaths?` or `strikes? on`.
Each level compiles into one case-insensitive alternation that must start at a word
boundary. How a keyword may end is the subtle part:

- **Critical and Major keywords match as prefixes.** `kill` matches *killed* and
  `evacuat` matches *evacuation*. This handles inflections cheaply.
- **The stems in `WHOLE_WORDS` must end at a word boundary**: `wars?`, `coups?`, `dead`,
  `elect`, `strikes?` and `sues?`. Each was measured misfiring on live feeds: *war* on
  warning, warranty and warehouse; *elect* on electric; *coup* on couple; *dead* on
  deadlock; *strike* on striker; *sue* on Suez. Their real forms, such as *wartime*,
  *elected*, *deadly* and *sued*, are listed as keywords instead.
- **Minor keywords always match whole words**, so *pet* never fires inside *petrol*.
- **Longer alternatives are tried first.** Regular-expression alternation is ordered, so
  without this `deaths?` would always beat `death toll`.

`_canonical` maps matched text back to the rule that produced it (*killed* becomes
`kill`), so statistics count rules rather than spellings. `display_keyword` turns a rule
into readable text for the dashboard (`deaths?` becomes *death*). The reader's Triggers
line instead uses `matched_keywords`, which reports the text that matched.

### 7.3 Tuning

The dashboard's Triggers panel (section 11) exists to make the rules auditable. Start
from a keyword with a surprising *Events* count, or one that never decides.

1. Find an affected headline and open it with `r`. The Triggers line shows what matched.
2. Add the headline to `tests/test_severity.py` with the level it should get. The two
   whole-word tests show the pattern.
3. Change `RULES`, `WHOLE_WORDS` or the modifier patterns. Prefer listing a stem's real
   forms to loosening a pattern.
4. Run the whole suite. `tests/test_analytics.py` counts triggers over fixture articles
   and will notice changes in what fires.

Because severity is recomputed on every merge, a rule change also re-grades items already
in the list at the next refresh.

## 8. Weather alerts

### 8.1 Request

`nws.alerts_url(area)` builds
`https://api.weather.gov/alerts/active?status=actual&message_type=alert,update`, adding
`&area=MA,RI,...` when given two-letter state or marine-area codes. `us`, `all` or an
empty value means no area filter. `off` returns `None`.

Cancellation messages are not requested. A canceled alert simply leaves the active set,
and the lifecycle in section 8.4 treats that as clearing.

### 8.2 Parsing

`nws.parse` reads the GeoJSON `FeatureCollection` and builds one `Alert` from each
feature's `properties`. Geometry is ignored. A problem document (JSON with a `detail`
field) or a reply that is not JSON raises `FeedError` carrying the API's message.
Headlines and area descriptions are collapsed to single spaces. Descriptions and
instructions arrive hard-wrapped, so `_paragraphs` joins the lines of each paragraph but
keeps the blank lines between paragraphs. Timestamps keep their original UTC offsets.

### 8.3 Derived fields

- **Severity** follows `SEVERITY_MAP`: Extreme to Critical, Severe to Major, Moderate to
  Minor, Minor to Warning, Unknown to Indeterminate. Cancel and Expire messages map to
  Clear.
- **Office** is the sender's name without `NWS `, with doubled spaces collapsed, so
  `NWS Pago Pago  AS` becomes `Pago Pago AS`. It is the node.
- **States** come from the first two letters of each UGC code in `geocode.UGC`: `FLC123`
  and `FLZ021` both mean `FL`. Marine prefixes map to the `MARINE_AREAS` labels, such as
  `Marine Lakes`, which are kept short for the dashboard. An alert whose zones span
  several states belongs to each of them.
- **Kind** is the last word of the event name if it is Warning, Watch, Advisory,
  Statement or Outlook, and Other otherwise, as for *Air Quality Alert*.
- **Active until** is `ends`, or `expires` when there is no end time.
- **Title** is `<event>: <area description>`.

### 8.4 Lifecycle

`nws.apply_alerts(store, alerts, now)` folds one fetch into the weather store in four
steps.

1. **Continue updated alerts.** An alert whose `references` name an event that is still
   in memory, but not in this fetch, takes over that event's state through
   `Store.transfer`: acknowledgment, read flag, note, first sighting and tally. The old
   severity is remembered.
2. **Merge.** `Store.merge` runs with a classifier that returns the alert's own severity,
   so no keyword rules apply. Each event gets its `alert` attached. If an update raised
   the severity of an acknowledged event, the acknowledgment is removed and the event is
   marked new, so an escalation asks for attention again.
3. **Clear.** `Store.clear_missing` sets every event that is not in this fetch to Clear
   and records `cleared_at`. If the alert reappears, the next merge restores its live
   severity.
4. **Forget.** `Store.drop_cleared` deletes events that have been Clear for longer than
   `CLEAR_RETENTION`, 30 minutes, from memory and from the records.

A fetch that fails never reaches `apply_alerts`, so a network problem clears nothing.
Section 17 explains why updates continue their alert, and section 16 lists the cases this
misses.

## 9. The event store

`store.Store` holds one list's events and their persisted records. The app has two:
`store` for news, saved as `state.json`, and `weather_store` for alerts, saved as
`weather.json`. Both write notes into the same `notes/` directory.

### 9.1 Events and records

A store keeps two dictionaries keyed by GUID:

- `events` holds `Event` objects for everything seen in this session. Only these are
  shown.
- `_records` mirrors the JSON file. It holds flags and timestamps for every item seen in
  the last week, whether or not it is in memory.

### 9.2 Merge

`merge(articles, classify=classify_article, now=None)` returns the events that are
genuinely new. For each article:

- **Already in memory:** replace the article, recompute severity and match details, clear
  `cleared_at`, add one to the tally and set `last_seen`.
- **Not in memory, but recorded** from an earlier session: create the event with its
  recorded first sighting, flags, note, and tally plus one. It is not new.
- **Unknown:** create it with a tally of 1, `first_seen` and `last_seen` both now, and
  `new` set.

An event missing from a fetch is left alone in the news store. In the weather store,
`apply_alerts` clears it.

Because each merge adds one, an article that stays in its feed for ten hours at the
default five-minute interval reaches a tally of about 120. An article that two configured
feeds publish under the same GUID is a single event whose tally rises twice per refresh.

### 9.3 Saving, pruning and the file format

`save()` copies every in-memory event into `_records`, prunes, writes the JSON to a
temporary file in the data directory, and renames it over the target with `os.replace`.
An interrupted save therefore never leaves a half-written file. Pruning deletes records
whose event is not in memory and whose `last_seen` is more than seven days old
(`PRUNE_AFTER`).

```json
{
 "<guid or alert id>": {
  "acknowledged": true,
  "first_seen": "2026-10-10T15:56:33+00:00",
  "last_seen": "2026-10-11T01:46:33+00:00",
  "link": "https://example.org/story",
  "note": "notes/20261010-example-story.md",
  "read": true,
  "source": "CBS News",
  "tally": 119,
  "title": "Example story"
 }
}
```

`title`, `link` and `source` are there for people reading the file; `merge` never reads
them back. Severity is not saved. `load()` treats a missing, unreadable or malformed file
as empty.

### 9.4 Operations for feeds that report current state

The NWS reports what is active now, rather than a stream of stories, so the weather store
also uses three generic operations:

- `transfer(old, new)` writes the old event's record under the new id and removes the old
  event, so the next merge restores the state under the new id.
- `clear_missing(present, now)` turns every event not in `present` to Clear.
- `drop_cleared(retention, now)` deletes events that have been Clear for longer than
  `retention`.

Another feed of the same kind can reuse them.

### 9.5 Notes

`write_note(ev, filename, text)` writes `notes/<filename>` and sets `ev.note` to the
relative path. `read_note` returns the text, or `None`. The list chooses the file name
with `slug.note_filename(title, first_seen)`: `YYYYMMDD-<slug>.md`, where the slug is the
title folded to lower-case ASCII, joined with hyphens and cut at a word boundary within
60 characters. This scheme can make two events share a file; see section 16.

## 10. The event lists

### 10.1 Screen stack

```
WorldTerm (App)
└── MainScreen               news list; never removed; owns the refresh timer
    ├── WeatherScreen        alert list    open: w, WX lamp     close: w, Esc
    ├── DashboardScreen      analytics     open: d, header      close: Esc, q, d, right-click
    ├── ReaderScreen         details       open: r, menu        close: Esc, q, h, ←, right-click
    ├── NoteEditorScreen     note buffer   open: n, menu, n in the reader
    │   └── ConfirmDiscard   modal         y discards; n or Esc keeps editing
    ├── ContextMenu          modal         open: right-click, Enter, m
    └── HelpScreen           modal         open: ?, h           close: any key or click
```

The screens under `MainScreen` can also be opened from `WeatherScreen`. `WorldTerm.main`
and `WorldTerm.weather` find those screens anywhere in the stack, so new data reaches them
even when they are covered. `WorldTerm.dashboard` returns the dashboard only while it is
the top screen.

Every push creates a new screen object. The weather list's sort, filter and selection,
and the dashboard's sort choices, therefore reset on every visit. The news list keeps its
own because it is never removed.

### 10.2 EventListScreen and its subclasses

`listscreen.EventListScreen` is the whole Netcool list: header, table and footer,
selection, filters, the context menu, the reader, notes, and opening links. It holds the
list's view state (`sort_by_time`, `filter_idx`, `hide_acked` and `selection`), so the two
lists are independent and a batch action can never reach across them.

Subclasses provide the data and presentation through these hooks:

| Hook | Purpose | `MainScreen` | `WeatherScreen` |
|---|---|---|---|
| `store` | Which store to show | `app.store` | `app.weather_store` |
| `NAME` | Header title | `WORLDTERM` | `WEATHER` |
| `COLUMNS` | `(key, width)` pairs; width `None` marks Summary | Node, First, Last | Office, Issued, Expires |
| `cells(ev, summary_w)` | Cell strings in column order | inherited | alert times |
| `LAMPS` | Levels counted in the header | Critical to Indeterminate | adds Clear |
| `header_left()` | Start of the header | node names, event count | active and ended alerts, offices, states |
| `header_extra(text)` | After the lamps | the WX lamp, the `CYD` and `WALL` chips | nothing |
| `header_clicked(x)` | A click on the header | WX lamp opens weather; elsewhere, the dashboard | the dashboard |
| `refreshed_at()` | The "Refreshed" time | last news fetch | last alert fetch |
| `time_key(ev)` | Timestamp used for sorting | `last_seen` | issue time |
| `DASHBOARD_PAGE` | Dashboard page that `d` opens | news | weather |

`MainScreen` also binds `w` to open the weather list, and starts fetching. `WeatherScreen`
binds `w` to go back, and makes `Esc` go back when nothing is selected.

Sorting is by severity and then time, newest first, or by time alone (`s`). For news the
time is `last_seen`, which groups rows by the refresh that last saw them, as Netcool sorts
by last occurrence. The filter (`f`) cycles through all levels, Minor and above, Major and
above, and Critical only. Any filter hides Clear alerts.

### 10.3 Full-row severity color

Textual's `DataTable` styles the text in a cell. It does not color cell padding, unused
column width or the space after the last column. Netcool colors the whole row, so
`table.EventTable` overrides the private method `_get_row_style(row_index, base_style)` to
add a per-row style from `_row_styles`. DataTable applies the row style to every part of
the row. Cells are added as unstyled `Text`, so all color comes from the row style.

The details that make it work:

- `set_row_styles(styles)` takes one style per row, in display order, and must be called
  *before* the rows are added. `set_row_style(index, style)` changes one row. Follow it
  with an `update_cell` on that row, which bumps DataTable's update counter and so
  invalidates its render caches.
- The cursor style is reverse and bold, so the cursor row inverts its own colors instead
  of turning the theme's blue. The hover style is empty, because otherwise hover repaints
  the row background.
- The table always reserves its vertical scrollbar (`overflow-y: scroll`), so the content
  width does not jump when rows are added. The Summary column is sized to fill that width
  exactly: the content width, less the fixed columns, less two cells of padding per
  column. Columns are rebuilt on resize.

`_get_row_style` and the component names `datatable--cursor` and `datatable--hover` are
Textual internals, verified on 2.1.2. `test_acknowledged_rows_use_darker_background`
checks the color of every segment of a rendered row and will fail if an upgrade breaks
this.

A row's style comes from `styles.severity_style(level, acknowledged, new, selected)`: the
level's style, or its acknowledged style; bold if new and not acknowledged; underlined if
selected.

### 10.4 Rebuilding and refreshing rows

`rebuild()` clears the table and refills it from `visible_events()`, which applies the
filter, the hide-acknowledged switch and the sort. It runs when data arrives, when sort or
filter changes, after the reader or editor closes, and on resize. It keeps the cursor on
the same event, by GUID, and removes from the selection any event no longer visible.

`refresh_rows(guids)` restyles particular rows and updates their `Sel` and `A` cells in
place, without changing order or moving the cursor. Selection changes and
acknowledgments use it. The exception is an acknowledgment while acknowledged rows are
hidden, which has to remove rows and so rebuilds.

### 10.5 Selection and batch acknowledge

- `selection` is a set of GUIDs held by the list screen. Selected rows show `●` in the
  first column and are underlined, and the header shows how many there are.
- `ack_targets()` returns the selected events in display order, or the cursor row when
  nothing is selected. Every way of acknowledging (the `a` key, the menu, the reader) goes
  through `acknowledge(targets)`.
- `acknowledge` acknowledges every target, unless they are all acknowledged already, in
  which case it unacknowledges them all. The selection is kept, so pressing `a` a second
  time undoes a batch.
- The selection is cut down to visible rows on every rebuild, so a filter can never leave
  hidden events in a batch.
- Right-clicking a row outside the selection clears the selection and acts on that row
  alone. Right-clicking inside it acts on the whole selection.

### 10.6 Keys and mouse

Bindings are declared on each screen class: `EventListScreen.BINDINGS`, with additions in
each subclass, and the bindings of `DashboardScreen`, `ReaderScreen`, `NoteEditorScreen`
and `ContextMenu`. The footer lists them automatically. They are also described by hand in
`HELP_TEXT` in `ui/listscreen.py` and in the README's key table, so changing a binding
means three edits.

Two bindings are marked `priority=True` and need to stay that way:

- `Enter` and `m` open the menu, because `DataTable` binds `Enter` itself.
- `Esc` in the note editor cancels, because `TextArea` would otherwise consume it.

Mouse handling is in `EventListScreen.on_mouse_down`:

- The row under the pointer is computed from the table's content region, header height
  and scroll offset (`_row_at`), not from DataTable's hover state, which can be out of
  date.
- Ctrl+click toggles one row, and Shift+click selects from the cursor to the clicked row.
  Both act on mouse *down*. DataTable moves the cursor on the click that follows, so at
  mouse down the cursor is still the anchor of the range.
- A right-click moves the cursor to the row and opens the menu one line below the
  pointer.
- A left click on the header bar reaches `header_clicked` through `on_click`.

### 10.7 Context menu

`ContextMenu` is a `ModalScreen` with a transparent background. Its `OptionList` is moved
to the pointer by a style offset, clamped so the whole menu stays on screen. The clamp
uses `app.size`, because the modal screen's own size is still 0 by 0 when `on_mount` runs.
The menu's width and height are estimated from its labels with the same border, padding
and minimum width as its CSS.

The menu dismisses with the chosen item's key, or with `None` on `Esc`, `q` or a click
outside it. Typing a letter chooses the first item whose label starts with that letter.
The list screen builds the items in `open_menu` and acts on the result in its `_chosen`
callback.

### 10.8 Reader and note editor

For news, `ReaderScreen` shows the article's node, dates, flags, link, an Article line,
categories, matched keywords and text.

- The text starts as the feed's summary. When the page arrives it is replaced by the
  article's text (section 4.6): blocks separated by blank lines, subheadings in bold, list
  items as `•` lines, quotes in italics, at most 100 columns wide.
- The Article line says `fetching from <host> …`, then `N words from <host>, extracted from
  the page (o opens the original)`. On failure it says `unavailable (<reason>); showing the
  feed's summary`.

When the event has an `alert`, the reader shows instead the office, the NWS severity,
urgency, certainty and response, the areas, the timing, the zones, and the full
description and instructions. It fetches nothing. Every string from a feed or a page
reaches its widget as `Text` (section 10.10).

`NoteEditorScreen` wraps a `TextArea`. A new note starts from
`listscreen.note_template(ev)`: a heading made from the slug, then metadata, with a
different block for alerts. `Ctrl+E` writes the buffer to a temporary file, suspends the
app with `App.suspend()`, runs `$VISUAL` or `$EDITOR` on the file and loads the result
back. `Esc` asks before throwing away changes.

### 10.9 Header

`render_header()` runs every second, for the countdown, and after every change. From left
to right it shows the screen name and its summary, a selection badge, one lamp per level
in `LAMPS`, anything the subclass adds, and right-aligned status: last refresh, time to
the next refresh, and tags for a non-default sort, a filter, hidden acknowledgments and a
fetch in progress.

On the news list, the subclass adds the **WX lamp**: the number of active alerts,
colored by the most severe one. `MainScreen` records the lamp's position so a click on
it opens the weather list. Two chips follow it. `CYD` appears unless `--cyd off` or the
host is not Linux, green while a board is connected and dim otherwise. `WALL :8070` (the bound address) is green
while the wall page is served; a dim `WALL` means its port could not be taken; neither
appears with `--wall off`. Both chips carry their name, so they do not depend on color.

### 10.10 Colors, text and markup

`palette.SEVERITY_COLORS` gives each level an explicit background and text color.
Acknowledged rows use the same hue at 45% brightness (`ACK_DARKEN`) with light gray text
(`ACK_FOREGROUND`). `tests/test_styles.py` requires distinct backgrounds and at least 4:1
contrast for acknowledged text. The palette lives in `worldterm/palette.py`, outside `ui/`,
so the wall page can be sent it as JSON; `ui/styles.py` re-exports it and builds the Rich
styles from it.

| Level | Background | Text | Acknowledged background |
|---|---|---|---|
| Critical | `#f4005f` | white, bold | `#6e002b` |
| Major | `#ff8700` | black | `#733d00` |
| Minor | `#ffd700` | black | `#736100` |
| Warning | `#58d1eb` | black | `#285e6a` |
| Indeterminate | `#af5fd7` | white | `#4f2b61` |
| Clear | `#98e024` | black | `#446510` |

The colors are hexadecimal because Textual redraws ANSI color names through its theme,
which shows yellow as orange and magenta as red (section 17, D3).

Textual parses plain strings given to `Static`, `Label`, their `update()` methods and
border titles as markup: any word in square brackets is read as a style tag. So:

- Text that comes from a feed or an alert must be passed as `rich.text.Text`, which is
  never parsed. The table cells, headers and dashboard tables do this.
- Key hints in panel titles are written `(t)`, not `[t]`, which would vanish.

Two labels in the note editor still pass plain strings; section 16 describes what goes
wrong. The reader passes every string as `Text`, and a test opens a headline containing
`[/b]`.

## 11. The dashboard

### 11.1 Layout

`DashboardScreen(page)` builds both pages at once, each in its own body container, and
hides the inactive one with the `hidden` class. `w` or `Tab` switches page, and `d` from a
list opens the page for that list.

| Page | Panels at 100 columns or more |
|---|---|
| News, a 2 by 2 grid | *By severity* top left · *By source* in the right column, spanning both rows · *Triggers* bottom left |
| Weather, a 2 by 3 grid | *Alerts by severity* top left · *By state* or *By office* in the right column, spanning all rows · *Event types* middle left · *Timing and urgency* bottom left |

Below 100 columns (`NARROW_BELOW`), both bodies get the `narrow` class. The grid becomes a
vertical stack, every panel takes the height of its content, and the body itself takes the
focus so the arrow and page keys scroll it. The narrow rule names the panels by id,
because the id rules that give them `height: 1fr` would otherwise win over a class rule.

### 11.2 Data path

```
store.events
   │
   ▼  analytics.py             pure functions that return small dataclasses
   │
   ▼  charts.py,               dataclasses to Rich renderables
   │  weatherpanels.py
   ▼
Static.update(renderable)      in DashboardScreen._refresh_news / _refresh_weather
```

`refresh_data()` recomputes the visible page from scratch. It runs on mount, on a page
switch, on every sort or grouping change, and whenever the app merges data or finishes a
fetch while the dashboard is the top screen. With hundreds of events this is cheap.

### 11.3 Aggregations

| Function | Returns | Shown in |
|---|---|---|
| `severity_rows` | Total, acknowledged and read per level; empty Clear and Indeterminate rows are dropped | *By severity*, both pages |
| `group_rows(events, keys, sort)` | Per group: total, count per level, acknowledged share. `keys` may put one event in several groups | The lamp tables |
| `source_rows`, `state_rows`, `office_rows` | `group_rows` keyed by node, by `Alert.states`, or by `Alert.office` | *By source*, *By state*, *By office* |
| `trigger_rows` | Per keyword: items containing it, and items it decided | *Triggers* table |
| `level_totals` | Per rule level: keywords that fired, items decided | *Triggers* summary |
| `summary` | Totals, how severities were assigned, modifier counts | News header bar, *Triggers* summary |
| `type_rows` | Per NWS event type: count, count per level, states, offices, most common level | *Event types* |
| `kind_counts` | Warnings, watches, advisories, statements, outlooks, other | *Alerts by severity* |
| `issued_histogram(events, hours, now)` | Alerts per issue hour, oldest first, and the count issued earlier | *Timing*: sparkline |
| `expiry_buckets(events, now)` | Ended, within 1 h, 6 h, 24 h, later | *Timing*: bars |
| `urgency_certainty` | Urgency against certainty, active alerts only | *Timing*: table |
| `response_counts` | Recommended responses, active alerts only | *Timing* |
| `weather_summary` | Totals, active, ended, states, offices, types, updates | Weather header bar, *Timing* |
| `arrival_histogram(events, hours, now)` | Events per hour by `first_seen` (when worldterm first saw them), oldest first, and the count seen earlier | The wall page's arrivals strip |

`issued_histogram` and `arrival_histogram` share one bucketing helper, `_hour_buckets`. The
wall page's snapshot also reuses `severity_rows`, `source_rows`, `type_rows`, `kind_counts`
and `weather_summary`, so its numbers always agree with the dashboard's. `first_seen` is
restored from the state file across restarts; on a first run with no state file, every
event falls into the current hour until an hour has passed.

The sort orders are named in tuples (`SOURCE_SORTS`, `TRIGGER_SORTS`, `TYPE_SORTS`). Each
order ends with an alphabetical tie-break, so output is deterministic, and the tests depend
on that. On both pages `s` cycles the group sort. `t` cycles the trigger sort on the news
page and the event-type sort on the weather page. `o` switches the weather page between
states and offices.

### 11.4 Renderables

- `charts.StackedBar(parts, scale)` is a custom Rich renderable: colored blank cells, one
  segment per `(value, style)` part, filling `value / scale` of the width it is given.
  The bars of one chart share a `scale`, so lengths compare across rows. Cells are shared
  out by largest remainder, and every non-zero part gets at least one cell so small
  counts stay visible.
- `charts.group_table` draws Netcool lamps: each count right-aligned on its level's
  color, with `·` for zero.
- `charts.sparkline` maps values onto `▁▂▃▄▅▆▇█`, drawing zeros as a dim baseline.
- `weatherpanels.EXPIRY_STYLES` colors the time-to-expiry bars from hot (soon) to cold
  (later), with Clear's green for ended alerts.

Every bar and lamp is accompanied by a number or a legend.

## 12. Configuration and files

### 12.1 Command line

| Option | Default | Effect |
|---|---|---|
| `--feed`, `-f [LABEL=]URL` | none | Repeatable. Replaces `--preset`. `LABEL=` sets the node name |
| `--preset`, `-p NAME[,NAME...]` | `all` | Sources and groups from `sources.py` |
| `--list-presets` | | Print the catalog and exit |
| `--interval`, `-i SECONDS` | 300 | Refresh period for news and weather, at least 15 |
| `--timeout SECONDS` | 15 | Per-request timeout: each feed, the alerts, and each article page the reader fetches |
| `--data-dir DIR` | see below | Where the state files and notes live |
| `--weather`, `-w AREA` | `us` | `us`, `off`, or codes such as `MA,RI,AN` |
| `--weather-url URL` | none | Fetch alerts from this URL or `file://` path instead; overrides `--weather` |
| `--cyd MODE` | `auto` | The CYD display: `auto` uses a board whenever one is plugged in, `off` never opens a serial port, a path such as `/dev/ttyUSB0` uses that port only, `ble:ADDRESS` reaches the board over Bluetooth LE |
| `--wall MODE` | `8070` | The wall page: a port (every interface), `HOST:PORT` for one address, or `off` |
| `--version` | | Print the version written in `main.py` |

### 12.2 Data directory

`$XDG_DATA_HOME/worldterm`, or `~/.local/share/worldterm` when that variable is unset:

```
state.json     news event records
weather.json   alert event records
notes/         YYYYMMDD-<slug>.md, one per noted event, from both lists
```

Temporary `.state-*.json` files exist briefly while a save is in progress.

### 12.3 Environment

- `XDG_DATA_HOME` chooses the data directory.
- `VISUAL`, then `EDITOR`, is the external editor for `Ctrl+E`. The value is split like a
  shell command, so it may include arguments.
- Links open with `xdg-open` when it is installed, otherwise with Python's `webbrowser`.
- Right-click menus need a terminal with mouse reporting. Everything also works from the
  keyboard.

## 13. Failure handling

| Failure | Detected by | The user sees | State |
|---|---|---|---|
| Feed unreachable, HTTP error or timeout | `feed.fetch` raises `FeedError` | "Feed error: name: message" for 8 seconds | Existing events kept; other feeds unaffected |
| Feed is not XML, or not RSS or Atom | `feed.parse` raises `FeedError` | Same | Same |
| Any other exception while loading a feed | `fetch_feeds` | Same, with the exception's type | Same |
| NWS error document, or a reply that is not JSON | `nws.parse` raises `FeedError` | "Feed error: NWS alerts: message" | Alerts are not cleared |
| State file missing or corrupt | `Store.load` | Nothing | Starts empty; the file is replaced at the next save |
| State cannot be written | `WorldTerm.save` | "Cannot save state" | In-memory state continues |
| Note cannot be written | the list's note callback | "Cannot write note" | The note is not linked |
| No `$VISUAL` or `$EDITOR` | `NoteEditorScreen` | A warning | |
| Not running in a terminal | `main.py` | A message on stderr, exit status 2 | |
| No CYD plugged in, or none with a known USB id | `CydService` finds no port | Nothing; the `CYD` chip stays dim | Looks again every 2 seconds |
| `--cyd PATH` on a host without `termios` | `main.py` | An argument error, exit status 2 | With `auto`, such a host simply has no display |
| The port is busy (mpremote has it) or not permitted | `SerialPort` raises `OSError` | Nothing | Retried every 2 seconds |
| The port answers nothing, or with another protocol version | the handshake | One warning toast per port | The port is left alone for 30 seconds, then tried again |
| The board is unplugged, or stops answering | a read or write fails, or three commands go unanswered | "CYD display disconnected"; the chip dims | Rescan every 2 seconds; the frame is redrawn on reconnect |
| worldterm dies without closing the port | the board | "no update from worldterm" on the board's bottom row after 30 seconds | |
| A BLE board is off, out of range, or running another program | the connect fails or finds no Nordic UART Service | Nothing; the `CYD` chip stays dim | Tried again every 10 seconds |
| `--cyd ble:` with a malformed address, or Python older than 3.14 | `main.py` | An argument error, exit status 2 | |
| Another program on this computer holds the BLE connection: the desktop after pairing in Settings, another worldterm, the send command | the connect fails with `EBUSY` (`bleport.LinkError`, which carries a hint) | One toast naming the cause and `bluetoothctl disconnect`; the send command prints the same | Tried again every 10 seconds; one ATT channel per computer and board |
| The wall port is taken, or cannot be bound | `WallServer` raises `OSError` at startup | "Wall page: cannot listen on ..."; a dim `WALL` chip | No page; everything else runs |
| A browser cannot reach worldterm | the page's poll fails | "worldterm unreachable since HH:MM" on the page; the last data stays | |
| An article's page cannot be fetched (an HTTP error, DNS, a timeout, over 5 MB) | `fetch_with_type` raises `FeedError` | The reader's Article line: "unavailable (HTTP 403); showing the feed's summary" | Not cached: opening the reader again retries |
| The link is not a web page (audio, PDF) | `article.load` checks the type | "unavailable (not a web page (audio/mpeg))" | Same |
| The page has no article (a video, a live page, an audio story) | `extract` returns `None` | "unavailable (no article text on the page)" | Same |
| The extractor raises | `_fetch_article` | "unavailable (TypeError: ...)" | Same; the reader never waits forever |

## 14. Testing

### 14.1 Running the tests

```sh
python3 -m unittest discover -s tests -v
```

The suite takes about a minute, almost all of it in the headless UI tests. It needs no
network access and opens no serial port. The two feed-error tests connect to a closed port
on 127.0.0.1, the wall tests bind 127.0.0.1 on a port the kernel picks, and the reader
tests use a fake article loader or a `file://` page. It prints lines
such as `Executing <Task ...> took 0.110 seconds`. These are asyncio's slow-callback
warnings, which appear because `IsolatedAsyncioTestCase` runs the event loop in debug
mode. They are not failures.

### 14.2 What each file covers

| File | Covers |
|---|---|
| `test_feed.py` | RSS and Atom parsing, field priorities, HTML stripping, dates, GUID hashing, `file://` fetch, `fetch_with_type`, errors |
| `test_article.py` | Finding an article's text: each fixture page, broken HTML (unclosed `<p>` and `<figure>`), entities, line breaks, markup-like brackets, duplicates, link lists, the `MIN_TEXT` boundary, JSON-LD (plain, with markup, malformed, shorter than the page), charsets, and `load`'s refusals |
| `test_severity.py` | Every classifier behavior: levels, default, modifiers, categories, whole-word stems, longest match, `MatchDetails` |
| `test_slug.py` | Slugs and note file names |
| `test_sources.py` | Catalog consistency, preset resolution, `--feed` parsing |
| `test_store.py` | Merging, tally, saving and loading, pruning, corrupt files, notes |
| `test_nws.py` | Alert parsing, the severity table, derived fields, URL building, every step of `apply_alerts` |
| `test_analytics.py` | Every aggregation, for news and for weather |
| `test_styles.py` | Distinct colors, acknowledged contrast, row-style flags |
| `test_ui.py` | Both lists, the menu, selection and batch acknowledge, reader, editor, both dashboard pages, narrow layouts, feed errors; the reader's article text: shown, failed, cached, one download however often it is asked for, a reader closed early, quitting mid-download, alerts never fetched, markup-like headlines, and a `file://` page end to end |
| `test_serialport.py` | Port discovery against a fake `/sys`; raw I/O, timeouts, the exclusive lock and loss of the device, on a pseudo-terminal |
| `test_cyd.py` | Headline selection and the day rule, ASCII folding and wrapping, frames and the line protocol, every state of `CydService` against a scripted fake board, the BLE settings of `service_for`, and the app wiring with an injected service |
| `test_bleport.py` | The BLE link against a fake ATT server on a `SOCK_SEQPACKET` socket pair: MTU, discovery across several responses, subscription, writes cut to the MTU, notifications and indications, requests from the board, a missing UART, timeouts, the board leaving, the send-text program, and `CydService` driving the board over it |
| `test_wall.py` | `--wall` parsing, the snapshot over both fixtures (rotation, folding, cap, JSON safety), the server on a loopback port (page, state, HEAD, 404, 405, prompt stop, port in use), and the app wiring |

### 14.3 Fixtures

`tests/fixtures/` holds a captured BBC World RSS feed (35 items), a captured NPR feed (10
items), a hand-written Atom feed (3 entries) and `nws_sample.json`, 23 alerts captured
from the live API on 2026-10-10 with geometry removed and long text shortened. One of
those alerts, a Tornado Warning, is synthetic. It was derived from a real Flash Flood
Warning so that the Extreme level is tested, and its `note` field says so. Many tests
assert exact counts from these files, so replacing a fixture means updating them.

`tests/fixtures/articles/` holds four small pages written by hand. Each copies the structure
of a real article page measured on 2026-10-29, with invented text:

- `split_blocks.html`: BBC, an article split across sibling blocks.
- `single_container.html`: Al Jazeera and NPR, one content container.
- `jsonld_only.html`: SCMP, the text only in JSON-LD.
- `video_page.html`: France 24 and CBS, a video page with no article.

### 14.4 Writing UI tests

- Build the app with `file://` URLs, a temporary data directory and a long interval.
  News-only tests pass `weather="off"`. Weather tests pass `weather_url=` pointing at the
  fixture.
- Call `wait_for_load` or `wait_for_weather` before interacting. They poll the app's load
  flags.
- `UITests.rendered(widget, width)` prints a Static's renderable as plain text.
  `UITests.screen_text(app)` converts `export_screenshot()` output to text, for checking
  what is actually on screen.
- Use `pilot.click(EventTable, offset=(x, y), control=True, shift=True)` for modifier
  clicks. Right-clicks are posted to the screen as `events.MouseDown` with button 3.
- Pass `now=` to `nws.apply_alerts` and to the time-based aggregations for deterministic
  results.
- The CYD display is tested without hardware. `CydService` takes a scanner, a port
  factory, a clock, a monotonic timer and a sleep, so `test_cyd.py` drives `tick()`
  against a `FakePort` and a `FakeClock`. The app test passes `cyd_service=` with a fake
  port. The constructor default `cyd="off"` keeps every other test away from real ports.
- The Bluetooth link is tested without a radio: `BlePort(address, sock=...)` takes an
  already connected socket, and `test_bleport.py` hands it one end of an `AF_UNIX`
  `SOCK_SEQPACKET` pair, which keeps message boundaries as the L2CAP socket does. The other
  end is `FakeBoard`, a thread that serves the board's attribute table and answers lines.
  No test calls `service_for("ble:...").tick()`, which would start a real connection.
- The wall page is tested with `WallServer("127.0.0.1", 0)`: the kernel picks the port and
  `address` reports it. Pass it as `wall_server=` to the app, or `wall="127.0.0.1:0"`. The
  constructor default `wall="off"` keeps every other test off the network.
- The reader's article text is tested with `article_loader=`, the function the app calls
  with a link. `test_ui.py`'s `FakeLoader` records each call, returns a canned `Body` or
  raises a `FeedError`, and can wait on a `threading.Event` to keep a download "in flight".
  The download runs on its own thread, so wait for the result with `wait_until` rather than
  a single `pilot.pause()`. The constructor default, `None`, fetches nothing.

### 14.5 Checking in a real terminal

The headless tests do not exercise Textual's terminal driver. For an end-to-end check,
run `main.py` under a pseudo-terminal: `pty.fork()`, with the window size set through the
`TIOCSWINSZ` ioctl. Write keystrokes to it, then check for a clean exit, the absence of a
traceback, and the expected contents of the state files.

Terminals report mouse events in SGR form: `ESC [ < b ; x ; y M` for a press and the same
ending in `m` for a release, with 1-based coordinates. `b` is 0 for the left button and 2
for the right, plus 4 with Shift and 16 with Ctrl.

Rendering screenshots with Pilot at 140 by 44 and at 80 by 24 covers both dashboard
layouts.

## 15. Making common changes

### Add a news source

1. Add a `Source` to `SOURCES` in `worldterm/sources.py`, with an HTTPS URL and a label
   of at most 12 characters.
2. Add its key to the groups it belongs in. Put it in `all` only if it is general world
   news.
3. Check that it parses:
   `python3 -c "from worldterm import feed; print(len(feed.load('URL')))"`.
4. Run `tests/test_sources.py`, and update the sources table in the README.

### Tune the classifier

Follow section 7.3.

### Add a column to a list

1. Add a `(key, width)` pair to `COLUMNS` on the base class or a subclass. A width of
   `None` is reserved for Summary.
2. Return the cell from `cells()` at the same position.
3. If the cell must change without a rebuild, as `A` and `Sel` do, update it in
   `refresh_rows`.
4. Summary's width adjusts by itself. Check the list at 80 columns.

### Add a context-menu action

1. Add a `(key, label)` pair in `EventListScreen.open_menu`. Pick a label whose first
   letter is not already used (section 10.7).
2. Handle the key in `_chosen` by calling an `action_*` method.
3. If it deserves a key of its own, add a binding, a line in `HELP_TEXT` and a row in the
   README's key table.

### Add a dashboard panel

1. Write the aggregation in `analytics.py` as a pure function, with a test in
   `test_analytics.py`.
2. Write the renderable in `charts.py` if it is generic, or in `weatherpanels.py`.
3. In `DashboardScreen.compose`, add a container with the `panel` class holding a
   `Static`. Place it with the page's `grid-size`, `grid-rows` and `row-span` rules, and
   add its id to the narrow-mode height rule.
4. Fill it in `_refresh_news` or `_refresh_weather`.
5. Look at the page at 140 by 44 and at 80 by 24.

### Add another kind of event feed

Earthquakes, outages or service status would follow the weather pipeline:

1. **Ingest.** A module like `nws.py` that parses the source into records with a severity
   and a `to_article()`.
2. **Lifecycle.** An `apply_*` function that calls `Store.merge` with a `classify`
   callable returning `(severity, None)`, and uses `clear_missing` and `drop_cleared` if
   the source reports current state.
3. **Payload.** `Event.alert` is specific to the NWS. Rather than add a second field like
   it, consider generalizing it to a `payload`, and move the alert checks in `analytics`,
   the reader and `note_template` to it.
4. **App.** A `Store` with its own file name, a
   `@work(thread=True, exclusive=True, group=...)` worker, `_apply_*` and `_*_done`
   callbacks, and entries in `start_fetch`, `save` and `busy`.
5. **List.** A subclass of `EventListScreen` that implements the hooks in section 10.2,
   and a binding that opens it.
6. **Dashboard.** A page of its own. `action_toggle_page` flips between two pages and
   would need to cycle through `PAGES` instead.
7. **Tests.** A trimmed fixture, parser and lifecycle tests with `now=`, and Pilot tests
   for the list.

### Change the colors

Edit `SEVERITY_COLORS` in `worldterm/palette.py`; the acknowledged shades follow
automatically. Use hexadecimal values, keep every background distinct, run `test_styles.py`
for the contrast check, and update the color tables in the README and section 10.10.

The wall page receives the palette in its JSON, so it follows too, with one exception:
`--accent` in `wall.html`, the arrivals bars, is Warning's cyan written by hand. A level's
color must also stay readable on the page's dark surface (`--surface`, `#0f1115`), where
Critical is the tightest at 4.5:1.

### Change what the CYD shows

1. Which events: `cyd.select_critical` (level and flags; a wrapper around
   `select_headlines`, which the wall page shares) and `cyd.todays` (the day rule).
2. How a frame looks: `cyd.render_headline`, `render_idle` and `render_offline`. Rows are
   40 columns of printable ASCII; `encode` enforces it and `test_cyd.py` checks it.
3. If the JSON shape changes, bump `PROTOCOL_VERSION` in `worldterm/cyd.py` and `PROTO` in
   `cyd/main.py` together, and redeploy the board. A board on another version is refused.
4. The timing constants (`CYCLE_SECONDS`, `WRITE_GAP`, ...) are at the top of
   `worldterm/cyd.py`.

### Change what the wall shows

1. Which headlines rotate: `HEADLINE_MIN` and `WEATHER_HEADLINE_MIN` in `wall.py`; the cap is
   `HEADLINE_CAP`.
2. Which numbers: `build_snapshot`, from the pure functions in `analytics.py`. Keep the
   output plain JSON types (`test_json_safe_and_titles_verbatim` checks this).
3. How it looks: `wall.html`, one file, no external resources. Keep feed text in
   `textContent`. Check it in headless Chrome at 1920x1080 and 3840x2160
   (`google-chrome --headless=new --screenshot --window-size=1920,1080 http://127.0.0.1:8070/`).

### Tune article extraction

1. Find a page whose text comes out wrong, and look at its structure: which elements hold
   the paragraphs, and what the furniture around them is called.
2. Copy that structure into a small page in `tests/fixtures/articles/`, with invented text,
   and write the test it should pass in `tests/test_article.py`.
3. Prefer a word in `POS` or `NEG`, or a tag in `SKIP_TAGS`, to a new rule. Any change
   must keep the other fixtures passing.
4. Repeat the measurement in section 6.4 over every source, and compare with its table.

### Upgrade Textual

Check, in this order: the `EventTable` overrides (section 10.3), the context menu's
placement on its first frame (section 10.7), the editor's `Esc` binding, markup handling
(section 10.10), the whole test suite, and screenshots of both lists and both dashboard
pages at both sizes. Until that has been done for a new major version, consider capping
the dependency, for example `textual>=2.1,<3`.

## 16. Known issues and technical debt

Most serious first. Items 1 to 4 were reproduced with scripts against the code on
2026-10-10. The serious half of item 1 has since been fixed, and what remains is minor.

1. **The note editor treats two of its own labels as markup.** The *[modified]* flag in its
   title bar never appears, and the discard prompt loses its *[y]* and *[n]* key hints.
   Pass those strings as `Text(...)` or with `markup=False`. The reader had the same fault
   with text from feeds, where a headline containing *[/b]* raised `MarkupError` and ended
   the program. It was fixed on 2026-10-29, when article text arrived, and
   `test_text_that_looks_like_markup_is_shown_as_it_is` guards it.

2. **Two events can share a note file, and saving one overwrites the other.** File names
   are the first-seen date plus a slug cut at 60 characters, and `write_note` overwrites
   without checking. Two events first seen on the same day collide when their titles
   produce the same slug, which happens when the titles match or merely share their first
   60 or so characters. Saving a note on the second replaces the first one's note, and
   both events then point at the same file. This is common for alerts: in the live feed on
   2026-10-10, 163 of 507 alerts shared their title with another alert, such as two
   *Flood Warning: Pasco, FL*. To fix it, add a short hash of the GUID to new file names
   and refuse to overwrite a file that belongs to another event. Existing notes keep
   working because each event stores the path it saved.

3. **Some NWS updates lose the acknowledgment.** `apply_alerts` continues only an alert
   whose predecessor is in memory and absent from the current fetch. Three cases slip
   through:
   - The original was seen in an earlier session and superseded while worldterm was
     closed. The update arrives as a new, unacknowledged event, although `Store.transfer`
     could already read the predecessor's record.
   - One alert is superseded by several segment updates in the same fetch. Only the first
     inherits, because `transfer` moves the state rather than copying it.
   - A predecessor had already turned Clear before its update arrived. The escalation
     check compares against Clear and drops the acknowledgment.

   The first two were reproduced with the fixture; the third follows from the code. To
   fix them, look for predecessors in the records as well as in memory, and copy state to
   every alert that references the same predecessor.

4. **News events never leave a running session.** `merge` keeps events that drop out of
   their feed, and pruning only removes records that are not in memory. A session left
   open for days keeps every article it has seen, and the list, `state.json`, rebuild time
   and the dashboard grow until restart. The seven-day prune only takes effect across a
   restart. To fix it, drop or clear news events whose `last_seen` is older than a
   retention period after each merge.

5. **Full-row color depends on Textual internals, and the dependency is open-ended.** The
   row colors rely on the private `DataTable._get_row_style` (section 10.3), and
   `pyproject.toml` accepts any Textual from 2.1 on. A fresh `pip install` would
   therefore take the newest release, which has never been tested with this code.

6. **Network etiquette.** `feed.USER_AGENT` names the project's repository,
   `https://github.com/emexalt/worldterm`, as the NWS asks API clients to identify
   themselves; keep it pointing somewhere that reaches a person if the repository moves,
   or switch it to a contact address. The default `--weather us` downloads the whole
   national alert set, about 2.5 MB on 2026-10-10, at every refresh. A separate, longer
   weather interval or a narrower default area would be kinder. Opening the reader adds one
   request for the article's page, once per article per session.

7. **Saves rewrite everything, often.** `WorldTerm.save` rewrites both state files in full
   after every feed arrives and after every user action. With the default 15 feeds that is
   at least 16 full writes of each file per refresh. That is fine at today's sizes, but
   worth batching if the files grow (see item 4). `save` also stops at the first store
   that fails to write. Each save also builds the CYD and wall snapshots, a scan of both
   stores and one `json.dumps`: well under a millisecond today, but it grows with item 4.

8. **An empty but valid NWS reply would clear every alert.** A failed request clears
   nothing, but a successful reply with no features clears every event, which then
   vanishes after 30 minutes and returns as new. If the API ever behaves this way during
   an outage, ignore an empty reply that follows a large one.

9. **Interaction quirks.**
   - `q` quits the app from the weather list, because it inherits the list binding, while
     it means "back" on the dashboard and in the reader.
   - In the context menu, `c` chooses *Create note*, not *Cancel*, when the event has no
     note.
   - The weather list and the dashboard forget their sort, filter and selection between
     visits (section 10.1).
   - The dashboard is not refreshed while the help overlay covers it.
   - The header countdown restarts when a fetch finishes, but the refresh timer keeps the
     schedule it started with. After a manual refresh with `R`, the countdown overstates
     the time to the next automatic refresh.

10. **Small debts.**
    - The version is written in `pyproject.toml`, in `main.py`, and (as `worldterm/0.1`)
      in the wall server's `Server` header in `wall.py`.
    - The entry point installs a top-level module called `main`, a generic name to put in
      site-packages.
    - `App = WorldTerm` in `ui/app.py`, and the `selection`, `sort_by_time` and
      `visible_events` members of `WorldTerm`, exist only for older callers and tests.
    - `"election"` appears twice in `RULES`.
    - `WeatherScreen.cells` writes `●` directly instead of using `SELECTED_MARK`.
    - A corrupt state file is replaced silently rather than moved aside for inspection.

11. **Limits of the classifier.** Keywords are English only. Wording rather than substance
    decides the level. A *Live:* page is promoted whatever it covers, and category tags
    are consulted only when the text matches nothing.

12. **The wall page is unauthenticated and, by default, on every interface.** Anyone on the
    network can read the headlines and counts. It is read-only by construction and carries no
    notes or links, and `--wall 127.0.0.1:PORT` keeps it local, but there is no option for a
    password or TLS. The countdown drift of item 9 is visible there too.

13. **Article text is a heuristic.** `extract` finds the body on every source measured
    (section 6.4), but other pages can defeat it:
    - Behind a paywall the text stops where the free part ends. A page drawn by script
      shows nothing unless its JSON-LD carries `articleBody`.
    - A site's own lines inside an article (newsletter plugs, login prompts, "Issued on")
      are kept.
    - Text written straight into `<div>`s with `<br>`s, rather than in `<p>`s, is not found.
      Readability turns such divs into paragraphs, and `extract` could too.
    - A page made mostly of list items, such as "five things to know", can fall under
      `MIN_TEXT`, which counts paragraphs only, and keep the summary.
    - A page served to worldterm can differ from what a browser gets: a consent wall would
      be extracted as if it were the article.

14. **The CYD's Bluetooth is open.** Anyone within range can connect to the board and put
    text or frames on its screen; there is no pairing (the owner's choice, 2026-10-30).
    Nothing can be read from the board this way. Pairing with a bond, accepted only for a
    short while after a tap on the screen, would close it; the board's stack supports bonds,
    and the host would then set `BT_SECURITY` on its socket. Also: worldterm finds a BLE
    board only by address, the host side needs Python 3.14 or newer, and a phone beside
    worldterm on the same board has not been tried. A pairing made in the desktop's
    Bluetooth settings takes the board's ATT channel and locks worldterm out (found
    2026-10-30); worldterm reports it, but cannot share the link, because Linux gives an LE
    link one ATT channel and the alternative, BlueZ's D-Bus API, needs a library.

## 17. Design decisions

**D1. A standard-library data layer.** The project started on a machine without `pip`,
where `feedparser` could not be installed. Parsing uses `urllib`, `xml.etree`, `html.parser`
and `json`. The cost is less tolerance of broken feeds than `feedparser` offers. The gains
are no installation, fast tests and a data layer any front end can use.

**D2. Textual, after a first version in curses.** The first version drew everything with
curses. Once Textual had been installed from the system packages, the UI was rewritten on
it and the data layer was left untouched, which is why the layering in section 3 is
strict. Textual brought CSS layout, modal screens, an editor widget and headless tests, at
the price of one dependency on its internals (section 10.3).

**D3. Hexadecimal colors.** Textual's theme redraws ANSI color names, showing yellow as
orange and magenta as red. That made Major and Minor, and Critical and Indeterminate,
impossible to tell apart. Hexadecimal values bypass the theme.

**D4. Keyword rules for news, with recorded reasons.** Rules work offline, give the same
answer every time, cost almost nothing and can be tested exactly. `MatchDetails` records
why each item got its level, so the dashboard can audit the rules. A statistical or
language-model classifier would need models or network access, and could not be tested by
exact assertions.

**D5. Whole-word exceptions only where measured.** Prefix matching handles inflections for
free but over-matches. Rather than make every keyword a whole word, which would mean
listing every inflection, only the stems that measurably misfired on live feeds were
restricted. The Triggers panel is how the next one will be found.

**D6. Alert severity from the NWS.** An alert carries an authoritative severity, so alerts
bypass the keyword rules through the `classify` parameter of `Store.merge`.

**D7. One store class, one file per list, shared notes.** Both lists get the same tally,
flags and persistence from one class. Separate files keep the news pruning and weather
clearing policies apart, and let either file be deleted on its own.

**D8. Clear first, forget later.** A Netcool operator sees a fault turn green before it
disappears. Alerts that leave the feed stay in the list as Clear for 30 minutes for the
same reason.

**D9. An update continues its alert.** About half of all alert messages are updates: 259
of 507 on 2026-10-10. Without continuation, each would appear as a new, unacknowledged
row. An escalation removes the acknowledgment, because the operator acknowledged
something less severe.

**D10. View state belongs to the list screen.** Sort, filter and selection live on each
`EventListScreen`, so the lists are independent and a batch action cannot cross between
them. The cost is that a recreated screen forgets its view state.

**D11. One dashboard screen with two pages.** The pages share bindings, layout code and
chart helpers, and `w` switches pages just as it switches lists.

**D12. Workers fetch; the UI thread merges.** The stores need no locks, because every
change to a store happens on the event loop (section 4.2). The only locks in the program
guard the hand-off of a snapshot to the CYD worker and to the wall server, and each is held
just long enough to swap one reference.

**D13. Tally counts presence, not repetition.** In Netcool, the tally counts duplicate
events folded into one row. Feeds do not repeat items, so presence in a refresh is the
nearest equivalent. The tally therefore measures how long an item has been in its feed,
not how important it is.

**D14. The CYD display is a dumb terminal, driven with the standard library.** The board
program draws rows of text it is sent and answers; choosing, wrapping, timing and the
hot-plug state machine are host-side pure functions and a service that the tests drive
with a fake port and a fake clock. The serial port is opened with `termios`, `fcntl` and
`select` and found through `/sys` rather than with pyserial, to keep principles 1 and 2 and
the no-install property of D1; the cost is that only Linux hosts can find the board.
Acknowledging stays in worldterm: the board reports taps, and nothing acts on them yet.

**D15. The wall is a static page over a cached JSON snapshot.** The page rotates and counts
down by itself, so the server has nothing to do but hand out bytes; that is what lets it be a
plain `http.server` on a daemon thread with no state of its own, and read-only by construction
(two `GET` paths, nothing else). HTTP/1.0 keeps every connection short-lived so quitting never
waits. The page uses the same hex palette as the terminal (`palette.py`), checked against the
page's dark surface: every level clears 4.5:1, the adjacent levels stay apart under simulated
colour-blindness, and every colour carries a text label. The acknowledged shades of the terminal
are not used on the wall, where they would fall below 2:1 on a projector; acknowledged counts
are shown as numbers instead.

**D16. Article text with the standard library, fetched when read.**
- *No library.* An extractor library such as trafilatura or readability-lxml would be more
  thorough, but it brings lxml and more, against principles 1 and 2 and D1's no-install
  property. `article.py` is instead a small scorer, and every rule in it was needed on a
  measured source and is pinned by a fixture (section 6.4). When a site changes, the fix is
  usually a word in `POS` or `NEG`.
- *Fetched when read.* Pages are fetched only when a reader opens, never ahead, because
  fetching every headline would mean hundreds of page requests per refresh.
- *Kept in memory.* Pages are cached only in memory, so nothing new is written or has to be
  pruned.
- *A daemon thread.* The download runs on a daemon thread rather than a Textual worker, so
  quitting never waits for it (section 4.6).

**D17. Bluetooth LE through a socket, with the serial line protocol on top.**
- *Nordic UART Service.* The board offers NUS, the de facto "serial port over BLE", rather
  than a GATT service of worldterm's own. The line protocol then crosses unchanged, the
  service needs four settings rather than a second implementation, and phone apps that
  speak NUS can send the board text with no worldterm code.
- *No library.* The usual Python route is bleak, which brings dbus-fast and drives BlueZ
  over D-Bus. Python 3.14's Bluetooth sockets take an LE address type and a channel, so an
  ATT client on the L2CAP socket for CID 4 is about 200 lines and needs no root; tried on
  2026-10-30 before it was written (connect 0.45 s, a 1.2 KB frame in 0.45 s). The costs:
  Linux only, Python 3.14 or newer, no scanning (the board shows its address instead), and
  one connection per computer and board.
- *Text is any line that is not a command.* So a phone app or `echo` works without a
  prefix. A message stays 20 s, then worldterm's last frame is redrawn by the board itself,
  which keeps it; a new frame ends the message at once.

## 18. Glossary

**Netcool/OMNIbus.** IBM's fault-management event console, the model for worldterm.

**Event.** One row in a list: an article or an alert, plus its state.

**Node.** Where an event came from: a news source, or an NWS office.

**Tally.** How many refreshes have contained the event (section 9.2).

**Acknowledge.** Mark an event as seen and owned without removing it.

**Journal.** Netcool's notes on an event. Here, a Markdown file.

**Clear.** Level 0, meaning the fault has gone away. In worldterm, an alert that has left
the feed.

**Lamp.** A colored counter of events at one level.

**CAP.** The Common Alerting Protocol, the OASIS standard that NWS alerts follow. It
defines severity, urgency, certainty, response and message type.

**Urgency.** How soon to act: Immediate, Expected, Future, Past or Unknown.

**Certainty.** How sure the forecaster is: Observed, Likely, Possible, Unlikely or
Unknown.

**Response.** What people should do: Shelter, Evacuate, Prepare, Execute, Avoid, Monitor,
Assess, AllClear or None.

**Message type.** Alert for a new alert, Update for one that replaces the alerts in its
`references`, and Cancel for a withdrawal.

**Segment.** One part of an alert that was split by area. Each segment is an alert with
its own id.

**UGC.** Universal Geographic Code. `SSCNNN` names a county and `SSZNNN` a forecast zone,
where `SS` is a state or marine-area prefix.

**VTEC.** Valid Time Event Code, a machine-readable string naming the office, phenomenon,
significance and event number. worldterm stores it in `Alert.vtec` but does not use it
yet.

**Office.** The NWS Weather Forecast Office that issued an alert.

**Pilot.** Textual's driver for headless tests.

**Worker.** A background task managed by Textual. Here, always a thread.

**CYD.** The "Cheap Yellow Display", an ESP32-2432S028 board with a 320x240 LCD, which
worldterm can use as a desk display for today's Critical headlines (section 4.4).

**Wall.** The read-only web page for a big screen that worldterm serves (section 4.5).

**ATT.** The Attribute Protocol, the request-and-response protocol under GATT on a
Bluetooth LE link. `bleport.py` speaks it directly.

**NUS.** The Nordic UART Service, a GATT service with an RX characteristic to write to and
a TX characteristic that notifies: the common way to carry a byte stream over Bluetooth LE.

**JSON-LD.** Structured data that a page embeds in `<script type="application/ld+json">`.
News pages describe themselves there as a schema.org `NewsArticle`, and some include the
whole text as `articleBody` (section 6.4).

**Readability.** Mozilla's method, behind Firefox Reader View, for finding the main text of
a page by scoring paragraphs and the elements around them. `article.py` uses its scoring
(section 6.4).
