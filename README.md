# metro-glance

Timetable data for a Garmin watch app that shows Metro do Porto departures.

A scheduled job runs `build.py` once a day. It downloads the current timetable
from [Metro do Porto](https://www.metrodoporto.pt/pages/337) (falling back to the
[Porto open-data portal](https://dadosabertos.cm-porto.pt/dataset/horarios-paragens-e-rotas-metro-porto)),
and, when it has changed, rewrites the small files under `data/` that the watch downloads.

| File | Contents |
|---|---|
| `data/version.json` | Version id, validity dates, and a `stale` flag when the timetable has expired |
| `data/calendar.json` | Day type per weekday, plus exception dates such as public holidays |
| `data/stops.json` | `s`: every station as id, name, latitude, longitude |
| `data/patterns.json` | Lines, and each distinct sequence of stops a train follows |
| `data/groups.json` | For each station, the directions trains leave in |
| `data/d/<stop>_<next>_<daytype>.json` | Departures from `<stop>` towards `<next>`: minutes after midnight (`t`) and pattern (`p`) |

Times past midnight belong to the previous service day and are written as 1440 or more.

Timetable data: Metro do Porto. This project is not affiliated with Metro do Porto.

Run locally: `python build.py` (Python 3, standard library only).
