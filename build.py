#!/usr/bin/env python3
"""Turn the Metro do Porto GTFS timetable into small files a watch can download.

Usage:
    python build.py                 # download the current timetable and rebuild data/
    python build.py --zip feed.zip  # rebuild from a local file (for testing)

Output (all under data/):
    version.json     what is published and whether it is still valid
    calendar.json    which day type applies on which date
    stops.json       every station: id, name, latitude, longitude
    patterns.json    lines, and every distinct sequence of stops a train follows
    groups.json      for each station, the directions trains leave in
    d/<stop>_<next>_<daytype>.json   departures from <stop> towards <next>

Only the Python standard library is used.
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict

FMT = 1
SOURCE_PAGE = "https://www.metrodoporto.pt/pages/337"
FALLBACK_API = ("https://dadosabertos.cm-porto.pt/api/3/action/package_show"
                "?id=horarios-paragens-e-rotas-metro-porto")
HEARTBEAT_DAYS = 25  # re-commit version.json this often so the schedule stays active
OUT = "data"
UA = {"User-Agent": "metro-glance timetable updater (github.com/Unrul3r/metro-glance)"}


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- finding and downloading the feed ----------

def find_primary_url():
    """The Metro's download page links the current GTFS zip; its file id changes."""
    html = http_get(SOURCE_PAGE).decode("utf-8", "replace")
    links = re.findall(r'<a[^>]+href="([^"]+\.zip)"[^>]*>(.*?)</a>', html, re.S | re.I)
    for href, text in links:
        if re.search(r"gtfs|google_transit", href + " " + text, re.I):
            return urllib.parse.urljoin(SOURCE_PAGE, href)
    raise RuntimeError("no GTFS link found on " + SOURCE_PAGE)


def find_fallback_url():
    """Newest zip on the city open-data portal, by the date in the resource name."""
    pkg = json.loads(http_get(FALLBACK_API))
    best = None
    for res in pkg["result"]["resources"]:
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})", res.get("name", ""))
        if not m or not res.get("url", "").startswith("http"):
            continue
        if res.get("format", "").upper() not in ("ZIP", "GTFS"):
            continue
        key = (m.group(3), m.group(2), m.group(1))
        if best is None or key > best[0]:
            best = (key, res["url"])
    if best is None:
        raise RuntimeError("no dated GTFS resource on the portal")
    return best[1]


def read_feed(zip_bytes):
    tables = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if not base.endswith(".txt"):
                continue
            text = z.read(name).decode("utf-8-sig", "replace")
            rows = [r for r in csv.DictReader(io.StringIO(text)) if any((v or "").strip() for v in r.values())]
            tables[base[:-4]] = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]
    for needed in ("stops", "trips", "stop_times", "routes"):
        if needed not in tables:
            raise RuntimeError("feed is missing " + needed + ".txt")
    return tables


def feed_range(t):
    """(first day, last day) the feed covers, as dates."""
    starts, ends = [], []
    for r in t.get("calendar", []):
        starts.append(r["start_date"])
        ends.append(r["end_date"])
    for r in t.get("calendar_dates", []):
        starts.append(r["date"])
        ends.append(r["date"])
    if not starts:
        raise RuntimeError("feed has no calendar")
    d = lambda s: dt.datetime.strptime(s, "%Y%m%d").date()
    return d(min(starts)), d(max(ends))


def get_feed(local_zip=None):
    """Return (zip bytes, source url). Prefer whichever source is valid for longer."""
    if local_zip:
        with open(local_zip, "rb") as f:
            return f.read(), "local:" + os.path.basename(local_zip)
    candidates = []
    for finder in (find_primary_url, find_fallback_url):
        try:
            url = finder()
            data = http_get(url, timeout=120)
            end = feed_range(read_feed(data))[1]
            candidates.append((end, data, url))
            print(f"source ok: {url} (valid to {end})")
        except Exception as e:  # one source failing must not stop the other
            print(f"source failed: {finder.__name__}: {e}")
    if not candidates:
        raise RuntimeError("no timetable source could be read")
    candidates.sort(key=lambda c: c[0], reverse=True)  # stable: primary wins ties
    return candidates[0][1], candidates[0][2]


