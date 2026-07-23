#!/usr/bin/env python3
"""Rewrites the generated block in README.md.

Two data sources, each used for what it can actually do:

- ESPN site API  -> football, both basketballs, baseball. It exposes a team
  schedule endpoint, so one call per sport yields the latest result, a named
  postseason "stakes" headline (e.g. "Music City Bowl"), and the season record.
- NCAA API       -> poll rankings for all five sports, plus softball scores,
  which ESPN does not carry at all. It has no team-history endpoint, so softball
  is found by pulling the season's game dates and walking backwards through the
  daily scoreboard until Tennessee appears (bounded, and throttled well under
  the published 5 req/sec limit).

Also computes days since the last *public* GitHub contribution and picks a
deterministic fact for the day. Everything degrades gracefully:

- Scores are cached in .github/profile-cache.json. If a sport can't be
  fetched (out of season, ESPN hiccup), the last successfully retrieved score is
  kept and re-rendered - never an error row.
- The NCAA payload gives a numeric bracketRound rather than a round *name*, so
  softball's stakes default to the game date. --set-softball writes a nicer
  label that sticks to that specific game and is cleared once a newer one lands.
- The contribution timer anchors to a cached date, so the counter keeps ticking
  up even after old public events roll out of GitHub's events feed.

Usage:
  python scripts/update_profile.py            # fetch live, update README + cache
  python scripts/update_profile.py --offline  # skip network, render from cache
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to cp1252 and choke on emoji in our log lines; force
# UTF-8 so local preview runs behave like the Linux Actions runner.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CACHE = ROOT / ".github" / "profile-cache.json"

START = "<!-- PROFILE:START -->"
END = "<!-- PROFILE:END -->"

GH_USER = os.environ.get("GH_USER", "sbcjr")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OFFLINE = "--offline" in sys.argv

# Verified ESPN site-API coordinates. Tennessee shares team id 2633 for football
# and both basketballs, but baseball is a different id (199). "manual" sports have
# no dependable ESPN team-schedule endpoint and simply persist their cached score.
SPORTS = [
    {"key": "football", "label": "🏈 Football",
     "sport": "football", "league": "college-football", "team": "2633"},
    {"key": "mbb", "label": "🏀 Men's Basketball",
     "sport": "basketball", "league": "mens-college-basketball", "team": "2633"},
    {"key": "wbb", "label": "🏀 Lady Vols Basketball",
     "sport": "basketball", "league": "womens-college-basketball", "team": "2633"},
    {"key": "baseball", "label": "⚾ Baseball",
     "sport": "baseball", "league": "college-baseball", "team": "199"},
    {"key": "softball", "label": "🥎 Softball",
     "ncaa": {"sport": "softball", "division": "d1"}},
]

# Poll rankings come from the NCAA API (ncaa-api.henrygd.me). It has no
# team-history endpoint, so scores still come from ESPN; this is rankings only.
# The service allows 5 req/sec - we send 5 requests total, spaced well under it.
NCAA_BASE = "https://ncaa-api.henrygd.me/"
NCAA_DELAY = 0.6
NCAA_MAX_LOOKBACK = 8  # scoreboard dates to walk back before giving up
RANKINGS = {
    "football": "rankings/football/fbs/associated-press",
    "mbb": "rankings/basketball-men/d1/associated-press",
    "wbb": "rankings/basketball-women/d1/associated-press",
    "baseball": "rankings/baseball/d1/d1baseballcom-top-25",
    "softball": "rankings/softball/d1/espncom%2Fusa-softball",
}

DEV_FACTS = [
    'The first computer "bug" was a literal moth, taped into Grace Hopper\'s 1947 logbook.',
    "The term 'debugging' predates computers - engineers 'debugged' hardware for decades prior.",
    "Python is named after Monty Python, not the snake.",
    "The '@' in email was chosen by Ray Tomlinson in 1971 because it was unlikely to appear in names.",
    "The first 1GB hard drive (1980, IBM 3380) weighed ~550 lbs and cost $40,000.",
    "'Hello, World!' comes from Kernighan's 1972 tutorial for the B language.",
    "The two hard things in CS: cache invalidation, naming things, and off-by-one errors.",
    "Git was written by Linus Torvalds in ~2 weeks in 2005 after a BitKeeper falling-out.",
    "The QWERTY layout was designed in the 1870s to slow typists and prevent jams.",
    "A 'jiffy' is a real unit of time - often ~10 ms - used in some OS kernels.",
    "The first webcam watched a coffee pot at Cambridge so no one wasted a trip for an empty pot.",
    "JavaScript was created in 10 days in 1995 by Brendan Eich.",
    "The Apollo 11 guidance computer had ~4 KB of RAM - less than a single emoji today.",
    "'Foobar' likely derives from the WWII-era military slang 'FUBAR'.",
    "The first version of Unix (1969) fit on a machine with 24 KB of memory.",
    "SQL was originally called SEQUEL, but the name was already trademarked.",
    "Ada Lovelace wrote the first algorithm intended for a machine in the 1840s.",
    "The 'save' icon is a floppy disk most developers under 30 have never held.",
    "cURL, released in 1998, now ships in billions of devices including cars and TVs.",
    "A byte wasn't always 8 bits - early machines used 6, 7, or 9-bit bytes.",
    "The Linux penguin is named Tux; Torvalds picked a penguin after being bitten by one.",
    "The first domain ever registered was symbolics.com, in March 1985.",
]


def http_get_json(url: str, token: str = "") -> dict | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": "sbcjr-profile",
        "Accept": "application/json",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                print(f"  GET {url} -> HTTP {resp.status}")
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        print(f"  GET {url} failed: {type(exc).__name__}: {exc}")
        return None


def parse_when(raw: str) -> str:
    """Fallback label for games with no postseason headline, e.g. 'March 17'."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return f"{dt:%B} {dt.day}"
    except ValueError:
        return "-"


