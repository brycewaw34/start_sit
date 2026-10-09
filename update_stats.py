#!/usr/bin/env python3
"""
Diamond Scout auto-updater.

Pulls everything the Start/Sit page needs and writes it into
diamond_scout_startsit.html:

  - player list, teams, injury status      (Sleeper player database)
  - last-3-game usage and fantasy points   (Sleeper weekly stats)
  - defensive rank vs each position        (computed from the same stats)
  - spread / total / implied team points   (ESPN scoreboard, DraftKings line)
  - current week and bye teams             (Sleeper state + ESPN)

Usage:
  python update_stats.py              auto-detects season and week
  python update_stats.py --week 6     force a specific week

Standard library only, nothing to install. GitHub Actions runs this daily.
If anything looks wrong (an API is down, data comes back empty), the script
exits without touching the HTML, so the page keeps its last good data.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "diamond_scout_startsit.html"

POSITIONS = ["QB", "RB", "WR", "TE"]
LAST_N = 3            # games used for usage and form averages
DEF_WEEKS = 4         # most recent completed weeks used for defensive ranks
MAX_SEARCH_RANK = 400 # include players Sleeper ranks this high even with no games yet

SLEEPER_STATE = "https://api.sleeper.app/v1/state/nfl"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_WEEK = ("https://api.sleeper.app/stats/nfl/{season}/{week}"
                "?season_type=regular&position[]={pos}")
ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
                   "?seasontype=2&week={week}&dates={season}")

ALL_TEAMS = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
             "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO",
             "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS"]
ESPN_TO_SLEEPER = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR"}

DATA_START = "/*@@DATA@@*/"
DATA_END = "/*@@END@@*/"

# For offline testing: point DS_FIXTURES at a folder of saved responses.
FIXTURES = os.environ.get("DS_FIXTURES")


# ── NETWORK ──────────────────────────────────────────────────────────────────

def fixture_name(url):
    return re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1]) + ".json"


def fetch_json(url, retries=3):
    if FIXTURES:
        path = Path(FIXTURES) / fixture_name(url)
        if not path.exists():
            raise RuntimeError(f"missing fixture {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "diamond-scout/2.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} failed: {last}")


# ── HELPERS ──────────────────────────────────────────────────────────────────

def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def r1(v):
    return round(v + 0.0, 1)


def played(stats):
    return num(stats.get("off_snp")) > 0 or num(stats.get("gp")) > 0


def team_code(abbr):
    abbr = (abbr or "").upper()
    return ESPN_TO_SLEEPER.get(abbr, abbr)


def week_range(weeks):
    if not weeks:
        return ""
    weeks = sorted(weeks)
    return f"Wk {weeks[0]}" if len(weeks) == 1 else f"Wks {weeks[0]}-{weeks[-1]}"


def volume_and_rz(pos, s):
    if pos == "QB":
        return num(s.get("pass_att")), num(s.get("pass_rz_att")) + num(s.get("rush_rz_att"))
    if pos == "RB":
        return (num(s.get("rush_att")) + num(s.get("rec_tgt")),
                num(s.get("rush_rz_att")) + num(s.get("rec_rz_tgt")))
    return num(s.get("rec_tgt")), num(s.get("rec_rz_tgt"))


# ── EXISTING PAGE DATA ───────────────────────────────────────────────────────

def read_page():
    if not HTML_FILE.exists():
        sys.exit(f"ERROR: {HTML_FILE.name} not found next to this script.")
    text = HTML_FILE.read_text(encoding="utf-8")
    if DATA_START not in text or DATA_END not in text:
        sys.exit("ERROR: data markers not found in the HTML. Use the updated HTML file.")
    block = text.split(DATA_START, 1)[1].split(DATA_END, 1)[0]
    prev = {}
    for m in re.finditer(r"^const (\w+)\s*=\s*(.*?);\s*$", block, re.M | re.S):
        try:
            prev[m.group(1)] = json.loads(m.group(2))
        except json.JSONDecodeError:
            pass
    return text, prev


# ── STEP 1: WEEK ─────────────────────────────────────────────────────────────

def get_week(forced_week):
    state = fetch_json(SLEEPER_STATE)
    season = int(state.get("season") or state.get("league_season"))
    if forced_week:
        return season, forced_week
    stype = state.get("season_type")
    if stype != "regular":
        print(f"Season type is '{stype}', nothing to update. Use --week to force.")
        sys.exit(0)
    return season, int(state.get("week") or state.get("display_week"))


# ── STEP 2: STATS ────────────────────────────────────────────────────────────

def get_weekly_records(season, weeks):
    """Return {week: [record, ...]} where each record has player_id, team, opponent, stats."""
    out = {}
    for w in weeks:
        rows = []
        for pos in POSITIONS:
            data = fetch_json(SLEEPER_WEEK.format(season=season, week=w, pos=pos))
            for rec in data or []:
                s = rec.get("stats") or {}
                if not rec.get("player_id") or not played(s):
                    continue
                rows.append({
                    "pid": str(rec["player_id"]),
                    "pos": ((rec.get("player") or {}).get("position") or pos),
                    "team": team_code(rec.get("team")),
                    "opp": team_code(rec.get("opponent")),
                    "week": w,
                    "s": s,
                })
        print(f"  Week {w}: {len(rows)} player games")
        out[w] = rows
    return out


def build_players(db, records):
    by_player = {}
    for w, rows in records.items():
        for r in rows:
            by_player.setdefault(r["pid"], []).append(r)

    players = []
    for pid, p in db.items():
        pos = p.get("position")
        team = p.get("team")
        if pos not in POSITIONS or not team:
            continue
        games = sorted(by_player.get(pid, []), key=lambda r: r["week"], reverse=True)
        rank = p.get("search_rank") or 10**9
        if not games and (not p.get("active") or rank > MAX_SEARCH_RANK):
            continue
        name = (p.get("full_name")
                or f"{p.get('first_name', '')} {p.get('last_name', '')}").strip()
        if not name:
            continue

        entry = {"id": pid, "n": name, "t": team_code(team), "p": pos,
                 "inj": p.get("injury_status") or "", "rank": rank if rank < 10**9 else None,
                 "g": 0}
        recent = games[:LAST_N]
        if recent:
            n = len(recent)
            snaps = [num(g["s"].get("off_snp")) / num(g["s"].get("tm_off_snp")) * 100
                     for g in recent if num(g["s"].get("tm_off_snp")) > 0]
            vols, rzs = zip(*(volume_and_rz(pos, g["s"]) for g in recent))
            entry.update({
                "g": n,
                "gp": len(games),
                "wks": sorted(g["week"] for g in recent),
                "snp": round(sum(snaps) / len(snaps)) if snaps else None,
                "vol": r1(sum(vols) / n),
                "rz": r1(sum(rzs) / n),
                # zeros and negative games count; that's the honest average
                "ppr": r1(sum(num(g["s"].get("pts_ppr")) for g in recent) / n),
                "half": r1(sum(num(g["s"].get("pts_half_ppr")) for g in recent) / n),
                "std": r1(sum(num(g["s"].get("pts_std")) for g in recent) / n),
            })
        players.append(entry)

    players.sort(key=lambda e: (e["t"], POSITIONS.index(e["p"]), -(e.get("ppr") or 0)))
    return players


def build_def_ranks(records, weeks):
    """Rank 1 = allows the most fantasy points per game to that position (easiest)."""
    use = sorted(weeks)[-DEF_WEEKS:]
    ranks = {}
    for scoring, key in (("ppr", "pts_ppr"), ("half", "pts_half_ppr"), ("std", "pts_std")):
        ranks[scoring] = {}
        for pos in POSITIONS:
            per_team_week = {}
            for w in use:
                for r in records.get(w, []):
                    if r["pos"] != pos or not r["opp"]:
                        continue
                    k = (r["opp"], w)
                    per_team_week[k] = per_team_week.get(k, 0.0) + num(r["s"].get(key))
            totals = {}
            for (team, _w), pts in per_team_week.items():
                totals.setdefault(team, []).append(pts)
            avg = {t: sum(v) / len(v) for t, v in totals.items()}
            ordered = sorted(avg.items(), key=lambda kv: kv[1], reverse=True)
            ranks[scoring][pos] = {t: i + 1 for i, (t, _) in enumerate(ordered)}
    return ranks, use


# ── STEP 3: VEGAS LINES ──────────────────────────────────────────────────────

def parse_details(details):
    """'JAX -7.5' -> ('JAX', 7.5). 'EVEN' / 'PK' -> (None, 0.0)."""
    if not details:
        return None, None
    m = re.match(r"\s*([A-Za-z]{2,4})\s+([-+]?\d+(?:\.\d+)?)", details)
    if m:
        return team_code(m.group(1)), abs(float(m.group(2)))
    if re.search(r"\b(EVEN|PK|PICK)\b", details.upper()):
        return None, 0.0
    return None, None


def build_lines(season, week, prev_lines, prev_week):
    data = fetch_json(ESPN_SCOREBOARD.format(season=season, week=week))
    lines = {}
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): team_code((c.get("team") or {}).get("abbreviation"))
                 for c in comp.get("competitors", [])}
        home, away = teams.get("home"), teams.get("away")
        if not home or not away:
            continue
        status = comp.get("status") or ev.get("status") or {}
        state = (status.get("type") or {}).get("state") or "pre"
        kick = comp.get("date") or ev.get("date")

        odds = (comp.get("odds") or [None])[0] or {}
        total = odds.get("overUnder")
        fav, line = parse_details(odds.get("details"))
        if fav is None and line is None and odds:
            # fall back to the favorite flags if 'details' was missing
            for side, team in (("homeTeamOdds", home), ("awayTeamOdds", away)):
                if (odds.get(side) or {}).get("favorite") and odds.get("spread") is not None:
                    fav, line = team, abs(float(odds["spread"]))

        for team, opp, is_home in ((home, away, True), (away, home, False)):
            entry = {"opp": opp, "home": is_home, "state": state, "kick": kick,
                     "impl": None, "total": None, "spread": None}
            if total is not None and line is not None:
                total = float(total)
                if fav is None:  # pick'em
                    spread, impl = 0.0, total / 2
                elif team == fav:
                    spread, impl = -line, (total + line) / 2
                else:
                    spread, impl = line, (total - line) / 2
                entry.update({"impl": round(impl, 2), "total": total, "spread": spread})
            else:
                # ESPN drops odds once a game kicks off; keep the last line we saw
                old = (prev_lines or {}).get(team) if prev_week == week else None
                if old and old.get("opp") == opp and old.get("impl") is not None:
                    entry.update({k: old[k] for k in ("impl", "total", "spread")})
            lines[team] = entry

    byes = sorted(t for t in ALL_TEAMS if t not in lines)
    return lines, byes


# ── STEP 4: WRITE ────────────────────────────────────────────────────────────

def render_block(meta, lines, byes, defs, players):
    out = [DATA_START, "// Auto-generated by update_stats.py. Edits here get overwritten."]
    out.append("const META=" + json.dumps(meta, separators=(",", ":")) + ";")
    out.append("const LINES=" + json.dumps(lines, separators=(",", ":"), sort_keys=True) + ";")
    out.append("const BYES=" + json.dumps(byes) + ";")
    out.append("const DEF=" + json.dumps(defs, separators=(",", ":"), sort_keys=True) + ";")
    rows = ",\n".join(json.dumps(p, separators=(",", ":")) for p in players)
    out.append("const PLAYERS=[\n" + rows + "\n];")
    out.append(DATA_END)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Update Diamond Scout data")
    ap.add_argument("--week", type=int, help="force the upcoming week number")
    ap.add_argument("week_pos", nargs="?", type=int, help=argparse.SUPPRESS)  # old usage
    args = ap.parse_args()
    forced = args.week or args.week_pos

    text, prev = read_page()
    prev_meta = prev.get("META") or {}

    print("Step 1: current week")
    season, week = get_week(forced)
    completed = list(range(1, week))
    print(f"  Season {season}, Week {week}. Completed weeks: {completed or 'none'}")

    print("Step 2: player database")
    db = fetch_json(SLEEPER_PLAYERS)
    if not isinstance(db, dict) or len(db) < 1000:
        sys.exit("ERROR: Sleeper player database came back empty. Leaving page as-is.")

    print("Step 3: weekly stats")
    records = get_weekly_records(season, completed)
    if completed and sum(len(v) for v in records.values()) < 100:
        sys.exit("ERROR: Sleeper returned almost no stats. Leaving page as-is.")
    players = build_players(db, records)
    with_stats = sum(1 for p in players if p["g"])
    print(f"  {len(players)} players listed, {with_stats} with stats")

    defs, def_weeks = build_def_ranks(records, completed)

    print("Step 4: Vegas lines")
    try:
        lines, byes = build_lines(season, week, prev.get("LINES"), prev_meta.get("week"))
        with_odds = sum(1 for v in lines.values() if v["impl"] is not None)
        print(f"  {len(lines) // 2} games, {with_odds // 2} with lines, byes: {', '.join(byes) or 'none'}")
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: lines unavailable ({e}). Keeping previous lines if same week.")
        same = prev_meta.get("week") == week
        lines = prev.get("LINES", {}) if same else {}
        byes = prev.get("BYES", []) if same else []

    stat_weeks = sorted({w for p in players for w in p.get("wks", [])})
    meta = {
        "season": season,
        "week": week,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": f"L{LAST_N}",
        "statsNote": (f"Last {LAST_N} games played ({week_range(stat_weeks)})"
                      if stat_weeks else "No games played yet"),
        "defNote": (f"Fantasy pts allowed per game, {week_range(def_weeks)}"
                    if def_weeks else ""),
        "linesSource": "DraftKings via ESPN",
    }

    block = render_block(meta, lines, byes, defs, players)
    before, rest = text.split(DATA_START, 1)
    after = rest.split(DATA_END, 1)[1]
    HTML_FILE.write_text(before + block + after, encoding="utf-8")
    print(f"Done. Wrote {HTML_FILE.name} for Week {week}.")


if __name__ == "__main__":
    main()
