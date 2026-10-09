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
        **league_parts(s),
    }


# ── YOUR LEAGUE ──────────────────────────────────────────────────────────────

LEAGUE_SCORING = {}   # set by set_league_scoring(); empty = no league scoring


def set_league_scoring(scoring):
    LEAGUE_SCORING.clear()
    LEAGUE_SCORING.update({k: float(v) for k, v in (scoring or {}).items() if float(v or 0) != 0})


def _group(key):
    """Which part of a player's game a scoring key belongs to (offense only)."""
    if key.startswith(("rec", "bonus_rec")) and not key.startswith("bonus_rush_rec"):
        return "rec"
    if key.startswith(("rush", "bonus_rush")):
        return "rush"
    if key.startswith(("pass", "bonus_pass")) and key != "pass_int_td":
        return "pass"
    if key in ("fum_lost", "fum"):
        return "rush"
    return None


def league_parts(stats):
    if not LEAGUE_SCORING:
        return {}
    parts = {"rec": 0.0, "rush": 0.0, "pass": 0.0}
    for k, v in LEAGUE_SCORING.items():
        grp = _group(k)
        if grp and k in stats:
            parts[grp] += v * num(stats.get(k))
    return {"lg_rec": round(parts["rec"], 2), "lg_rush": round(parts["rush"], 2), "lg_pass": round(parts["pass"], 2),
            "pts_lg": round(sum(parts.values()), 2)}


def _p_over(mean, line, sd):
    from math import erf, sqrt
    if mean <= 0 or sd <= 0:
        return 0.0
    return 0.5 * (1 - erf((line - mean) / (sd * sqrt(2))))


def league_projection(stats):
    """Score a projected stat line with your league's rules, including expected yardage bonuses."""
    pts = sum(v * num(stats.get(k)) for k, v in LEAGUE_SCORING.items() if _group(k) and k in stats and not k.startswith("bonus"))
    for kind in ("rec", "rush"):
        mu = num(stats.get(f"{kind}_yd"))
        sd = max(15.0, 0.6 * mu)
        pts += LEAGUE_SCORING.get(f"bonus_{kind}_yd_100", 0) * _p_over(mu, 100, sd)
        pts += LEAGUE_SCORING.get(f"bonus_{kind}_yd_200", 0) * _p_over(mu, 200, sd)
    mu = num(stats.get("pass_yd"))
    pts += LEAGUE_SCORING.get("bonus_pass_yd_300", 0) * _p_over(mu, 300, 65)
    pts += LEAGUE_SCORING.get("bonus_pass_yd_400", 0) * _p_over(mu, 400, 65)
    return round(pts, 2)


def fetch_league(league_id):
    base = f"https://api.sleeper.app/v1/league/{league_id}"
    lg = fetch_json(base)
    users = {u["user_id"]: u for u in fetch_json(base + "/users")}
    teams = []
    for r in fetch_json(base + "/rosters"):
        u = users.get(r.get("owner_id")) or {}
        name = ((u.get("metadata") or {}).get("team_name") or "").strip() or u.get("display_name") or f"Team {r['roster_id']}"
        teams.append({"rid": r["roster_id"], "owner": u.get("display_name") or "", "name": name,
                      "players": [str(p) for p in (r.get("players") or [])],
                      "starters": [str(p) for p in (r.get("starters") or [])]})
    return {"id": str(league_id), "name": lg.get("name"), "scoring": lg.get("scoring_settings") or {},
            "positions": lg.get("roster_positions") or [], "teams": teams}


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
        if LEAGUE_SCORING:
            out[str(r["player_id"])]["lg"] = league_projection(s)
    return out


