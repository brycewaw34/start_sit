#!/usr/bin/env python3
"""
Diamond Scout daily updater. Writes everything the page needs into
diamond_scout_startsit.html:

  - projections for every QB/RB/WR/TE: our model (model.py) blended with
    Sleeper's projection, using the mix that tested best (backtest.py)
  - teammate injury effects, projected targets/carries, usage trend
  - expected points vs actual over the last 3 games ("due for more" / "running hot")
  - last-3-game usage and fantasy points, defensive ranks, injury tags
  - Vegas lines (ESPN, DraftKings) and bye weeks
  - Next Gen Stats: separation, YAC over expected, rush yards over expected

Usage:
  python update_stats.py              auto-detects season and week
  python update_stats.py --week 6     force a specific week

Standard library only. If a required source fails, it exits without touching
the HTML so the page keeps its last good data.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import datasrc as ds
import model

HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "diamond_scout_startsit.html"
SUMMARY = HERE / "data" / "backtest_summary.json"
CONFIG = HERE / "config.json"
LIVE_LOG = HERE / "data" / "live_log_{season}.json"

LAST_N = 3
DEF_WEEKS = 4
MAX_SEARCH_RANK = 400
DEFAULT_MODEL_WEIGHT = 0.5

ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
                   "?seasontype=2&week={week}&dates={season}")
ALL_TEAMS = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
             "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO",
             "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS"]
DATA_START, DATA_END = "/*@@DATA@@*/", "/*@@END@@*/"


def r1(v):
    return round(v + 0.0, 1)


def week_range(weeks):
    if not weeks:
        return ""
    weeks = sorted(weeks)
    return f"Wk {weeks[0]}" if len(weeks) == 1 else f"Wks {weeks[0]}-{weeks[-1]}"


def read_page():
    if not HTML_FILE.exists():
        sys.exit(f"ERROR: {HTML_FILE.name} not found next to this script.")
    text = HTML_FILE.read_text(encoding="utf-8")
    if DATA_START not in text or DATA_END not in text:
        sys.exit("ERROR: data markers not found in the HTML.")
    block = text.split(DATA_START, 1)[1].split(DATA_END, 1)[0]
    prev = {}
    for m in re.finditer(r"^const (\w+)\s*=\s*(.*?);\s*$", block, re.M | re.S):
        try:
            prev[m.group(1)] = json.loads(m.group(2))
        except json.JSONDecodeError:
            pass
    return text, prev


def model_weight():
    try:
        return float(json.loads(SUMMARY.read_text())["best_blend"])
    except Exception:  # noqa: BLE001
        return DEFAULT_MODEL_WEIGHT


def espn_weight():
    """ESPN only joins the blend once the live log shows it actually helps."""
    try:
        return float(json.loads(SUMMARY.read_text()).get("espn_weight", 0))
    except Exception:  # noqa: BLE001
        return 0.0


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_live_log(season, week, players, lines):
    """Freeze each player's projections before his game kicks off, for honest grading later."""
    path = Path(str(LIVE_LOG).format(season=season))
    log = json.loads(path.read_text()) if path.exists() else {"season": season, "weeks": {}}
    wk = log["weeks"].setdefault(str(week), {})
    for e in players:
        ln = lines.get(e["t"]) or {}
        if ln.get("state", "pre") != "pre" or not e.get("pj") or (e["pj"].get("ppr") or 0) < 3:
            continue
        wk[e["id"]] = {"t": e["t"], "p": e["p"], "f": e["pj"], "s": e.get("ps"), "m": e.get("pm"), "e": e.get("pe")}
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(log, separators=(",", ":")))
    return len(wk)


# ── LINES (ESPN, falls back to nflverse) ────────────────────────────────────

def parse_details(details):
    if not details:
        return None, None
    m = re.match(r"\s*([A-Za-z]{2,4})\s+([-+]?\d+(?:\.\d+)?)", details)
    if m:
        return ds.team_code(m.group(1)), abs(float(m.group(2)))
    if re.search(r"\b(EVEN|PK|PICK)\b", details.upper()):
        return None, 0.0
    return None, None