def tennessee_rank(entries: list) -> str | None:
    """Find Tennessee's rank in an NCAA rankings payload.

    Key names vary per poll ('RANK ' with a trailing space, 'COLLEGE',
    'SCHOOL (1ST PLACE VOTES)', 'TEAM'), and names may carry vote counts
    like 'Tennessee (25)'. Must not match 'Tennessee Tech' or 'Middle Tenn.'.
    """
    for row in entries:
        rank = name = record = None
        for k, v in row.items():
            key = k.strip().upper()
            if key == "RANK":
                rank = str(v).strip()
            elif key.startswith(("SCHOOL", "COLLEGE", "TEAM")):
                name = str(v)
            elif "RECORD" in key:
                record = str(v).strip()
        if not name:
            continue
        if re.sub(r"\s*\(\d+\)\s*$", "", name).strip().lower() == "tennessee":
            return rank, record
    return None, None


def refresh_ranks(cache: dict) -> dict:
    """Update poll ranks, distinguishing 'unranked' from 'fetch failed'."""
    ranks = cache.setdefault("ranks", {})
    poll_records = cache.setdefault("poll_records", {})
    if OFFLINE:
        return cache
    for key, path in RANKINGS.items():
        data = http_get_json(NCAA_BASE + path)
        time.sleep(NCAA_DELAY)
        if data is None:
            print(f"  rank {key}: lookup failed, keeping cached #{ranks.get(key, '-')}")
            continue
        found, record = tennessee_rank(data.get("data") or [])
        if found:
            ranks[key] = found
            if record:
                poll_records[key] = record
            print(f"  rank {key}: #{found}" + (f" ({record})" if record else ""))
        else:
            ranks.pop(key, None)
            print(f"  rank {key}: unranked")
    return cache