# ---------- processing ----------

def minutes(hms):
    h, m = hms.split(":")[:2]
    return int(h) * 60 + int(m)


def build(t):
    stops = {int(r["stop_id"]): (r["stop_name"], round(float(r["stop_lat"]), 5), round(float(r["stop_lon"]), 5))
             for r in t["stops"]}
    routes = {r["route_id"]: r for r in t["routes"]}
    trips = {r["trip_id"]: r for r in t["trips"]}

    by_trip = defaultdict(list)
    for r in t["stop_times"]:
        if r["trip_id"] in trips:
            by_trip[r["trip_id"]].append((int(r["stop_sequence"]), int(r["stop_id"]),
                                          minutes(r["departure_time"] or r["arrival_time"])))
    for rows in by_trip.values():
        rows.sort()

    # lines, in the order routes.txt lists them
    line_ids = [r["route_id"] for r in t["routes"]]
    lines = [[routes[i].get("route_short_name") or i, routes[i].get("route_color") or "888888"] for i in line_ids]

    # patterns: one per distinct (line, stop sequence)
    pat_index, patterns = {}, []
    trip_pat = {}
    for tid, rows in by_trip.items():
        seq = tuple(s for _, s, _ in rows)
        if len(seq) < 2:
            continue
        key = (trips[tid]["route_id"], seq)
        if key not in pat_index:
            pat_index[key] = len(patterns)
            head = trips[tid].get("trip_headsign") or stops[seq[-1]][0]
            patterns.append([line_ids.index(key[0]), head, list(seq)])
        trip_pat[tid] = pat_index[key]

    # which services run on which date
    first, last = feed_range(t)
    used = {trips[tid]["service_id"] for tid in trip_pat}
    weekly = {}
    for r in t.get("calendar", []):
        flags = [r[d] == "1" for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
        weekly[r["service_id"]] = (flags, r["start_date"], r["end_date"])
    added, removed = defaultdict(set), defaultdict(set)
    for r in t.get("calendar_dates", []):
        (added if r["exception_type"] == "1" else removed)[r["date"]].add(r["service_id"])

    day_sets = {}
    day = first
    while day <= last:
        key = day.strftime("%Y%m%d")
        active = {s for s, (flags, a, b) in weekly.items() if a <= key <= b and flags[day.weekday()]}
        active = ((active | added[key]) - removed[key]) & used
        day_sets[key] = frozenset(active)
        day += dt.timedelta(days=1)

    # day types: each distinct set of running services, most common first
    counts = Counter(s for s in day_sets.values() if s)
    types = [s for s, _ in counts.most_common()]
    type_of = {s: i for i, s in enumerate(types)}
    wd_default = []
    for wd in range(7):
        c = Counter(type_of[s] for k, s in day_sets.items()
                    if s and dt.datetime.strptime(k, "%Y%m%d").weekday() == wd)
        wd_default.append(c.most_common(1)[0][0] if c else 0)
    exceptions = {}
    for k, s in sorted(day_sets.items()):
        wd = dt.datetime.strptime(k, "%Y%m%d").weekday()
        ty = type_of[s] if s else -1  # -1: no service at all that day
        if ty != wd_default[wd]:
            exceptions[k] = ty

    # departures: (stop, next stop, day type) -> [(minute, pattern)]
    deps = defaultdict(list)
    service_types = defaultdict(list)
    for i, s in enumerate(types):
        for sid in s:
            service_types[sid].append(i)
    for tid, p in trip_pat.items():
        rows = by_trip[tid]
        for ty in service_types.get(trips[tid]["service_id"], []):
            for (_, stop, mins), (_, nxt, _) in zip(rows, rows[1:]):
                deps[(stop, nxt, ty)].append((mins, p))

    groups = defaultdict(dict)  # stop -> next stop -> sorted pattern ids
    for (stop, nxt, ty), rows in deps.items():
        rows.sort()
        groups[stop].setdefault(nxt, set()).update(p for _, p in rows)

    return {
        "stops": [[i, n, la, lo] for i, (n, la, lo) in sorted(stops.items(), key=lambda kv: kv[1][0])],
        "lines": lines,
        "patterns": patterns,
        "groups": {str(s): [[n, sorted(ps)] for n, ps in sorted(g.items())] for s, g in sorted(groups.items())},
        "calendar": {"from": first.strftime("%Y%m%d"), "to": last.strftime("%Y%m%d"),
                     "wd": wd_default, "ex": exceptions, "types": len(types)},
        "deps": deps,
        "range": (first, last),
    }


# ---------- writing ----------

def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="build from a local GTFS zip instead of downloading")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--today", help="YYYY-MM-DD, for testing")
    ap.add_argument("--force", action="store_true", help="rewrite even if nothing changed")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()

    zip_bytes, src = get_feed(a.zip)
    version = hashlib.sha256(zip_bytes).hexdigest()[:12]
    feed = read_feed(zip_bytes)
    if len(feed["stops"]) < 50 or len(feed["trips"]) < 1000 or len(feed["stop_times"]) < 20000:
        if not a.zip:
            raise RuntimeError("feed looks too small to be the real timetable; keeping the published data")

    first, last = feed_range(feed)
    stale = today > last
    vpath = os.path.join(a.out, "version.json")
    old = None
    if os.path.exists(vpath):
        with open(vpath, encoding="utf-8") as f:
            old = json.load(f)
    if (old and not a.force and old.get("fmt") == FMT and old.get("v") == version
            and old.get("stale") == stale):
        age = (today - dt.date.fromisoformat(old.get("checked", "2000-01-01"))).days
        if age < HEARTBEAT_DAYS:
            print(f"unchanged ({version}), checked {age} days ago: nothing to write")
            return
        old["checked"] = today.isoformat()
        dump(vpath, old)
        print(f"unchanged ({version}): heartbeat written")
        return

    b = build(feed)
    ddir = os.path.join(a.out, "d")
    if os.path.isdir(ddir):
        shutil.rmtree(ddir)
    sizes = []
    for (stop, nxt, ty), rows in sorted(b["deps"].items()):
        sizes.append(dump(os.path.join(ddir, f"{stop}_{nxt}_{ty}.json"),
                          {"t": [m for m, _ in rows], "p": [p for _, p in rows]}))
    info = {
        "stops.json": dump(os.path.join(a.out, "stops.json"), b["stops"]),
        "patterns.json": dump(os.path.join(a.out, "patterns.json"), {"lines": b["lines"], "pat": b["patterns"]}),
        "groups.json": dump(os.path.join(a.out, "groups.json"), b["groups"]),
        "calendar.json": dump(os.path.join(a.out, "calendar.json"), b["calendar"]),
    }
    dump(vpath, {"fmt": FMT, "v": version, "from": first.strftime("%Y%m%d"), "to": last.strftime("%Y%m%d"),
                 "stale": stale, "checked": today.isoformat(), "src": src})

    print(f"version {version}, valid {first} to {last}, stale={stale}")
    print(f"stops {len(b['stops'])}, lines {len(b['lines'])}, patterns {len(b['patterns'])}, "
          f"day types {b['calendar']['types']}, exceptions {b['calendar']['ex']}")
    print(f"weekday defaults (Mon..Sun): {b['calendar']['wd']}")
    print("file sizes (bytes):", info)
    print(f"departure files: {len(sizes)}, largest {max(sizes)} bytes, total {sum(sizes)} bytes")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