def espn_lines(season, week, prev_lines, prev_week):
    data = ds.fetch_json(ESPN_SCOREBOARD.format(season=season, week=week))
    lines = {}
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): ds.team_code((c.get("team") or {}).get("abbreviation"))
                 for c in comp.get("competitors", [])}
        names = {c.get("homeAway"): ((c.get("team") or {}).get("shortDisplayName") or (c.get("team") or {}).get("name") or "")
                 for c in comp.get("competitors", [])}
        scores = {c.get("homeAway"): c.get("score") for c in comp.get("competitors", [])}
        home, away = teams.get("home"), teams.get("away")
        if not home or not away:
            continue
        status = comp.get("status") or ev.get("status") or {}
        state = (status.get("type") or {}).get("state") or "pre"
        kick = comp.get("date") or ev.get("date")
        odds = (comp.get("odds") or [None])[0] or {}
        total = odds.get("overUnder")
        fav, line = parse_details(odds.get("details"))
        for team, opp, is_home in ((home, away, True), (away, home, False)):
            e = {"opp": opp, "home": is_home, "state": state, "kick": kick, "impl": None, "total": None, "spread": None,
                 "name": names.get("home" if is_home else "away", "")}
            if state != "pre":
                mine, theirs = scores.get("home" if is_home else "away"), scores.get("away" if is_home else "home")
                if mine not in (None, "") and theirs not in (None, ""):
                    e["score"], e["opp_score"] = int(float(mine)), int(float(theirs))
            if total is not None and line is not None:
                t = float(total)
                if fav is None:
                    spread, impl = 0.0, t / 2
                elif team == fav:
                    spread, impl = -line, (t + line) / 2
                else:
                    spread, impl = line, (t - line) / 2
                e.update({"impl": round(impl, 2), "total": t, "spread": spread})
            else:
                old = (prev_lines or {}).get(team) if prev_week == week else None
                if old and old.get("opp") == opp and old.get("impl") is not None:
                    e.update({k: old[k] for k in ("impl", "total", "spread")})
            lines[team] = e
    return lines


# ── DISPLAY STATS (last 3 games, defense ranks) ──────────────────────────────

def volume_and_rz(pos, r):
    if pos == "QB":
        return r["patt"], r["rz_patt"] + r["rz_car"]
    if pos == "RB":
        return r["car"] + r["tgt"], r["rz_car"] + r["rz_tgt"]
    return r["tgt"], r["rz_tgt"]


def recent_stats(games, pos):
    recent = games[:LAST_N]
    if not recent:
        return {"g": 0}
    n = len(recent)
    snaps = [g["snp"] / g["tm_snp"] * 100 for g in recent if g.get("tm_snp")]
    vols, rzs = zip(*(volume_and_rz(pos, g) for g in recent))
    return {
        "g": n, "gp": len(games), "wks": sorted(g["week"] for g in recent),
        "snp": round(sum(snaps) / len(snaps)) if snaps else None,
        "vol": r1(sum(vols) / n), "rz": r1(sum(rzs) / n),
        "ppr": r1(sum(g["pts_ppr"] for g in recent) / n),
        "half": r1(sum(g["pts_half"] for g in recent) / n),
        "std": r1(sum(g["pts_std"] for g in recent) / n),
        **({"lg": r1(sum(g.get("pts_lg", 0) for g in recent) / n)} if "pts_lg" in recent[0] else {}),
    }