def ncaa_latest_game(sport: str, division: str) -> dict | None:
    """Most recent completed Tennessee game via the NCAA API.

    The API has no team-history endpoint, so this pulls the season's game dates
    from /schedule-alt and walks backwards through /scoreboard until Tennessee
    appears. Bounded by NCAA_MAX_LOOKBACK so a quiet stretch can't spiral into
    dozens of requests. Multiple games on one day are ordered by startTimeEpoch.
    """
    year = datetime.now(timezone.utc).year
    for season in (year, year - 1):
        sched = http_get_json(f"{NCAA_BASE}schedule-alt/{sport}/{division}/{season}")
        time.sleep(NCAA_DELAY)
        games = (((sched or {}).get("data") or {}).get("schedules") or {}).get("games") or []
        dates = [g.get("contestDate") for g in games if g.get("contestDate")]
        if not dates:
            continue
        for contest_date in list(reversed(dates))[:NCAA_MAX_LOOKBACK]:
            try:
                mm, dd, yy = contest_date.split("/")
            except ValueError:
                continue
            board = http_get_json(f"{NCAA_BASE}scoreboard/{sport}/{division}/{yy}/{mm}/{dd}")
            time.sleep(NCAA_DELAY)
            best = None
            for entry in (board or {}).get("games", []):
                gm = entry.get("game") or {}
                if gm.get("gameState") != "final":
                    continue
                for side in ("home", "away"):
                    names = (gm.get(side) or {}).get("names") or {}
                    if names.get("short") == "Tennessee":
                        epoch = int(gm.get("startTimeEpoch") or 0)
                        if best is None or epoch > best[0]:
                            best = (epoch, gm, side)
            if not best:
                continue
            _, gm, side = best
            other = "away" if side == "home" else "home"
            opp = ((gm.get(other) or {}).get("names") or {}).get("short") or "TBD"
            try:
                us_score = int((gm.get(side) or {}).get("score") or 0)
                them_score = int((gm.get(other) or {}).get("score") or 0)
            except (TypeError, ValueError):
                us_score = them_score = 0
            marker = "🟢 W" if (gm.get(side) or {}).get("winner") else "🔴 L"
            joiner = "vs." if side == "home" else "@"
            try:
                dt = datetime.strptime(gm.get("startDate") or contest_date, "%m/%d/%Y")
                when = f"{dt:%B} {dt.day}"
            except ValueError:
                when = contest_date
            return {
                "matchup": f"UT {joiner} {opp}",
                "result": f"{marker} {us_score}-{them_score}",
                "stakes": when,
                "game_id": str(gm.get("gameID") or ""),
            }
    return None


def _score(competitor: dict) -> int:
    raw = competitor.get("score")
    if isinstance(raw, dict):
        raw = raw.get("displayValue") or raw.get("value")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def stakes_label(headline: str) -> str:
    """Condense an ESPN game headline into a short 'stakes' label.

    'NCAA Men's Basketball Championship - Midwest Region - Elite 8' -> 'Elite 8'
    'SEC Tournament - Quarterfinal'                                 -> 'SEC Quarterfinal'
    'Liberty Mutual Music City Bowl'                                -> 'Music City Bowl'
    """
    if not headline:
        return ""
    parts = [p.strip() for p in headline.split(" - ") if p.strip()]
    if len(parts) >= 2:
        tail = parts[-1]
        acronym = re.match(r"^([A-Z]{2,5})\b", parts[0])
        if acronym and acronym.group(1) != "NCAA" and acronym.group(1) not in tail:
            return f"{acronym.group(1)} {tail}"
        return tail
    words = parts[0].split()
    if words and words[-1].lower() == "bowl" and len(words) > 3:
        return " ".join(words[-3:])  # drop the sponsor prefix
    return parts[0]


def _extract_game(event: dict, team_id: str) -> tuple | None:
    """Return (us, them, raw_date, dt, headline) for a completed game, else None."""
    comps = event.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    status = (comp.get("status") or event.get("status") or {}).get("type", {})
    if not status.get("completed"):
        return None
    competitors = comp.get("competitors") or []
    us = next((c for c in competitors if str((c.get("team") or {}).get("id")) == team_id), None)
    them = next((c for c in competitors if c is not us), None)
    if not us or not them:
        return None
    raw_date = event.get("date") or comp.get("date") or ""
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    notes = comp.get("notes") or event.get("notes") or []
    headline = next((n.get("headline") for n in notes
                     if isinstance(n, dict) and n.get("headline")), "")
    return us, them, raw_date, dt, headline