def cached_season(season, last_week=18):
    """Full completed season, fetched once then read from data/."""
    path = CACHE / f"sleeper_rows_{season}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        ok = len(cached.get("weeks", [])) >= last_week
        if ok and LEAGUE_SCORING and cached.get("league_scoring") != dict(LEAGUE_SCORING):
            ok = False  # league scoring changed or missing: refetch once
        if ok:
            return cached["rows"]
    rows = []
    for w in range(1, last_week + 1):
        rows += sleeper_rows(season, w)
    if len(rows) > 3000:  # only cache a real season
        CACHE.mkdir(exist_ok=True)
        path.write_text(json.dumps({"season": season, "weeks": list(range(1, last_week + 1)),
                                    "league_scoring": dict(LEAGUE_SCORING) or None,
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
            out.setdefault(r["player_gsis_id"], {"_name": r.get("player_display_name"), "_team": team_code(r.get("team_abbr"))}).update({
                "sep": round(num(r["avg_separation"]), 2),
                "cush": round(num(r["avg_cushion"]), 2),
                "yacoe": round(num(r["avg_yac_above_expectation"]), 2),
                "iay": round(num(r["avg_intended_air_yards"]), 1),
                "ays": round(num(r["percent_share_of_intended_air_yards"]), 1),
            })
    for r in fetch_csv(NFL_NGS.format(kind="rushing")):
        if r.get("season") == str(season) and r.get("week") == "0" and r.get("season_type", "REG") == "REG":
            out.setdefault(r["player_gsis_id"], {"_name": r.get("player_display_name"), "_team": team_code(r.get("team_abbr"))}).update({
                "ryoe": round(num(r["rush_yards_over_expected_per_att"]), 2),
                "rpoe": round(num(r["rush_pct_over_expected"]) * (100 if num(r["rush_pct_over_expected"]) <= 1 else 1), 1),
                "box8": round(num(r.get("percent_attempts_gte_eight_defenders")), 1),
            })
    return out


def norm_name(s):
    s = re.sub(r"[.'’`-]", "", (s or "").lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def gsis_map(db):
    """gsis id -> sleeper id."""
    out = {}
    for pid, p in db.items():
        g = (p.get("gsis_id") or "").strip()
        if g:
            out[g] = pid
    return out


# ── ESPN PROJECTIONS ─────────────────────────────────────────────────────────

ESPN_PLAYERS = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
                "/segments/0/leaguedefaults/3?view=kona_player_info&scoringPeriodId={week}")
# ESPN stat ids -> Sleeper stat keys (offense)
ESPN_STAT = {"3": "pass_yd", "4": "pass_td", "20": "pass_int", "19": "pass_2pt", "24": "rush_yd", "25": "rush_td",
             "26": "rush_2pt", "42": "rec_yd", "43": "rec_td", "44": "rec_2pt", "53": "rec", "72": "fum_lost"}


def espn_projections(season, week, db):
    """ESPN's weekly projections, keyed by Sleeper id. Returns {} if ESPN won't answer."""
    if FIXTURES:
        try:
            data = fetch_json(ESPN_PLAYERS.format(season=season, week=week))
        except RuntimeError:
            return {}
    else:
        flt = {"players": {"filterSlotIds": {"value": [0, 2, 4, 6, 23]},
                           "filterStatsForSourceIds": {"value": [1]},
                           "filterStatsForSplitTypeIds": {"value": [1]},
                           "filterStatsForTopScoringPeriodIds": {"value": 1, "additionalValue": [f"11{season}{week}"]},
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}, "limit": 500}}
        req = urllib.request.Request(ESPN_PLAYERS.format(season=season, week=week),
                                     headers={"User-Agent": "diamond-scout/3.0", "X-Fantasy-Filter": json.dumps(flt),
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
    by_espn = {str(p.get("espn_id")): pid for pid, p in db.items() if p.get("espn_id")}
    out = {}
    for entry in data.get("players", []):
        pl = entry.get("player") or entry
        pid = by_espn.get(str(pl.get("id")))
        if not pid:
            continue
        for st in pl.get("stats") or []:
            if st.get("statSourceId") == 1 and st.get("scoringPeriodId") == week and st.get("seasonId") == season:
                raw = st.get("stats") or {}
                s = {ESPN_STAT[k]: num(v) for k, v in raw.items() if k in ESPN_STAT}
                std = (s.get("pass_yd", 0) / 25 + 4 * s.get("pass_td", 0) - 2 * s.get("pass_int", 0)
                       + s.get("rush_yd", 0) / 10 + 6 * s.get("rush_td", 0) + s.get("rec_yd", 0) / 10
                       + 6 * s.get("rec_td", 0) - 2 * s.get("fum_lost", 0)
                       + 2 * (s.get("pass_2pt", 0) + s.get("rush_2pt", 0) + s.get("rec_2pt", 0)))
                rec = s.get("rec", 0)
                proj = {"ppr": round(std + rec, 2), "half": round(std + rec / 2, 2), "std": round(std, 2)}
                if LEAGUE_SCORING:
                    proj["lg"] = league_projection(s)
                if proj["ppr"] > 0:
                    out[pid] = proj
    return out


# ── WEATHER ──────────────────────────────────────────────────────────────────

STADIUMS = {  # home team -> (lat, lon) of an open-air or retractable stadium
    "ARI": (33.5276, -112.2626), "BAL": (39.2780, -76.6227), "BUF": (42.7738, -78.7870),
    "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167), "CIN": (39.0955, -84.5161),
    "CLE": (41.5061, -81.6995), "DAL": (32.7473, -97.0945), "DEN": (39.7439, -105.0201),
    "GB": (44.5013, -88.0622), "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639),
    "JAX": (30.3239, -81.6373), "KC": (39.0489, -94.4839), "MIA": (25.9580, -80.2389),
    "NE": (42.0909, -71.2643), "NYG": (40.8135, -74.0745), "NYJ": (40.8135, -74.0745),
    "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158), "SEA": (47.5952, -122.3316),
    "SF": (37.4030, -121.9700), "TB": (27.9759, -82.5033), "TEN": (36.1665, -86.7713),
    "WAS": (38.9078, -76.8645),
}
OPEN_METEO = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
              "&hourly=wind_speed_10m,wind_gusts_10m,precipitation,temperature_2m"
              "&wind_speed_unit=mph&temperature_unit=fahrenheit&timezone=UTC&forecast_days=10")


def roofs_from_games(season):
    """{(week, home_team): roof} from nflverse; dome/closed = no weather."""
    out = {}
    for g in fetch_csv(NFL_GAMES):
        if g.get("season") == str(season) and g.get("game_type") == "REG":
            out[(int(g["week"]), team_code(g["home_team"]))] = (g.get("roof") or "").lower()
    return out


def add_weather(lines, week, roofs):
    """Adds wind (mph), gusts, rain (mm/hr), temp (F) to each team's line for outdoor games."""
    from datetime import datetime
    done = {}
    for team, ln in lines.items():
        home = team if ln.get("home") else ln.get("opp")
        roof = roofs.get((week, home), "")
        if roof in ("dome", "closed"):
            ln["dome"] = True
            continue
        if home not in STADIUMS or not ln.get("kick"):
            continue
        if home not in done:
            try:
                lat, lon = STADIUMS[home]
                h = fetch_json(OPEN_METEO.format(lat=lat, lon=lon))["hourly"]
                kick = datetime.strptime(ln["kick"][:13], "%Y-%m-%dT%H")
                key = kick.strftime("%Y-%m-%dT%H:00")
                if key not in h["time"]:
                    done[home] = None
                    continue
                i = h["time"].index(key)
                win = range(i, min(i + 3, len(h["time"])))  # the three hours of the game
                done[home] = {"wind": round(max(h["wind_speed_10m"][j] for j in win), 1),
                              "gust": round(max(h["wind_gusts_10m"][j] for j in win), 1),
                              "rain": round(max(h["precipitation"][j] for j in win), 1),
                              "temp": round(h["temperature_2m"][i])}
            except Exception:  # noqa: BLE001
                done[home] = None
        if done.get(home):
            ln.update(done[home])
