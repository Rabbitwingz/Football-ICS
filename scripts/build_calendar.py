#!/usr/bin/env python3
"""Build a subscribable .ics feed of confirmed, upcoming Brighton & Hove Albion fixtures.

Sources
  PL  - Premier League's own match API (sdp-prem-prod.premier-league-prod.pulselive.com)
  EFL - ESPN public API, league "eng.league_cup"
  CL  - ESPN public API, league "uefa.champions"

Confirmation rules (never guess a date or kickoff time)
  PL  - At release the PL parks every match in a matchweek in a single placeholder slot
        (e.g. all ten at Sat 15:00). Once broadcast picks are announced the matches spread
        across several slots. A matchweek therefore counts as confirmed only when its matches
        (played ones included) fall in more than one distinct kickoff slot. Matches flagged
        "tbc" by the PL, or not in the "PreMatch" state, are always excluded. The final matchweek
        is fixed by rule (all matches kick off at once) and is trusted as published.
  Cups - ESPN must mark the time as valid, the match must be in the scheduled state, and both
        teams must be known (no "Winner of ..." placeholders).

Only fixtures that have not kicked off yet are written. Each fixture has a stable UID, so when a match
is rescheduled the existing calendar event moves rather than being duplicated. state.json
tracks SEQUENCE/DTSTAMP so calendar clients notice those changes.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICS_PATH = ROOT / "brighton.ics"
STATE_PATH = ROOT / "state.json"

TEAM_NAME = "Brighton & Hove Albion"
PL_TEAM_ID = "36"
ESPN_TEAM_ID = "331"
ESPN_LEAGUES = {"EFL": "eng.league_cup", "CL": "uefa.champions"}
COMPETITION_NAMES = {"PL": "Premier League", "EFL": "EFL Cup", "CL": "UEFA Champions League"}

PL_API = "https://sdp-prem-prod.premier-league-prod.pulselive.com/api/v2"
ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/soccer"

IST = timezone(timedelta(hours=5, minutes=30))
UK_OFFSETS = {"GMT": timedelta(0), "BST": timedelta(hours=1)}
MATCH_LENGTH = timedelta(hours=2)
UID_DOMAIN = "football-ics.rabbitwingz.github.io"

# Opponent names as the PL API spells them -> how they should read in titles.
NAME_FIXES = {"Brighton and Hove Albion": TEAM_NAME}


def fetch_json(url: str, retries: int = 3):
    req = urllib.request.Request(url, headers={"User-Agent": "Football-ICS/1.0", "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:  # network blips are common on shared CI runners
            if attempt == retries - 1:
                raise RuntimeError(f"GET {url} failed: {exc}") from exc
            time.sleep(2 * (attempt + 1))


def team_name(name: str) -> str:
    return NAME_FIXES.get(name, name)


# --------------------------------------------------------------------------- Premier League

def pl_season_matches(season: int) -> list[dict]:
    matches, cursor = [], None
    while True:
        url = f"{PL_API}/matches?competition=8&season={season}&_limit=100"
        if cursor:
            url += f"&_next={cursor}"
        page = fetch_json(url)
        matches.extend(page.get("data") or [])
        cursor = (page.get("pagination") or {}).get("_next")
        if not cursor:
            return matches


def pl_kickoff_utc(match: dict) -> datetime:
    offset = UK_OFFSETS.get(match.get("kickoffTimezone"))
    if offset is None:
        raise ValueError(f"Unknown timezone {match.get('kickoffTimezone')!r} for PL match {match.get('matchId')}")
    local = datetime.strptime(match["kickoff"], "%Y-%m-%d %H:%M:%S")
    return (local - offset).replace(tzinfo=timezone.utc)


def pl_fixtures(now: datetime) -> list[dict]:
    # PL seasons are keyed by their starting year; ask for both candidates so the feed rolls
    # over cleanly whenever the new season's fixtures are published.
    start_year = now.year if now.month >= 7 else now.year - 1
    fixtures = []
    for season in (start_year, start_year + 1):
        matches = pl_season_matches(season)
        if not matches:
            continue

        slots_by_week: dict[int, set[str]] = {}
        for m in matches:
            slots_by_week.setdefault(m["matchWeek"], set()).add(m["kickoff"])
        final_week = max(slots_by_week)
        confirmed_weeks = {w for w, slots in slots_by_week.items() if len(slots) > 1 or w == final_week}

        for m in matches:
            if PL_TEAM_ID not in (m["homeTeam"]["id"], m["awayTeam"]["id"]):
                continue
            if m.get("period") != "PreMatch" or m["matchWeek"] not in confirmed_weeks:
                continue
            kickoff = pl_kickoff_utc(m)
            if kickoff <= now:
                continue
            detail = fetch_json(f"{PL_API}/matches/{m['matchId']}")
            if detail.get("tbc"):
                continue
            fixtures.append({
                "uid": f"pl-{m['matchId']}@{UID_DOMAIN}",
                "comp": "PL",
                "home": team_name(m["homeTeam"]["name"]),
                "away": team_name(m["awayTeam"]["name"]),
                "start": kickoff,
                "location": m.get("ground") or "",
                "round": f"Matchweek {m['matchWeek']}",
            })
    return fixtures


# --------------------------------------------------------------------------- Cups (ESPN)

def espn_fixtures(code: str, league: str, now: datetime) -> list[dict]:
    data = fetch_json(f"{ESPN_API}/{league}/teams/{ESPN_TEAM_ID}/schedule?fixture=true")
    fixtures = []
    for event in data.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        status = ((comp.get("status") or {}).get("type") or {}).get("name")
        if status != "STATUS_SCHEDULED" or comp.get("timeValid") is not True:
            continue
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        if set(sides) != {"home", "away"}:
            continue
        names = {k: ((v.get("team") or {}).get("displayName") or "").strip() for k, v in sides.items()}
        if any(not n or "TBD" in n or "Winner" in n or "Loser" in n for n in names.values()):
            continue
        kickoff = datetime.strptime(event["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        if kickoff <= now:
            continue
        venue = comp.get("venue") or {}
        city = (venue.get("address") or {}).get("city")
        location = ", ".join(p for p in (venue.get("fullName"), city) if p)
        notes = comp.get("notes") or []
        round_name = (notes[0].get("headline") or "") if notes else ""
        fixtures.append({
            "uid": f"espn-{code.lower()}-{event['id']}@{UID_DOMAIN}",
            "comp": code,
            "home": team_name(names["home"]),
            "away": team_name(names["away"]),
            "start": kickoff,
            "location": location,
            "round": round_name,
        })
    return fixtures


# --------------------------------------------------------------------------- ICS output

def ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Fold to 75-octet lines (RFC 5545 3.1) without splitting UTF-8 characters."""
    out, current, size = [], "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > 75:
            out.append(current)
            current, size = " ", 1
        current += ch
        size += width
    out.append(current)
    return "\r\n".join(out)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fmt_ist(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y%m%dT%H%M%S")


def fixture_fingerprint(f: dict) -> str:
    key = "|".join([f["start"].isoformat(), f["location"], f["home"], f["away"]])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def apply_state(fixtures: list[dict], now: datetime) -> dict:
    """Bump SEQUENCE/DTSTAMP when a fixture changes; keep them stable otherwise."""
    old = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    new = {}
    for f in fixtures:
        fp = fixture_fingerprint(f)
        prev = old.get(f["uid"])
        if prev is None:
            entry = {"fingerprint": fp, "sequence": 0, "stamp": fmt_utc(now)}
        elif prev["fingerprint"] != fp:
            entry = {"fingerprint": fp, "sequence": prev["sequence"] + 1, "stamp": fmt_utc(now)}
        else:
            entry = prev
        f["sequence"], f["stamp"] = entry["sequence"], entry["stamp"]
        new[f["uid"]] = entry
    return new


def build_ics(fixtures: list[dict]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Rabbitwingz//Football-ICS//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Football",
        "X-WR-CALDESC:Confirmed upcoming Brighton & Hove Albion fixtures (PL\\, EFL Cup\\, UCL) in IST",
        "X-WR-TIMEZONE:Asia/Kolkata",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Kolkata",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0530",
        "TZOFFSETTO:+0530",
        "TZNAME:IST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for f in fixtures:
        start_ist = f["start"].astimezone(IST)
        summary = f"⚽ {f['comp']} - {f['home']} vs {f['away']}"
        description = "\n".join(p for p in (
            COMPETITION_NAMES[f["comp"]] + (f" - {f['round']}" if f["round"] else ""),
            f"Kick-off: {start_ist:%a %d %b %Y, %H:%M} IST",
        ) if p)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{f['uid']}",
            f"DTSTAMP:{f['stamp']}",
            f"SEQUENCE:{f['sequence']}",
            f"DTSTART;TZID=Asia/Kolkata:{fmt_ist(f['start'])}",
            f"DTEND;TZID=Asia/Kolkata:{fmt_ist(f['start'] + MATCH_LENGTH)}",
            f"SUMMARY:{ics_escape(summary)}",
            f"LOCATION:{ics_escape(f['location'])}",
            f"DESCRIPTION:{ics_escape(description)}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        fixtures = pl_fixtures(now)
        for code, league in ESPN_LEAGUES.items():
            fixtures += espn_fixtures(code, league, now)
    except Exception as exc:
        # Leave the published feed untouched rather than publishing a partial calendar.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # The same match must never appear twice, even if a source lists it twice.
    unique = {f["uid"]: f for f in fixtures}
    fixtures = sorted(unique.values(), key=lambda f: (f["start"], f["uid"]))

    state = apply_state(fixtures, now)
    ICS_PATH.write_bytes(build_ics(fixtures).encode("utf-8"))
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for f in fixtures:
        print(f"{f['start'].astimezone(IST):%Y-%m-%d %H:%M IST}  {f['comp']:<3} {f['home']} vs {f['away']}  @ {f['location']}")
    print(f"{len(fixtures)} confirmed upcoming fixtures written to {ICS_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