def team_season_summary(sport: str, league: str, team_id: str) -> dict | None:
    """Most recent completed game + season W-L record.

    Fetches both regular (seasontype=2) and postseason (seasontype=3) so bowls,
    conference tourneys, and the CWS count. Falls back a season for the offseason.
    """
    year = datetime.now(timezone.utc).year
    base = (f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}"
            f"/teams/{team_id}/schedule")
    for season in (year, year - 1):
        games = []
        for stype in (2, 3):  # 2 = regular season, 3 = postseason
            data = http_get_json(f"{base}?season={season}&seasontype={stype}")
            for event in (data or {}).get("events", []):
                game = _extract_game(event, team_id)
                if game:
                    games.append(game)
        if not games:
            continue
        wins = sum(1 for g in games if g[0].get("winner"))
        losses = sum(1 for g in games
                     if not g[0].get("winner") and g[1].get("winner"))
        us, them, raw_date, _dt, headline = max(games, key=lambda g: g[3])
        opp = ((them.get("team") or {}).get("shortDisplayName")
               or (them.get("team") or {}).get("displayName") or "TBD")
        joiner = "vs." if us.get("homeAway", "home") == "home" else "@"
        marker = "🟢 W" if us.get("winner") else "🔴 L"
        return {
            "matchup": f"UT {joiner} {opp}",
            "result": f"{marker} {_score(us)}-{_score(them)}",
            "stakes": stakes_label(headline) or parse_when(raw_date),
            "record": f"{wins}-{losses}",
        }
    return None


def _days_from_anchor(cache: dict) -> int:
    anchor = cache.get("contrib_anchor")
    if not anchor:
        return 0
    try:
        anchor_dt = datetime.fromisoformat(anchor).date()
    except ValueError:
        return 0
    return max(0, (datetime.now(timezone.utc).date() - anchor_dt).days)


def days_since_public_contribution(cache: dict) -> int:
    """Days since the most recent public contribution, anchored via cache."""
    if not OFFLINE:
        url = f"https://api.github.com/users/{GH_USER}/events/public?per_page=100"
        events = http_get_json(url, GH_TOKEN)
        contrib_types = {
            "PushEvent", "PullRequestEvent", "IssuesEvent",
            "PullRequestReviewEvent", "CommitCommentEvent", "CreateEvent",
        }
        latest = None
        for ev in (events or []):
            if ev.get("type") in contrib_types and ev.get("created_at"):
                if latest is None or ev["created_at"] > latest:
                    latest = ev["created_at"]
        if latest:
            cache["contrib_anchor"] = latest[:10]
    return _days_from_anchor(cache)


def dev_fact_of_the_day() -> str:
    day = datetime.now(timezone.utc).timetuple().tm_yday
    return DEV_FACTS[day % len(DEV_FACTS)]


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"scores": {}}


