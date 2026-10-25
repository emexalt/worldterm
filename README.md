# worldterm

A terminal "event console" for news. RSS articles are shown the way IBM Netcool/OMNIbus
shows SNMP traps: a dense, color-coded list where every story is an event with a
**severity**, a **node** (the news source), a **summary**, a **tally** and
**first / last occurrence** timestamps. Right-click a row to **Acknowledge**, **Read**,
or **Create note**. A second screen shows **National Weather Service alerts** the same way,
and a dashboard charts both. Outside the terminal, worldterm can show today's Critical
headlines on a [desk display](#cyd-display) plugged in over USB, and serves a read-only
[wall dashboard](#wall-dashboard) for a big screen or projector.

Built on [Textual](https://textual.textualize.io/). The feed parser, classifier, state store,
the desk display's serial link and the wall page's web server are pure standard library.

Maintainers: the architecture, data flow, extension recipes and known issues are described in
[docs/DESIGN.md](docs/DESIGN.md).

```
 WORLDTERM  Node: BBC News  Events: 35   CRT 12  MAJ 8  MIN 6  WRN 9  IND 0   WX 23   CYD   WALL :8070    Refreshed 03:21:20  next 4:12
 Sev  A R N Node         Summary                                                       Tally First       Last
 CRT        BBC News     Powerful magnitude 7.7 earthquake hits Panama, damaging …         3 03:06:12    03:21:20
 CRT  A R   BBC News     Life-threatening Hurricane Isaias makes landfall in Florida       3 03:06:12    03:21:20
 MAJ      N BBC News     US unveils sanctions on ICC in move court condemns as …           3 03:06:12    03:21:20
 MIN        BBC News     Crazy Rich Asians to return as TV show with original …            3 03:06:12    03:21:20
 WRN        BBC News     Navi Pillay, former UN human rights chief, wins Nobel …           3 03:06:12    03:21:20
 Enter/right-click menu  a ack  r read  n note  o open  R refresh  s sort  f filter  x hide-acked  ? help  q quit
```

## Run

```sh
python3 main.py                          # every world-news source (preset "all", default)
python3 main.py --preset bbc             # one source
python3 main.py --preset europe,cnbc     # combine groups and sources with commas
python3 main.py --list-presets           # show every source and group
python3 main.py --feed https://example.org/rss.xml --feed Wire=file:///path/to/local.xml
python3 main.py --interval 120           # refresh every 2 minutes (default 300)
python3 main.py --data-dir ./state       # keep state.json and notes/ somewhere else
python3 main.py --weather MA,RI,CT       # NWS alerts for these states only (default: us, all of them)
python3 main.py --weather off            # no weather screen
python3 main.py --cyd off                # never look for a Cheap Yellow Display (default: auto)
python3 main.py --cyd /dev/ttyUSB1       # the display is on this port
python3 main.py --wall off               # no wall dashboard page (default: http://<this host>:8070/)
python3 main.py --wall 127.0.0.1:9000    # serve the wall page to this machine only
```

Requires Python ≥ 3.10, Textual ≥ 2.1 (`apt install python3-textual` or
`pip install textual`) and a terminal with mouse support. The optional
[CYD display](#cyd-display) needs nothing more, but only works from Linux. The
[wall dashboard](#wall-dashboard) needs only a web browser on the screen that shows it.

## Sources and presets

Sources favor wire-style reporting, public broadcasters and international outlets.
Each one shows a short name in the Node column. Feeds are fetched in parallel, so one
slow or unreachable feed doesn't hold up the rest. Its error appears as a notification.

| Group          | Sources                                                      |
|----------------|--------------------------------------------------------------|
| `all` (default)| every source in `us`, `europe`, `asia-pacific` and `intl`    |
| `us`           | NPR, PBS NewsHour, ABC News, CBS News, Christian Science Monitor |
| `europe`       | BBC World, Deutsche Welle, France 24, RFI, Euronews          |
| `asia-pacific` | ABC Australia, The Japan Times, South China Morning Post     |
| `intl`         | BBC World, Deutsche Welle, France 24, Al Jazeera English, UN News |
| `business`     | BBC Business, NPR Business, CNBC International               |
| `science`      | BBC Science, NPR Science, BBC Technology                     |

Every source is also a preset on its own: `bbc`, `bbc-top`, `npr`, `npr-world`,
`pbs`, `abc-us`, `cbs`, `csmonitor`, `dw`, `france24`, `rfi`, `euronews`,
`aljazeera`, `abc-au`, `japantimes`, `scmp`, `un`, `bbc-business`, `npr-business`,
`cnbc`, `bbc-tech`, `bbc-science`, `npr-science`. The catalog lives in
`worldterm/sources.py`. With `--feed`, prefix the URL with `LABEL=` to set its Node name.
Otherwise the feed's own title is used.

## Keys and mouse

| Key               | Action                                             |
|-------------------|----------------------------------------------------|
| `↑` `↓` `j` `k`   | move the cursor                                    |
| `PgUp` `PgDn` `Home` `End` | page / jump                               |
| **right-click** / `Enter` / `m` | context menu: Acknowledge · Read · Create/Edit note · Open link |
| `a`               | acknowledge / unacknowledge the selected rows, or the cursor row if none are selected |
| `Space`           | select / deselect the cursor row and move down     |
| `Shift+↑` `Shift+↓` | extend the selection                             |
| `Ctrl+A`          | select every visible row (press again to deselect all) |
| `Esc`             | clear the selection                                |
| **Ctrl+click** / **Shift+click** | toggle one row / select a range from the cursor |
| `r`               | read the article (summary, metadata, link)         |
| `n`               | create or edit the note for this article           |
| `o`               | open the article link in your browser              |
| `R`               | refresh feeds now                                  |
| `s`               | toggle sort: severity-then-time ↔ time only        |
| `f`               | cycle severity filter: all → ≥Minor → ≥Major → Critical |
| `x`               | hide / show acknowledged events                    |
| `d` / click the header | dashboard: news and weather analytics (`w` or `Tab` switches page) |
| `w` / click the **WX** lamp | weather screen: NWS alerts as events (`w` or `Esc` returns) |
| `?`               | help                                               |
| `q`               | quit                                               |

Flag columns: **A** acknowledged · **R** read · **N** has a note. Events that arrived
since you started are bold until you act on them. Status messages appear as toast
notifications in the corner.

### Batch acknowledge

Selected rows show a `●` in the first column and are underlined, and the header shows
how many are selected. Pressing `a`, or right-clicking inside the selection and choosing
**Acknowledge N selected**, acknowledges them all. If every selected row is already
acknowledged, it unacknowledges them instead, so pressing `a` twice undoes a batch.
The selection stays until you press `Esc` or choose **Deselect all**. Right-clicking a
row outside the selection clears it and acts on that row alone. Rows hidden by a filter
are dropped from the selection, so a batch never touches events you can't see.

Some terminals reserve Shift+click for their own text selection. Use `Shift+↑`/`↓`
there.

## Weather alerts

Press `w`, or click the **WX** lamp in the header, for a second event list fed by the
National Weather Service's active-alerts API (`api.weather.gov/alerts/active`). Every
watch, warning, advisory and statement is a row with the same colors, flags, selection,
right-click menu, reader and notes as the news list, but its own state file
(`weather.json`), sort, filter and selection. `w` or `Esc` (with nothing selected) returns
to the news.

```
 WEATHER  Alerts: 507  Offices: 76  States: 41   CRT 0  MAJ 88  MIN 143  WRN 263  IND 13  CLR 0   Refreshed 10:45:39  next 4:59
 Sev  A R N Office          Summary                                                 Tally  Issued      Expires
 MAJ        Pendleton OR    Freeze Warning: Central Oregon                              1  10:28:00    12:00:00
 MAJ        Quad Cities I…  Flood Warning: Clinton, IA; Scott, IA                       1  10:19:00    10-13 01:48
 MIN        Marquette MI    Gale Warning: Huron Islands to Marquette MI; Marquette…     1  11:13:00    10-11 02:00
 WRN        Green Bay WI    Small Craft Advisory: Rock Island Passage to Sturgeon …     3  10:15:00    10-11 13:00
```

The alert supplies everything the row needs, so no keyword rules are involved:

| NWS severity | Netcool level | Typical alerts                                          |
|--------------|---------------|---------------------------------------------------------|
| Extreme      | Critical      | Tornado Warning, Hurricane Warning, Flash Flood Emergency |
| Severe       | Major         | Flood Warning, Winter Storm Warning, Red Flag Warning, Flood Watch |
| Moderate     | Minor         | Gale Warning, Wind Advisory, Rip Current Statement      |
| Minor        | Warning       | Small Craft Advisory, Frost Advisory, Coastal Flood Advisory |
| Unknown      | Indeterminate | Air Quality Alert, Hydrologic Outlook                   |
| *(gone from the feed)* | Clear | the alert expired or was canceled                    |

**Node** is the issuing office (the sender without its `NWS` prefix), **Summary** is the
event type and the areas it covers, **Issued** is the alert's own timestamp and
**Expires** when it ends. Netcool semantics carry over:

- An alert that drops out of the active feed turns **Clear** (green) and stays for 30
  minutes, so you see what ended, then disappears.
- An NWS *Update* is a new message that references the one it replaces. The row keeps
  the old alert's acknowledgment, read flag, note, first-seen time and tally, so an
  alert that is extended five times is one event with a tally of six, not six rows. If
  the update raises the severity, the acknowledgment is dropped so it asks for attention
  again.
- The main header's **WX** lamp shows the number of active alerts in the color of the
  worst one.

`--weather` takes `us` (default: every active alert, around 500 at any time), `off`, or
a comma-separated list of state and marine-area codes (`MA,RI,AN`). Alerts refresh on
the same interval as the feeds. The reader shows the NWS headline, urgency, certainty,
response, areas, onset and end times, the full description and the preparedness
instructions. The parser is `worldterm/nws.py`, stdlib only; `--weather-url` points it
at another GeoJSON source (or a `file://` for testing).

## Dashboard

Press `d` (or click the header bar) for a view of the whole event set, not just the
filtered list. The dashboard has two pages, **News** and **Weather**; `w` or `Tab` switches,
and it opens on the page matching the list you came from.

**News page:**

- **By severity** - one bar per level, scaled to the largest. The bright part of the
  bar is outstanding events, the darker part is acknowledged ones.
- **By source** - a matrix of sources against severities, with each count drawn as a
  Netcool-style lamp in its color, the acknowledged percentage, and a stacked bar of
  the source's mix. `s` cycles the sort: total, critical, outstanding, name.
- **Triggers** - which classifier keywords are firing and how often. *Events* is the
  number of articles containing the keyword. *Decided* is how many of those it actually
  set the severity for; the rest were outranked by a keyword from a higher level (the
  darker part of the bar). Above the table: how severities were assigned overall
  (keyword, category tag, no match, no text), how often the Live/Breaking and
  Analysis/Opinion modifiers applied, and per rule level how many keywords fired. `t`
  cycles the sort: events, decided, level.

**Weather page**, what the alerts can be aggregated by and how it is drawn:

- **Alerts by severity** - one bar per NWS severity, labeled with the Netcool level it
  maps to (*Severe → Major*), outstanding bright and acknowledged dark, plus the split
  into warnings, watches, advisories, statements and outlooks.
- **By state / by office** - the same lamp matrix as the news page, grouped by the
  states (and marine areas) an alert's zones fall in, or by the issuing forecast office
  (`o` toggles). An alert covering several states counts in each. `s` cycles the sort.
- **Event types** - which alert types are firing and how often: count, the level most
  of them carry, how many states each reaches, and a stacked bar of its severity mix.
  `t` cycles the sort: count, level, name.
- **Timing and urgency** - a 24-hour sparkline of when the active alerts were issued
  (peak per hour, plus how many are older); how soon they end (ended, within 1h, 6h,
  24h, later) as bars colored hot-to-cold; a cross-tab of CAP urgency against certainty
  (Immediate/Observed is the corner to watch); the recommended responses (Shelter,
  Evacuate, Prepare, Avoid, Monitor...); and how many of the messages are updates of
  earlier ones, which shows how much the picture is churning.

Both pages update in place when a refresh lands. `Esc`, `q`, `d` or a right-click
returns to the event list. The numbers come from `worldterm/analytics.py`, which is
plain Python over the event store, so they can be reused outside the UI.

## CYD display

worldterm can drive a "Cheap Yellow Display" (an ESP32-2432S028 board with a 320x240 LCD,
running MicroPython) over USB as a desk display: **today's unacknowledged Critical
headlines**, news and weather alike, one at a time, eight seconds each, with the board's
LED red while one is on screen. Acknowledging a headline in worldterm takes it off the
display within a second; undoing the acknowledgment brings it back. When there is nothing
to show, the display says so.

```
 CRITICAL 2/4            Fri 24 Oct 21:30
 BBC News                          20:58
 Life-threatening Hurricane Isaias makes
 landfall in Florida

 4 unacknowledged critical today
```

Nothing needs configuring. With the default `--cyd auto`, worldterm looks every two seconds
for a USB serial port that belongs to an ESP32 board and asks it whether it is running the
display program. Plug the board in at any time and the `CYD` chip in the header lights up;
pull it out and the chip dims and worldterm carries on as before. `--cyd /dev/ttyUSB1`
names the port instead, and `--cyd off` never opens a serial port. Finding the board uses
`/sys`, so it works on Linux only.

Deploying the display program to the board, sharing the port with mpremote, permissions
and the wire protocol are described in [cyd/README.md](cyd/README.md).

## Wall dashboard

worldterm also serves a **read-only web page made for a big screen or a projector** at
`http://<this host>:8070/` while it runs. It shows the unacknowledged **Critical and Major**
headlines one at a time, eight seconds each, with a ticker of the newest items of every level
underneath, and four panels of statistics: unacknowledged counts per level, inbound events per
hour over the last 24 hours, the mix of levels per source, and a summary of active weather
alerts when weather is on. Nothing can be acknowledged or changed from the page: the server
answers plain `GET` requests for the page and its JSON and refuses everything else.

```
 WORLDTERM          30 unacknowledged of 35 events from 1 source   refreshed 21:30 · next in 4:12   21:31
 ┌─────────────────────────────────────────────┐  ┌──────────┬──────────┬──────────┬──────────┐
 │                                             │  │ 11       │ 12       │ 4        │ 3        │
 │ Critical  BBC News  21:12 · 19 min ago      │  │ CRITICAL │ MAJOR    │ MINOR    │ WARNING  │
 │                                             │  │ of 13    │ of 12    │ of 6     │ of 4     │
 │ Life-threatening Hurricane Isaias           │  └──────────┴──────────┴──────────┴──────────┘
 │ makes landfall in Florida                   │  INBOUND EVENTS, LAST 24 HOURS
 │                                             │  ▂▃▅▂▁▃▇▅▂▁▂▃▅▂▁▃▇▅▂▃▄▂▁█
 │ 1 / 23 unacknowledged                       │  peak 9/h · 35 in 24 h · 0 older
 │ ━━━━━━━━━━━━━━━━━━━━━━━──────────────────── │  BY SOURCE
 ├─────────────────────────────────────────────┤  BBC News     ████████▓▓▓▓▓▓▒▒░░░   35
 │ CRT  BBC News  Trump announces deal ... now │  WEATHER ALERTS
 │ MAJ  BBC News  US unveils sanctions ... 1 h │  23 active · 3 immediate · 18 offices · 16 states
 │ ...                                         │  8 Warnings · 2 Watches · 8 Advisories ...
 └─────────────────────────────────────────────┘
```

The page is one self-contained file (`worldterm/wall.html`) with no external resources, so it
works on a screen with no internet access. It polls worldterm every five seconds, counts down
to the next refresh itself, and keeps showing the last data with a banner if worldterm goes
away. Text scales with the screen, so 4K is 1080p at twice the size. Headline text is written
as text, never as markup.

By default the page is served on every network interface, so any device on your LAN can open
it (nothing on it can change anything, and it carries no notes or links). On a laptop that
joins other people's networks, bind it to this machine with `--wall 127.0.0.1:8070`, or turn
it off with `--wall off`. If the port is already taken, worldterm says so in a toast and
carries on without the page. The `WALL :8070` chip in the header is lit while the page is
served.

## Severity (importance)

Netcool levels, assigned by keyword rules over the headline and summary
(`worldterm/severity.py`, table `RULES`, easy to tune):

| Level | Color | Acknowledged | Examples of triggers                          |
|-------|--------|--------------|-----------------------------------------------|
| Critical | red `#f4005f` | `#6e002b` | war, missile, killed, explosion, earthquake, hurricane, coup, outbreak, crash |
| Major | orange `#ff8700` | `#733d00` | election, sanctions, ceasefire, tariff, court ruling, protest, flood, strike |
| Minor | yellow `#ffd700` | `#736100` | sport, film, music, royal, festival, award, travel |
| Warning | cyan `#58d1eb` | `#285e6a` | anything that matches no rule |
| Indeterminate | purple `#af5fd7` | `#4f2b61` | item with no readable text |

Acknowledged rows keep their hue at 45% brightness with light gray text. The colors are
`SEVERITY_COLORS` in `worldterm/palette.py`, and the darker shades are derived from them. The
wall dashboard uses the same colors (but not the darker shades, which a projector washes out).

A keyword only counts once per article, and the most severe level with any match
wins. Critical and Major keywords match as prefixes (*kill* also catches *killed*),
except *war*, *coup*, *dead*, *elect*, *strike* and *sue*, which match whole words only
because their prefixes fired on *warning*, *couple*, *deadlock*, *electric*, *striker*
and *Suez*. The dashboard's trigger table is the place to spot the next such case. Modifiers: a headline starting with *Live* or *Breaking* is bumped up one level;
*Analysis*, *Opinion*, *Explainer*, *Watch*, or a *Why/What/How …?* question is bumped
down one level (never below Warning).

## Notes

**Create note** opens an in-app editor buffer pre-filled with the article's slug and
metadata:

```
# powerful-magnitude-7-7-earthquake-hits-panama-damaging-buildings

Title:    Powerful magnitude 7.7 earthquake hits Panama, damaging buildings
Source:   BBC News
Severity: Critical
Date:     2026-10-10T02:51+01:00
Link:     https://www.bbc.co.uk/news/articles/…

▌
```

The editor is Textual's `TextArea` (soft wrap, undo with `Ctrl+Z`, selection with
Shift+arrows). `Ctrl+S` or `F2` saves; `Esc` cancels (asks if modified); `Ctrl+E` hands
the buffer to `$VISUAL`/`$EDITOR` and reloads it when that editor exits.

Notes are saved as `notes/YYYYMMDD-<slug>.md` under the data directory
(default `~/.local/share/worldterm/`, or `$XDG_DATA_HOME/worldterm/`). Choosing
**Edit note** on an article that already has one reopens the saved file.

## State

`state.json` in the data directory records per-article acknowledged / read / note /
tally / first and last seen; `weather.json` does the same for alerts. Writes are atomic. Articles that have dropped out of the
feed are pruned after 7 days. Articles already seen keep their tally and flags across
restarts, so the console behaves like a persistent event list rather than a reader
that forgets.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

`tests/test_ui.py` drives the Textual app headlessly (keyboard, right-click menu,
note editor, weather screen, both dashboard pages) via `App.run_test()`; the rest cover
feed and alert parsing, classification, aggregation and state. `tests/fixtures/nws_sample.json`
is a trimmed capture of the live alerts API (one Tornado Warning in it is synthetic, so the
Extreme level is exercised). `tests/test_cyd.py` and `tests/test_serialport.py` cover the
CYD display with a scripted fake board and a pseudo-terminal; no test opens a real serial
port, so the suite can run with the board plugged in. `tests/test_wall.py` covers the wall
page's snapshot and server on a loopback port chosen by the kernel.

## License

MIT licensed; see [LICENSE](LICENSE).
