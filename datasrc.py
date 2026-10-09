"""
Data plumbing shared by update_stats.py and backtest.py. Standard library only.

Sources:
  Sleeper   player database, weekly stats, weekly projections, current week
  nflverse  schedule with Vegas lines and scores, injury reports, Next Gen Stats
            (free, open data published on GitHub)
"""

import csv
import gzip
import io
import json
import os
import re
import time
import urllib.request
from pathlib import Path

POSITIONS = ["QB", "RB", "WR", "TE"]
HERE = Path(__file__).resolve().parent
CACHE = HERE / "data"

SLEEPER_STATE = "https://api.sleeper.app/v1/state/nfl"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_WEEK = ("https://api.sleeper.app/{kind}/nfl/{season}/{week}"
                "?season_type=regular&position[]={pos}")
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download/"
NFL_GAMES = NFLVERSE + "schedules/games.csv"
NFL_INJURIES = NFLVERSE + "injuries/injuries_{season}.csv"
NFL_NGS = NFLVERSE + "nextgen_stats/ngs_{kind}.csv.gz"

TEAM_FIX = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR"}

FIXTURES = os.environ.get("DS_FIXTURES")  # offline testing: folder of saved responses


def team_code(abbr):
    abbr = (abbr or "").upper()
    return TEAM_FIX.get(abbr, abbr)


def fixture_name(url):
    return re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1])


def fetch_bytes(url, retries=3):
    if FIXTURES:
        for ext in (".json", ""):
            path = Path(FIXTURES) / (fixture_name(url) + ext)
            if path.exists():
                return path.read_bytes()
        raise RuntimeError(f"missing fixture {fixture_name(url)}")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "diamond-scout/3.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} failed: {last}")


def fetch_json(url):
    return json.loads(fetch_bytes(url).decode("utf-8"))


def fetch_csv(url):
    raw = fetch_bytes(url)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ── SLEEPER ──────────────────────────────────────────────────────────────────

def sleeper_records(season, week, kind="stats"):
    """All QB/RB/WR/TE records for one week. kind = 'stats' or 'projections'."""
    out = []
    for pos in POSITIONS:
        data = fetch_json(SLEEPER_WEEK.format(kind=kind, season=season, week=week, pos=pos))
        for rec in data or []:
            if rec.get("player_id"):
                rec["_pos"] = ((rec.get("player") or {}).get("position") or pos)
                out.append(rec)
    return out


def played(s):
    return num(s.get("off_snp")) > 0 or num(s.get("gp")) > 0


def to_row(rec, season, week):
    """Sleeper stat record -> model row (see model.py)."""
    s = rec.get("stats") or {}
    g = lambda k: num(s.get(k))  # noqa: E731
    return {
        "pid": str(rec["player_id"]), "pos": rec["_pos"], "season": int(season), "week": int(week),
        "team": team_code(rec.get("team")), "opp": team_code(rec.get("opponent")),
        "tgt": g("rec_tgt"), "air": g("rec_air_yd"), "rz_tgt": g("rec_rz_tgt"),
        "car": g("rush_att"), "rz_car": g("rush_rz_att"),
        "patt": g("pass_att"), "rz_patt": g("pass_rz_att"),
        "rec": g("rec"), "rec_yd": g("rec_yd"), "rec_td": g("rec_td"),
        "rush_yd": g("rush_yd"), "rush_td": g("rush_td"),
        "pass_yd": g("pass_yd"), "pass_td": g("pass_td"), "pass_int": g("pass_int"),
        "fum_lost": g("fum_lost"), "two_pt": g("rec_2pt") + g("rush_2pt") + g("pass_2pt"),
        "snp": g("off_snp"), "tm_snp": g("tm_off_snp"),
        "pts_ppr": g("pts_ppr"), "pts_half": g("pts_half_ppr"), "pts_std": g("pts_std"),
    }


def sleeper_rows(season, week):
    return [to_row(r, season, week) for r in sleeper_records(season, week, "stats")
            if played(r.get("stats") or {}) and r.get("team")]


def sleeper_projections(season, week):
    out = {}
    for r in sleeper_records(season, week, "projections"):
        s = r.get("stats") or {}
        if s.get("pts_ppr") is None:
            continue
        out[str(r["player_id"])] = {"ppr": num(s.get("pts_ppr")), "half": num(s.get("pts_half_ppr")),
                                    "std": num(s.get("pts_std"))}
    return out