def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def refresh_scores(cache: dict) -> dict:
    scores = cache.setdefault("scores", {})
    for sport in SPORTS:
        key = sport["key"]
        if OFFLINE:
            continue
        if sport.get("ncaa"):
            cfg = sport["ncaa"]
            print(f"Fetching {key} (NCAA {cfg['sport']}/{cfg['division']}) ...")
            got = ncaa_latest_game(cfg["sport"], cfg["division"])
            if got:
                prev = scores.get(key, {})
                override = prev.get("stakes_override")
                bound = prev.get("stakes_override_game")
                # A manual label sticks to the game it describes. Unbound labels
                # bind to the first game seen; a new game clears a stale label.
                if override and (not bound or bound == got["game_id"]):
                    got["stakes"] = override
                    got["stakes_override"] = override
                    got["stakes_override_game"] = got["game_id"]
                got["record"] = (cache.get("poll_records", {}).get(key)
                                 or prev.get("record") or "-")
                got["fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                scores[key] = got
                print(f"  -> {got['result']} ({got['matchup']}, {got['stakes']}) | record {got['record']}")
            elif key in scores:
                print(f"  {key} unavailable - keeping cached score from {scores[key].get('fetched', '?')}")
            else:
                print(f"  {key} unavailable and no cached score yet")
            continue
        print(f"Fetching {key} ({sport['sport']}/{sport['league']}, team {sport['team']}) ...")
        got = team_season_summary(sport["sport"], sport["league"], sport["team"])
        if got:
            got["fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            scores[key] = got
            print(f"  -> {got['result']} ({got['matchup']}, {got['stakes']}) | record {got['record']}")
        elif key in scores:
            print(f"  {key} unavailable - keeping cached score from {scores[key].get('fetched', '?')}")
        else:
            print(f"  {key} unavailable and no cached score yet")
    return cache


def render_block(cache: dict, days: int, fact: str) -> str:
    scores = cache.get("scores", {})
    ranks = cache.get("ranks", {})
    rows = []
    for sport in SPORTS:
        s = scores.get(sport["key"])
        rank = ranks.get(sport["key"])
        label = f"{sport['label']} (#{rank})" if rank else sport["label"]
        if s:
            stakes = s.get("stakes") or s.get("when")
            result = f"{s['result']} - {stakes}" if stakes else s["result"]
            rows.append(f"| {label} | {s['matchup']} | {result} | {s.get('record', '-')} |")
        else:
            rows.append(f"| {label} | - | ⚪ awaiting first score | - |")
    table = "\n".join(rows)
    return f"""{START}
<!-- Auto-updated by .github/workflows/update-profile.yml. Do not hand-edit inside these markers. -->

**🍊 Vols latest results**

| Sport/Rank | Latest matchup | Result | Season |
|---|---|---|:--:|
{table}

`WW91IGZvdW5kIGl0LCB5YXkuLi4uIPCfpZo=`

**⏱️ Days since my last public contribution:** `{days}`
> …but don't be fooled - legit contributions all ship to 🔒 **SECRET** private repos.

**🧠 Fact of the day:** {fact}

{END}"""


def write_readme(cache: dict, days: int) -> bool:
    """Splice the rendered block into README.md between the markers."""
    if not README.exists():
        print(f"README not found at {README}", file=sys.stderr)
        return False
    text = README.read_text(encoding="utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        print(f"README.md must contain exactly one {START} and one {END} "
              f"(found {text.count(START)} and {text.count(END)}).", file=sys.stderr)
        return False
    block = render_block(cache, days, dev_fact_of_the_day())
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    README.write_text(before + block + after, encoding="utf-8")
    return True


def set_softball() -> int:
    """`--set-softball "<matchup>" <W|L> <score> "<stakes>" <record>`

    e.g. --set-softball "UT vs. Texas" L 0-4 "WCWS National Semifinal" 49-12
    Softball has no reliable feed, so this is how you refresh its row by hand.
    """
    i = sys.argv.index("--set-softball")
    vals = sys.argv[i + 1:i + 6]
    if len(vals) < 5:
        print('usage: --set-softball "<matchup>" <W|L> <score> "<stakes>" <record>')
        print('e.g.   --set-softball "UT vs. Texas" L 0-4 "WCWS National Semifinal" 49-12')
        return 2
    matchup, outcome, score, stakes, record = vals[:5]
    marker = "🟢 W" if outcome.upper().startswith("W") else "🔴 L"
    cache = load_cache()
    prev = cache.setdefault("scores", {}).get("softball", {})
    cache["scores"]["softball"] = {
        "matchup": matchup,
        "result": f"{marker} {score}",
        "stakes": stakes,
        "record": record,
        # The label sticks to this specific game; when a newer one is fetched
        # the auto date label takes over again.
        "stakes_override": stakes,
        "stakes_override_game": prev.get("game_id", ""),
        "game_id": prev.get("game_id", ""),
        "fetched": "manual",
    }
    if not write_readme(cache, _days_from_anchor(cache)):
        return 1
    save_cache(cache)
    print(f"Softball set: {marker} {score} - {matchup} ({stakes}), record {record}.")
    return 0


def main() -> int:
    if "--set-softball" in sys.argv:
        return set_softball()

    cache = load_cache()
    cache = refresh_ranks(cache)
    cache = refresh_scores(cache)
    days = days_since_public_contribution(cache)
    if not write_readme(cache, days):
        return 1
    save_cache(cache)
    print(f"Updated README.md (contribution timer: {days} days).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
