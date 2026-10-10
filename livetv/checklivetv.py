#!/usr/bin/env python3
"""
Omnivue Live TV checker.

Run it from the folder that holds channels.json and guide.json:
    python check_livetv.py

Or point it at the files yourself:
    python check_livetv.py path/to/channels.json path/to/guide.json

Times can be written two ways, both meaning an exact instant:
    "2026-10-10T09:00:00+03:00"   (ISO text with an offset)
    1791619200                    (epoch SECONDS, a plain number, no quotes)
All times in this report are shown in Kenya time (EAT, +03:00).

ERROR   = something that will break or confuse the app. Fix before pushing.
WARN    = suspicious, worth a look, but the app can live with it.
Exit code is 1 if any ERROR was found, otherwise 0.
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

MIN_HOURS_AHEAD = 24  # warn when a channel's guide runs out sooner than this
ID_STYLE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # lowercase-with-hyphens
KENYA = timezone(timedelta(hours=3))  # EAT, no daylight saving
EPOCH_MIN = 946684800    # 2000-01-01, anything smaller is not a believable guide time
EPOCH_MAX = 4102444800   # 2100-01-01

errors = []
warnings = []


def err(where, msg):
    errors.append(f"ERROR  {where}: {msg}")


def warn(where, msg):
    warnings.append(f"WARN   {where}: {msg}")


def fmt(t):
    """Always show Kenya time so epoch and ISO entries read the same."""
    return t.astimezone(KENYA).strftime("%a %d %b %H:%M")


def load(path, required):
    if not path.exists():
        if required:
            err(path.name, "file not found")
        else:
            print(f"(no {path.name} found, skipping guide checks)")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        err(path.name, f"invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}")
        return None


def parse_time(value):
    """Accepts epoch seconds (a number) or ISO text with an offset.
    Returns (datetime, None) on success or (None, reason) on failure."""
    if value is None or isinstance(value, bool):
        return None, "is missing or not a time"
    if isinstance(value, (int, float)):
        if value >= 10 ** 11:  # 13 digits means milliseconds
            return None, f"{value} looks like milliseconds. Use seconds (10 digits)"
        if not (EPOCH_MIN <= value <= EPOCH_MAX):
            return None, f"{value} is not a believable epoch-seconds time"
        return datetime.fromtimestamp(value, timezone.utc), None
    if not isinstance(value, str):
        return None, "must be epoch seconds or text like 2026-10-10T09:00:00+03:00"
    if value.strip().isdigit():
        return None, f"'{value}' is a number in quotes. Remove the quotes to use epoch seconds"
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, f"'{value}' is not a valid ISO time"
    if t.tzinfo is None:
        return None, f"'{value}' has no timezone offset (add +03:00)"
    return t, None


def check_channels(channels):
    """Checks channels.json. Returns {id: channel} (first occurrence of each id)."""
    if not isinstance(channels, list):
        err("channels.json", "root must be an array [ ... ]")
        return {}

    by_id = defaultdict(list)
    url_users = defaultdict(set)

    for i, ch in enumerate(channels, 1):
        if not isinstance(ch, dict):
            err(f"channel #{i}", "is not an object")
            continue

        cid, name = ch.get("id"), ch.get("name")
        label = f"channel #{i} ({cid if cid else name})"

        if not isinstance(cid, str) or not cid.strip():
            err(label, "missing id")
            continue
        by_id[cid].append(ch)

        if not ID_STYLE.match(cid):
            warn(label, f"id '{cid}' is not lowercase-with-hyphens")
        if not isinstance(name, str) or not name.strip():
            warn(label, "missing name")
        elif name != name.strip():
            warn(label, f"name {name!r} has leading/trailing spaces")

        status = ch.get("status")
        if status not in ("active", "inactive"):
            warn(label, f"unexpected status {status!r}")

        streams = ch.get("streams", [])
        if not isinstance(streams, list):
            err(label, "streams must be an array")
            streams = []

        has_url = False
        priorities = []
        for s in streams:
            if not isinstance(s, dict):
                err(label, "a stream entry is not an object")
                continue
            url = s.get("url")
            if isinstance(url, str) and url.strip():
                has_url = True
                url_users[url.strip()].add(cid)
            priorities.append(s.get("priority"))
        if len(priorities) != len(set(priorities)):
            warn(label, "two streams share the same priority")

        if status == "active" and not has_url and not ch.get("youtubeId"):
            warn(label, "is active but has no stream URL (set status to inactive)")

    for cid, group in by_id.items():
        if len(group) > 1:
            names = ", ".join(repr(c.get("name")) for c in group)
            err(f"id '{cid}'", f"used {len(group)} times: {names}")

    for users in url_users.values():
        if len(users) > 1:
            warn("streams", "same stream URL shared by: " + ", ".join(sorted(users)))

    return {cid: group[0] for cid, group in by_id.items()}


def check_guide(guide, known):
    if not isinstance(guide, list):
        err("guide.json", "root must be an array [ ... ]")
        return

    now = datetime.now(timezone.utc)
    seen = set()
    coverage = []  # (channel name, last end time)

    for i, entry in enumerate(guide, 1):
        if not isinstance(entry, dict):
            err(f"guide entry #{i}", "is not an object")
            continue

        gid = entry.get("id")
        if not isinstance(gid, str) or gid not in known:
            err(f"guide entry #{i}", f"id {gid!r} does not match any channel in channels.json")
            continue

        label = f"guide '{gid}'"
        if gid in seen:
            err(label, "appears more than once in the guide")
        seen.add(gid)

        channel = known[gid]
        if channel.get("status") != "active":
            warn(label, "channel is not active, its guide is never shown")

        gname = entry.get("name")
        if isinstance(gname, str) and gname != channel.get("name"):
            warn(label, f"name {gname!r} differs from channel name {channel.get('name')!r} "
                        "(label only, but check you used the right id)")

        programs = entry.get("programs")
        if not isinstance(programs, list) or not programs:
            warn(label, "has no programs")
            continue

        valid = []
        for n, p in enumerate(programs, 1):
            where = f"{label} program #{n}"
            if not isinstance(p, dict):
                err(where, "is not an object")
                continue

            title = p.get("title")
            title_ok = isinstance(title, str) and title.strip()
            if not title_ok:
                err(where, "missing title")
            elif title != title.strip():
                warn(where, f"title {title!r} has leading/trailing spaces")

            start, why = parse_time(p.get("start"))
            if why:
                err(where, f"start: {why}")
            end, why = parse_time(p.get("end"))
            if why:
                err(where, f"end: {why}")

            if not title_ok or start is None or end is None:
                continue
            if end <= start:
                err(where, f"'{title}' ends ({fmt(end)}) at or before it starts ({fmt(start)}). "
                           "Past midnight? The end needs the next day's date.")
                continue
            valid.append((start, end, title))

        if valid != sorted(valid):
            warn(label, "programs are not in time order")
        valid.sort()

        for (s1, e1, t1), (s2, e2, t2) in zip(valid, valid[1:]):
            if s2 < e1:
                err(label, f"overlap: '{t1}' ends {fmt(e1)} but '{t2}' starts {fmt(s2)}")
            elif s2 > e1:
                warn(label, f"gap from {fmt(e1)} to {fmt(s2)} (after '{t1}')")

        if valid:
            last_end = valid[-1][1]
            coverage.append((channel.get("name") or gid, last_end))
            if last_end <= now:
                warn(label, f"guide has run out (last program ended {fmt(last_end)})")
            elif last_end - now < timedelta(hours=MIN_HOURS_AHEAD):
                hours = (last_end - now).total_seconds() / 3600
                warn(label, f"guide runs out in about {hours:.0f}h (at {fmt(last_end)})")

    if coverage:
        print("Guide coverage (when each guide runs out, Kenya time):")
        for name, last_end in sorted(coverage, key=lambda c: c[1]):
            print(f"  {fmt(last_end)}  {name}")
        print()

    no_guide = [c for cid, c in known.items() if c.get("status") == "active" and cid not in seen]
    if no_guide:
        print(f"Note: {len(no_guide)} active channel(s) have no guide. That is fine, they just show nothing.\n")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    here = Path(__file__).resolve().parent
    channels_path = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "channels.json"
    guide_path = Path(sys.argv[2]) if len(sys.argv) > 2 else here / "guide.json"

    channels = load(channels_path, required=True)
    known = check_channels(channels) if channels is not None else {}

    guide = load(guide_path, required=False)
    if guide is not None:
        if known:
            check_guide(guide, known)
        else:
            print("(skipping guide checks because channels.json could not be read)")

    for line in errors + warnings:
        print(line)
    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()