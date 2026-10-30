#!/usr/bin/env python3
"""worldterm - present RSS news as a Netcool-style event list in the terminal."""
from __future__ import annotations

import argparse
import sys
from functools import partial

from worldterm import article, cyd, nws, sources, wall
from worldterm.store import default_data_dir
from worldterm.ui.app import WorldTerm



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="worldterm",
        description="Netcool-style event console for RSS news feeds.",
        epilog="Keys: ↑↓ move · Enter/right-click menu · a ack · r read · n note · R refresh · ? help · q quit",
    )
    p.add_argument("--feed", "-f", action="append", metavar="[LABEL=]URL", type=sources.parse_feed_arg,
                   help="RSS/Atom feed URL, optionally with a short Node label "
                        "(repeatable; http(s) or file://). Overrides --preset.")
    p.add_argument("--preset", "-p", default=sources.DEFAULT_PRESET, metavar="NAME[,NAME...]",
                   help=f"source or group of sources, comma-separated (default: {sources.DEFAULT_PRESET}); "
                        "see --list-presets")
    p.add_argument("--list-presets", action="store_true", help="list available sources and groups, then exit")
    p.add_argument("--interval", "-i", type=int, default=300, metavar="SECONDS",
                   help="refresh interval in seconds (default: 300, minimum 15)")
    p.add_argument("--timeout", type=float, default=15.0, metavar="SECONDS",
                   help="HTTP timeout for each feed, the alerts and each article page (default: 15)")
    p.add_argument("--data-dir", metavar="DIR", default=None,
                   help=f"where state.json and notes/ live (default: {default_data_dir()})")
    p.add_argument("--weather", "-w", default=nws.DEFAULT_AREA, metavar="AREA",
                   help="NWS alerts to show on the weather screen: 'us' for every active alert (default), "
                        "'off', or state / marine area codes such as MA,RI,AN")
    p.add_argument("--weather-url", metavar="URL", default=None,
                   help="fetch alerts from this URL instead (api.weather.gov GeoJSON, or file://)")
    p.add_argument("--cyd", default="auto", metavar="auto|off|PATH|ble:ADDRESS",
                   help="Cheap Yellow Display: 'auto' uses one whenever it is plugged in over USB "
                        "(default), 'off', a serial port such as /dev/ttyUSB0, or ble:ADDRESS for one "
                        "reached over Bluetooth LE (its address is on its screen)")
    p.add_argument("--wall", default=str(wall.DEFAULT_PORT), metavar="PORT|HOST:PORT|off",
                   help=f"read-only wall dashboard for a big screen, served at http://<this host>:{wall.DEFAULT_PORT}/ "
                        "on every interface (default); HOST:PORT to bind one address; 'off' for no page")
    p.add_argument("--version", action="version", version="worldterm 0.1.0")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_presets:
        print(sources.describe_presets())
        return 0
    if args.feed:
        feeds = list(args.feed)
    else:
        try:
            feeds = sources.resolve(args.preset)
        except KeyError as exc:
            parser.error(f"unknown preset {exc.args[0]!r}; choose from: {', '.join(sources.preset_names())}")
    try:
        nws.alerts_url(args.weather)
    except ValueError as exc:
        parser.error(f"--weather: {exc}")
    if args.cyd not in ("auto", "off"):
        reason = cyd.unavailable_reason(args.cyd)
        if reason:
            parser.error(f"--cyd {args.cyd}: {reason}")
    try:
        wall.parse_wall(args.wall)
    except ValueError as exc:
        parser.error(f"--wall: {exc}")
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        print("worldterm: needs an interactive terminal (stdin/stdout must be a TTY)", file=sys.stderr)
        return 2
    WorldTerm(feeds, interval=args.interval, data_dir=args.data_dir, timeout=args.timeout,
              weather=args.weather, weather_url=args.weather_url, cyd=args.cyd, wall=args.wall,
              article_loader=partial(article.load, timeout=args.timeout)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
