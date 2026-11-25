# Brighton & Hove Albion calendar feed

A subscribable `.ics` feed of **confirmed, upcoming** Brighton & Hove Albion fixtures in the
Premier League (PL), FA Cup (FA), EFL / Carabao Cup (EFL), UEFA Champions League (CL) and
UEFA Conference League (CONF). All times are in Indian Standard Time (IST).

## Subscribe (Google Calendar)

1. Google Calendar → **Other calendars** → **+** → **From URL**
2. Paste:

   ```
   https://raw.githubusercontent.com/Rabbitwingz/Football-ICS/main/brighton.ics
   ```

Google refreshes subscribed calendars on its own schedule, usually every 12–24 hours.

## How it works

A GitHub Actions workflow (`.github/workflows/update-calendar.yml`) runs every 6 hours. It
regenerates `brighton.ics` with `scripts/build_calendar.py` and commits the result whenever something changed.

| Competition | Source | When a fixture counts as confirmed |
|---|---|---|
| PL | Premier League match API | The matchweek's TV picks have been announced: its matches no longer all share one placeholder slot such as Sat 15:00. The PL must not flag the match as "tbc". |
| FA | ESPN API (`eng.fa`) | ESPN marks the kickoff time as valid, the match is scheduled, and both teams are known |
| EFL | ESPN API (`eng.league_cup`) | Same as FA |
| CL | ESPN API (`uefa.champions`) | Same as FA |
| CONF | ESPN API (`uefa.europa.conf`) | Same as FA |

- Fixtures appear only after their date and kickoff time are confirmed, and drop out once they have kicked off.
- Each match has a stable UID. A rescheduled match moves the existing event and bumps its
  `SEQUENCE`; it never creates a duplicate. `state.json` keeps track of those revisions.
- The location is the match venue as published by the competition's data source.
- Event titles look like `⚽ PL - Brighton & Hove Albion vs Brentford` (home team first).
- Descriptions give the competition, the round and the kick-off time in 12-hour IST, e.g.
  `Kick-off: Thu 26 Nov 2026, 11:15 PM IST`.
- If a source can't be reached, the run fails and the previously published feed stays as it was.

Run locally: `python scripts/build_calendar.py` (Python 3.9+, standard library only).
