#!/usr/bin/env python3
"""Rewrites the generated block in README.md.

All five Vols sports plus poll rankings come from the NCAA API
(ncaa-api.henrygd.me). ESPN's site API was the original score source but began
returning 403 (Akamai bot protection) from datacenter IPs in Aug 2026, so it is
no longer used.

The NCAA API has no team-history endpoint, so the most recent game is found by
pulling the season's game slots from /schedule-alt and walking backwards through
/scoreboard until Tennessee appears. Most sports index the scoreboard by date
(YYYY/MM/DD); football indexes by week (YYYY/WK). The walk-back is bounded and
throttled well under the published 5 req/sec limit.

Also computes days since the last *public* GitHub contribution and picks a
deterministic fact for the day. Everything degrades gracefully:

- Scores/ranks are cached in .github/profile-cache.json. If a sport can't be
  fetched, the last successfully retrieved value is kept - never an error row.
- Season record comes from the poll when Tennessee is ranked, else the standings
  endpoint, else the cached value.
- The NCAA feed gives a numeric round, not a name, so stakes default to the game
  date. --set-stakes pins a nicer label ("Music City Bowl") to that specific
  game; the auto date returns once a newer game is played.
- The contribution timer anchors to a cached date, so the counter keeps ticking
  up even after old public events roll out of GitHub's events feed.

Usage:
  python scripts/update_profile.py                     # fetch live, update all
  python scripts/update_profile.py --offline           # render from cache only
  python scripts/update_profile.py --set-stakes football "Music City Bowl"
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

# NCAA API sport slugs. Football is division "fbs" and its scoreboard is indexed
# by week (YYYY/WK); the rest are "d1" and indexed by date (YYYY/MM/DD).
SPORTS = [
    {"key": "football", "label": "🏈 Football",
     "ncaa_sport": "football", "division": "fbs", "weekly": True},
    {"key": "mbb", "label": "🏀 Men's Basketball",
     "ncaa_sport": "basketball-men", "division": "d1"},
    {"key": "wbb", "label": "🏀 Lady Vols Basketball",
     "ncaa_sport": "basketball-women", "division": "d1"},
    {"key": "baseball", "label": "⚾ Baseball",
     "ncaa_sport": "baseball", "division": "d1"},
    {"key": "softball", "label": "🥎 Softball",
     "ncaa_sport": "softball", "division": "d1"},
]

# NCAA API (ncaa-api.henrygd.me). Allows 5 req/sec; we stay well under it.
NCAA_BASE = "https://ncaa-api.henrygd.me/"
NCAA_DELAY = 0.6
NCAA_MAX_LOOKBACK = 25  # scoreboard slots to walk back (covers early tourney exits)
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


def _parse_mdy(raw: str):
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def _tn_final_from_board(board: dict | None) -> tuple | None:
    """Latest completed Tennessee game on a scoreboard slate, or None.

    A slate can hold two Tennessee games (e.g. a doubleheader or a best-of-3),
    so the latest by startTimeEpoch wins. Matches "Tennessee" exactly so it
    never picks up Tennessee Tech, Tennessee St., or Middle Tenn.
    """
    best = None
    for entry in (board or {}).get("games", []):
        gm = entry.get("game") or {}
        if gm.get("gameState") != "final":
            continue
        for side in ("home", "away"):
            if ((gm.get(side) or {}).get("names") or {}).get("short") == "Tennessee":
                epoch = int(gm.get("startTimeEpoch") or 0)
                if best is None or epoch > best[0]:
                    best = (epoch, gm, side)
    return best


def _game_to_row(gm: dict, side: str) -> dict:
    other = "away" if side == "home" else "home"
    opp = ((gm.get(other) or {}).get("names") or {}).get("short") or "TBD"

    def score(which: str) -> int:
        try:
            return int((gm.get(which) or {}).get("score") or 0)
        except (TypeError, ValueError):
            return 0

    marker = "🟢 W" if (gm.get(side) or {}).get("winner") else "🔴 L"
    joiner = "vs." if side == "home" else "@"
    played = _parse_mdy(gm.get("startDate") or "")
    when = f"{played:%B} {played.day}" if played else (gm.get("startDate") or "-")
    return {
        "matchup": f"UT {joiner} {opp}",
        "result": f"{marker} {score(side)}-{score(other)}",
        "stakes": when,
        "game_id": str(gm.get("gameID") or ""),
    }


def ncaa_latest_game(sport_cfg: dict, cached_watermark: str | None = None) -> dict | None:
    """Most recent completed Tennessee game via the NCAA API.

    Pulls the season's slots from /schedule-alt, keeps only those already begun,
    and walks backwards through /scoreboard (by date, or by week for football)
    until a completed Tennessee game turns up. Bounded by NCAA_MAX_LOOKBACK.

    Returns {"unchanged": True} when no new game slot has appeared since the
    cached watermark (a settled season) so the daily job skips a pointless walk.
    Football is exempt: its slot is a whole week, so a fresh result can land
    without the slot date advancing, and it's cheap to walk anyway.
    """
    sport, division = sport_cfg["ncaa_sport"], sport_cfg["division"]
    weekly = sport_cfg.get("weekly", False)
    today = datetime.now(timezone.utc).date()
    year = today.year
    for season in (year, year - 1):
        sched = http_get_json(f"{NCAA_BASE}schedule-alt/{sport}/{division}/{season}")
        time.sleep(NCAA_DELAY)
        entries = (((sched or {}).get("data") or {}).get("schedules") or {}).get("games") or []
        slots = []  # (scoreboard_path, start_date) for slots that have begun
        for idx, entry in enumerate(entries, start=1):
            contest = entry.get("contestDate") or ""
            # Football slots are week ranges "MM/DD/YYYY-MM/DD/YYYY" indexed by the
            # 1-based week number; other sports are single dates. Use the range
            # start so an in-progress week/day is considered.
            begun = _parse_mdy(contest.split("-")[0])
            if not begun:
                continue
            path = f"{season}/{idx:02d}" if weekly else f"{begun:%Y}/{begun:%m}/{begun:%d}"
            slots.append((path, begun))
        past = [s for s in slots if s[1] <= today]
        if not past:
            continue
        watermark = past[-1][1].isoformat()
        if not weekly and cached_watermark and watermark <= cached_watermark:
            return {"unchanged": True}
        for path, _date in reversed(past[-NCAA_MAX_LOOKBACK:]):
            board = http_get_json(f"{NCAA_BASE}scoreboard/{sport}/{division}/{path}")
            time.sleep(NCAA_DELAY)
            best = _tn_final_from_board(board)
            if best:
                row = _game_to_row(best[1], best[2])
                row["slot_watermark"] = watermark
                return row
    return None


def _tn_overall_from_standings(payload) -> str | None:
    """Tennessee's overall W-L from a standings payload, or None."""
    if isinstance(payload, dict):
        if payload.get("School") == "Tennessee" and "Overall W" in payload:
            return f"{payload['Overall W']}-{payload['Overall L']}"
        for value in payload.values():
            found = _tn_overall_from_standings(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _tn_overall_from_standings(value)
            if found:
                return found
    return None


def team_record(sport_cfg: dict, cache: dict) -> str:
    """Season record: poll figure if ranked, else standings, else cached."""
    key = sport_cfg["key"]
    poll = cache.get("poll_records", {}).get(key)
    if poll:
        return poll
    standings = http_get_json(
        f"{NCAA_BASE}standings/{sport_cfg['ncaa_sport']}/{sport_cfg['division']}")
    time.sleep(NCAA_DELAY)
    found = _tn_overall_from_standings(standings)
    if found:
        return found
    return cache.get("scores", {}).get(key, {}).get("record") or "-"


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
    if OFFLINE:
        return cache
    for sport in SPORTS:
        key = sport["key"]
        prev = scores.get(key, {})
        print(f"Fetching {key} (NCAA {sport['ncaa_sport']}/{sport['division']}) ...")
        got = ncaa_latest_game(sport, prev.get("slot_watermark"))
        if got and got.get("unchanged"):
            print(f"  {key} no new games - kept {prev.get('result', '?')} ({prev.get('fetched', '?')})")
            continue
        if not got:
            if key in scores:
                print(f"  {key} unavailable - keeping cached score from {scores[key].get('fetched', '?')}")
            else:
                print(f"  {key} unavailable and no cached score yet")
            continue
        override = prev.get("stakes_override")
        bound = prev.get("stakes_override_game")
        # A manual label sticks to the game it describes; a new game clears it.
        if override and (not bound or bound == got["game_id"]):
            got["stakes"] = override
            got["stakes_override"] = override
            got["stakes_override_game"] = got["game_id"]
        got["record"] = team_record(sport, cache)
        got["fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        scores[key] = got
        print(f"  -> {got['result']} ({got['matchup']}, {got['stakes']}) | record {got['record']}")
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


def set_stakes() -> int:
    """`--set-stakes <sport-key> "<label>"`

    e.g. --set-stakes football "Music City Bowl"
    The NCAA feed gives a numeric round, not a name, so stakes default to the
    game date. This pins a nicer label to that sport's current game; the auto
    date returns once a newer game is played. Run an update first so there's a
    game to attach to.
    """
    keys = [s["key"] for s in SPORTS]
    i = sys.argv.index("--set-stakes")
    vals = sys.argv[i + 1:i + 3]
    if len(vals) < 2 or vals[0] not in keys:
        print('usage: --set-stakes <key> "<label>"')
        print("  keys: " + ", ".join(keys))
        return 2
    key, label = vals[0], vals[1]
    cache = load_cache()
    row = cache.setdefault("scores", {}).get(key)
    if not row:
        print(f"no cached game for {key} yet - run an update first")
        return 1
    row["stakes"] = label
    row["stakes_override"] = label
    row["stakes_override_game"] = row.get("game_id", "")
    if not write_readme(cache, _days_from_anchor(cache)):
        return 1
    save_cache(cache)
    print(f"{key} stakes set to '{label}' (bound to game {row.get('game_id') or '?'}).")
    return 0


def main() -> int:
    if "--set-stakes" in sys.argv:
        return set_stakes()

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