def def_ranks(rows, weeks):
    use = set(sorted(weeks)[-DEF_WEEKS:])
    out = {}
    scs = model.SCORINGS + (("lg",) if rows and "pts_lg" in rows[0] else ())
    for sc in scs:
        out[sc] = {}
        for pos in ds.POSITIONS:
            per = {}
            for r in rows:
                if r["week"] in use and r["pos"] == pos and r["opp"]:
                    per.setdefault(r["opp"], {}).setdefault(r["week"], 0.0)
                    per[r["opp"]][r["week"]] += r["pts_" + sc]
            avg = {t: sum(v.values()) / len(v) for t, v in per.items()}
            ordered = sorted(avg.items(), key=lambda kv: kv[1], reverse=True)
            out[sc][pos] = {t: i + 1 for i, (t, _) in enumerate(ordered)}
    return out, sorted(use)


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Update Diamond Scout data")
    ap.add_argument("--week", type=int, help="force the upcoming week number")
    ap.add_argument("week_pos", nargs="?", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()

    text, prev = read_page()
    prev_meta = prev.get("META") or {}
    cfg = load_config()
    league = None
    if cfg.get("league_id"):
        try:
            league = ds.fetch_league(cfg["league_id"])
            ds.set_league_scoring(league["scoring"])
            print(f"League: {league['name']} ({len(league['teams'])} teams)")
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: league unavailable ({e}); standard scoring only")

    print("Step 1: current week")
    state = ds.fetch_json(ds.SLEEPER_STATE)
    season = int(state.get("season"))
    week = args.week or args.week_pos
    if not week:
        if state.get("season_type") != "regular":
            print(f"Season type is '{state.get('season_type')}', nothing to update.")
            return
        week = int(state.get("week"))
    completed = list(range(1, week))
    print(f"  Season {season}, Week {week}")

    print("Step 2: players")
    db = ds.fetch_json(ds.SLEEPER_PLAYERS)
    if not isinstance(db, dict) or len(db) < 1000:
        sys.exit("ERROR: Sleeper player database came back empty. Leaving page as-is.")

    print("Step 3: stats")
    rows = [r for w in completed for r in ds.sleeper_rows(season, w)]
    if completed and len(rows) < 100:
        sys.exit("ERROR: Sleeper returned almost no stats. Leaving page as-is.")
    prior = ds.cached_season(season - 1)
    print(f"  {len(rows)} player-games this season, {len(prior)} last season")

    print("Step 4: lines, scores, injuries")
    schedule = []
    try:
        schedule = ds.nfl_games(season)
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: nflverse schedule unavailable ({e})")
    try:
        lines = espn_lines(season, week, prev.get("LINES"), prev_meta.get("week"))
        source = "DraftKings via ESPN"
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: ESPN unavailable ({e}); using nflverse lines")
        lines, source = ds.lines_from_games(schedule, week), "nflverse"
    if not lines and schedule:
        lines, source = ds.lines_from_games(schedule, week), "nflverse"
    try:
        ds.add_weather(lines, week, ds.roofs_from_games(season))
        windy = sorted({t for t, l in lines.items() if (l.get("wind") or 0) >= 15})
        print(f"  weather added; windy (15+ mph): {', '.join(windy) or 'none'}")
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: weather unavailable ({e})")
    byes = sorted(t for t in ALL_TEAMS if t not in lines)
    team_pts = ds.team_points(schedule, week)
    print(f"  {len(lines) // 2} games, byes: {', '.join(byes) or 'none'}")

    print("Step 5: projections")
    players_in = {}
    for pid, p in db.items():
        if p.get("position") in ds.POSITIONS and p.get("team"):
            players_in[pid] = {"pos": p["position"], "team": ds.team_code(p["team"]),
                               "status": (p.get("injury_status") or "").lower(),
                               "depth": p.get("depth_chart_order")}
    P = model.Projector(rows, season, week, prior_rows=prior, league=bool(ds.LEAGUE_SCORING))
    scorings = P.scorings
    proj = P.project(players_in, lines, team_pts)
    try:
        sleeper_proj = ds.sleeper_projections(season, week)
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: Sleeper projections unavailable ({e}); model only")
        sleeper_proj = {}
    try:
        espn_proj = ds.espn_projections(season, week, db)
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: ESPN projections unavailable ({e})")
        espn_proj = {}
    w_model, w_espn = model_weight(), espn_weight()
    print(f"  model weight {w_model}, ESPN weight {w_espn}, Sleeper projections for {len(sleeper_proj)}, ESPN for {len(espn_proj)}")

    print("Step 6: Next Gen Stats")
    ngs = {}
    try:
        raw = ds.nfl_ngs(season)
        gm = ds.gsis_map(db)
        by_name = {}
        for pid, info in players_in.items():
            by_name[(ds.norm_name(db[pid].get("full_name")), info["team"])] = pid
        for g, v in raw.items():
            pid = gm.get(g) or by_name.get((ds.norm_name(v.get("_name")), v.get("_team")))
            if pid:
                ngs[pid] = {k: x for k, x in v.items() if not k.startswith("_")}
        print(f"  {len(ngs)} players matched")
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: Next Gen Stats unavailable ({e})")

    # ── assemble ────────────────────────────────────────────────────────────
    by_player = P.by_player
    defs, def_weeks = def_ranks(rows, completed)
    name = lambda pid: (db.get(pid, {}).get("full_name") or  # noqa: E731
                        f"{db.get(pid, {}).get('first_name', '')} {db.get(pid, {}).get('last_name', '')}").strip()
    players = []
    for pid, info in players_in.items():
        p = db[pid]
        games = by_player.get(pid, [])
        rank = p.get("search_rank") or 10 ** 9
        if not games and (not p.get("active") or rank > MAX_SEARCH_RANK):
            continue
        nm = name(pid)
        if not nm:
            continue
        e = {"id": pid, "n": nm, "t": info["team"], "p": info["pos"], "inj": p.get("injury_status") or "",
             "rank": rank if rank < 10 ** 9 else None}
        e.update(recent_stats(games, info["pos"]))
        pr = proj.get(pid) or {}
        sp = sleeper_proj.get(pid)
        if pr.get("bye") or pr.get("out"):
            e["pj"] = {sc: 0 for sc in scorings}
        else:
            ep = espn_proj.get(pid)
            final = {}
            for sc in scorings:
                m = pr.get(sc)
                if m is None:
                    continue
                v = w_model * m + (1 - w_model) * sp[sc] if sp and sc in sp else m
                if ep and w_espn and sc in ep:
                    v = (1 - w_espn) * v + w_espn * ep[sc]
                final[sc] = round(v, 1)
            if final:
                e["pj"] = final
                e["pm"] = {sc: round(pr[sc], 1) for sc in scorings}
            if sp:
                e["ps"] = {sc: round(sp[sc], 1) for sc in scorings if sc in sp}
            if ep:
                e["pe"] = {sc: round(ep[sc], 1) for sc in scorings if sc in ep}
            if pr.get("opps"):
                e["op"] = pr["opps"]
                e["sh"] = pr["share"]
                if pr.get("base_share"):
                    e["bs"] = pr["base_share"]
                if pr.get("trend") is not None:
                    e["tr"] = pr["trend"]
                if pr.get("boost"):
                    e["bo"] = [[name(src), v] for src, v in pr["boost"]]
        lk = P.luck(pid)
        if lk:
            e["xp"] = {sc: lk["x_" + sc] for sc in scorings}
        if pid in ngs:
            e["ngs"] = ngs[pid]
        players.append(e)
    players.sort(key=lambda e: (e["t"], ds.POSITIONS.index(e["p"]), -((e.get("pj") or {}).get("ppr") or 0)))
    try:
        n_logged = save_live_log(season, week, players, lines)
        print(f"  live log: {n_logged} pre-kickoff projections saved for week {week}")
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: live log not saved ({e})")
    with_proj = sum(1 for e in players if (e.get("pj") or {}).get("ppr"))
    print(f"  {len(players)} players listed, {with_proj} with projections")
    if completed and with_proj < 150:
        sys.exit("ERROR: too few projections. Leaving page as-is.")

    stat_weeks = sorted({w for e in players for w in e.get("wks", [])})
    try:
        bt = json.loads(SUMMARY.read_text())
        acc = {"final": bt["overall"][f"blend{int(w_model * 100)}"]["pairs"], "sleeper": bt["overall"]["sleeper"]["pairs"],
               "model": bt["overall"]["model"]["pairs"]}
    except Exception:  # noqa: BLE001
        acc = None
    meta = {
        "season": season, "week": week,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": f"L{LAST_N}",
        "statsNote": f"Last {LAST_N} games played ({week_range(stat_weeks)})" if stat_weeks else "No games played yet",
        "defNote": f"Fantasy pts allowed per game, {week_range(def_weeks)}" if def_weeks else "",
        "linesSource": source, "modelWeight": w_model, "espnWeight": w_espn, "accuracy": acc,
        "hasEspn": bool(espn_proj),
    }
    league_out = None
    if league:
        league_out = {"id": league["id"], "name": league["name"], "positions": league["positions"],
                      "teams": league["teams"], "me": cfg.get("my_team_owner", "")}
    block = "\n".join([
        DATA_START, "// Auto-generated by update_stats.py. Edits here get overwritten.",
        "const META=" + json.dumps(meta, separators=(",", ":")) + ";",
        "const LINES=" + json.dumps(lines, separators=(",", ":"), sort_keys=True) + ";",
        "const BYES=" + json.dumps(byes) + ";",
        "const DEF=" + json.dumps(defs, separators=(",", ":"), sort_keys=True) + ";",
        "const LEAGUE=" + json.dumps(league_out, separators=(",", ":")) + ";",
        "const PLAYERS=[\n" + ",\n".join(json.dumps(e, separators=(",", ":")) for e in players) + "\n];",
        DATA_END])
    before, rest = text.split(DATA_START, 1)
    HTML_FILE.write_text(before + block + rest.split(DATA_END, 1)[1], encoding="utf-8")
    try:
        import pickem
        pickem.write_page(season, week, lines, byes, schedule, players, meta["updated"])
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: pick'em page not updated ({e})")
    print(f"Done. Wrote {HTML_FILE.name} for Week {week}.")


if __name__ == "__main__":
    main()