def cached_season(season, last_week=18):
    """Full completed season, fetched once then read from data/."""
    path = CACHE / f"sleeper_rows_{season}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        if len(cached.get("weeks", [])) >= last_week:
            return cached["rows"]
    rows = []
    for w in range(1, last_week + 1):
        rows += sleeper_rows(season, w)
    if len(rows) > 3000:  # only cache a real season
        CACHE.mkdir(exist_ok=True)
        path.write_text(json.dumps({"season": season, "weeks": list(range(1, last_week + 1)),
                                    "rows": rows}, separators=(",", ":")))
    return rows


def cached_projections(season, weeks):
    """Sleeper's own weekly projections for past weeks (cached; they don't change)."""
    path = CACHE / f"sleeper_proj_{season}.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    changed = False
    for w in weeks:
        if str(w) not in data:
            data[str(w)] = sleeper_projections(season, w)
            changed = True
    if changed:
        CACHE.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")))
    return {int(k): v for k, v in data.items()}


# ── NFLVERSE ─────────────────────────────────────────────────────────────────

def nfl_games(season):
    """Regular-season games with home/away, Vegas line, and scores."""
    out = []
    for g in fetch_csv(NFL_GAMES):
        if g.get("season") != str(season) or g.get("game_type") != "REG":
            continue
        out.append({
            "week": int(g["week"]), "home": team_code(g["home_team"]), "away": team_code(g["away_team"]),
            "spread_line": num(g["spread_line"]) if g.get("spread_line") not in ("", "NA", None) else None,
            "total_line": num(g["total_line"]) if g.get("total_line") not in ("", "NA", None) else None,
            "home_score": num(g["home_score"]) if g.get("home_score") not in ("", "NA", None) else None,
            "away_score": num(g["away_score"]) if g.get("away_score") not in ("", "NA", None) else None,
        })
    return out


def lines_from_games(games, week):
    """nflverse spread_line = points the HOME team is favored by."""
    lines = {}
    for g in games:
        if g["week"] != week or g["spread_line"] is None or g["total_line"] is None:
            continue
        sl, tl = g["spread_line"], g["total_line"]
        lines[g["home"]] = {"opp": g["away"], "home": True, "total": tl, "spread": -sl, "impl": round((tl + sl) / 2, 2)}
        lines[g["away"]] = {"opp": g["home"], "home": False, "total": tl, "spread": sl, "impl": round((tl - sl) / 2, 2)}
    return lines


def team_points(games, before_week):
    pts = {}
    for g in games:
        if g["week"] < before_week and g["home_score"] is not None:
            pts.setdefault(g["home"], []).append(g["home_score"])
            pts.setdefault(g["away"], []).append(g["away_score"])
    return pts


def nfl_injuries(season):
    """{week: {gsis_id: status}} from official weekly injury reports."""
    out = {}
    for r in fetch_csv(NFL_INJURIES.format(season=season)):
        if (r.get("game_type") or "REG") != "REG" or not r.get("report_status"):
            continue
        out.setdefault(int(r["week"]), {})[r["gsis_id"]] = r["report_status"].lower()
    return out


def nfl_ngs(season):
    """Season-to-date Next Gen Stats by gsis id."""
    out = {}
    for r in fetch_csv(NFL_NGS.format(kind="receiving")):
        if r.get("season") == str(season) and r.get("week") == "0" and r.get("season_type", "REG") == "REG":
            out.setdefault(r["player_gsis_id"], {}).update({
                "sep": round(num(r["avg_separation"]), 2),
                "cush": round(num(r["avg_cushion"]), 2),
                "yacoe": round(num(r["avg_yac_above_expectation"]), 2),
                "iay": round(num(r["avg_intended_air_yards"]), 1),
                "ays": round(num(r["percent_share_of_intended_air_yards"]), 1),
            })
    for r in fetch_csv(NFL_NGS.format(kind="rushing")):
        if r.get("season") == str(season) and r.get("week") == "0" and r.get("season_type", "REG") == "REG":
            out.setdefault(r["player_gsis_id"], {}).update({
                "ryoe": round(num(r["rush_yards_over_expected_per_att"]), 2),
                "rpoe": round(num(r["rush_pct_over_expected"]) * (100 if num(r["rush_pct_over_expected"]) <= 1 else 1), 1),
                "box8": round(num(r.get("percent_attempts_gte_eight_defenders")), 1),
            })
    return out


def gsis_map(db):
    """gsis id -> sleeper id."""
    out = {}
    for pid, p in db.items():
        g = (p.get("gsis_id") or "").strip()
        if g:
            out[g] = pid
    return out
