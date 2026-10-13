#!/usr/bin/env python3
"""
Weekly review: grades the projections that were frozen before kickoff
(data/live_log_<season>.json) against what actually happened, plus your
league matchup and the pick'em games. Writes data/review_<season>_w<week>.json.

Run: python review_week.py            (latest week whose games are all final)
     python review_week.py --week 5
"""

import argparse
import itertools
import json
import statistics as st
from pathlib import Path

import datasrc as ds
import pickem

HERE = Path(__file__).resolve().parent
ESPN_WEEK = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
             "?seasontype=2&week={week}&dates={season}")
SLOTS = {"QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["TE"], "FLEX": ["RB", "WR", "TE"],
         "REC_FLEX": ["WR", "TE"], "WRRB_FLEX": ["RB", "WR"], "SUPER_FLEX": ["QB", "RB", "WR", "TE"]}


def espn_games(season, week):
    out = []
    for ev in ds.fetch_json(ESPN_WEEK.format(season=season, week=week)).get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        state = ((comp.get("status") or ev.get("status") or {}).get("type") or {}).get("state")
        t = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in t or "away" not in t:
            continue
        out.append({"home": ds.team_code(t["home"]["team"]["abbreviation"]), "away": ds.team_code(t["away"]["team"]["abbreviation"]),
                    "state": state, "hs": ds.num(t["home"].get("score")), "as": ds.num(t["away"].get("score"))})
    return out


def pairwise(rows, key, actual="a"):
    right = total = 0
    groups = {}
    for r in rows:
        groups.setdefault(r["p"], []).append(r)
    for lst in groups.values():
        for x, y in itertools.combinations(lst, 2):
            if x[actual] == y[actual] or x.get(key) is None or y.get(key) is None:
                continue
            right += (x[key] - y[key]) * (x[actual] - y[actual]) > 0
            total += 1
    return round(right / total, 4) if total else None


def best_lineup(pool, slots, key):
    used, lineup = set(), []
    order = sorted(range(len(slots)), key=lambda i: len(SLOTS[slots[i]]))
    picks = [None] * len(slots)
    for i in order:
        c = sorted((p for p in pool if p["id"] not in used and p["p"] in SLOTS[slots[i]] and p.get(key) is not None),
                   key=lambda p: -p[key])
        if c:
            picks[i] = c[0]; used.add(c[0]["id"])
    for s, p in zip(slots, picks):
        lineup.append({"slot": s, "id": p["id"] if p else None, "n": p["n"] if p else None,
                       "proj": p.get("proj") if p else None, "act": p.get("act") if p else None})
    return lineup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    args = ap.parse_args()
    cfg = json.loads((HERE / "config.json").read_text())
    league = ds.fetch_league(cfg["league_id"])
    ds.set_league_scoring(league["scoring"])
    state = ds.fetch_json(ds.SLEEPER_STATE)
    season = int(state["season"])
    week = args.week
    if not week:
        week = int(state["week"])
        if any(g["state"] != "post" for g in espn_games(season, week)):
            week -= 1
    print(f"Reviewing {season} week {week}")

    db = ds.fetch_json(ds.SLEEPER_PLAYERS)
    name = lambda pid: (db.get(pid, {}).get("full_name") or pid)  # noqa: E731
    actual = {r["pid"]: r for r in ds.sleeper_rows(season, week)}
    log = json.loads((HERE / "data" / f"live_log_{season}.json").read_text())["weeks"].get(str(week), {})

    # ── projections vs actual ────────────────────────────────────────────────
    rows = []
    for pid, e in log.items():
        a = actual.get(pid)
        pts_lg = a.get("pts_lg") if a else 0.0  # frozen projection + didn't play = 0
        r = {"id": pid, "n": name(pid), "p": e["p"], "t": e["t"], "a": round(pts_lg or 0, 2),
             "played": bool(a), "final": e["f"].get("lg"), "sleeper": (e.get("s") or {}).get("lg"),
             "model": (e.get("m") or {}).get("lg"), "espn": (e.get("e") or {}).get("lg")}
        rows.append(r)
    played = [r for r in rows if r["played"]]
    grade = {}
    for k in ("final", "sleeper", "model", "espn"):
        sub = [r for r in played if r.get(k) is not None]
        if len(sub) > 20:
            grade[k] = {"pairs": pairwise(sub, k), "mae": round(st.mean(abs(r[k] - r["a"]) for r in sub), 2), "n": len(sub)}
    misses = sorted((r for r in played if r["final"] is not None and r["final"] >= 8), key=lambda r: r["a"] - r["final"])
    dnp = [r for r in rows if not r["played"] and (r["final"] or 0) >= 8]

    # ── your matchup ─────────────────────────────────────────────────────────
    base = f"https://api.sleeper.app/v1/league/{cfg['league_id']}"
    matchups = ds.fetch_json(f"{base}/matchups/{week}")
    me = next((t for t in league["teams"] if t["owner"] == cfg.get("my_team_owner")), None)
    mine = next((m for m in matchups if me and m["roster_id"] == me["rid"]), None)
    team_out = None
    if mine:
        opp = next((m for m in matchups if m.get("matchup_id") == mine.get("matchup_id") and m["roster_id"] != mine["roster_id"]), None)
        pp = {str(k): v for k, v in (mine.get("players_points") or {}).items()}
        pool = []
        for pid in [str(p) for p in mine.get("players") or []]:
            pos = db.get(pid, {}).get("position")
            if pos in ("QB", "RB", "WR", "TE"):
                pool.append({"id": pid, "n": name(pid), "p": pos, "proj": (log.get(pid) or {}).get("f", {}).get("lg"),
                             "act": pp.get(pid, 0.0)})
        slots = [s for s in league["positions"] if s in SLOTS]
        started = [str(p) for p in mine.get("starters") or []]
        opp_team = next((t for t in league["teams"] if opp and t["rid"] == opp["roster_id"]), {})
        team_out = {
            "points": mine.get("points"), "opp_points": opp.get("points") if opp else None,
            "opp_name": opp_team.get("name"),
            "started": [{"id": p, "n": name(p), "pos": db.get(p, {}).get("position"), "act": pp.get(p, 0.0),
                         "proj": (log.get(p) or {}).get("f", {}).get("lg")} for p in started if p != "0"],
            "tool_lineup": best_lineup(pool, slots, "proj"),
            "hindsight_lineup": best_lineup(pool, slots, "act"),
        }
        team_out["tool_points"] = round(sum(x["act"] or 0 for x in team_out["tool_lineup"]), 2)
        team_out["hindsight_points"] = round(sum(x["act"] or 0 for x in team_out["hindsight_lineup"]), 2)
        flex_ids = {x["id"] for x in team_out["tool_lineup"]}
        team_out["started_skill_points"] = round(sum(x["act"] for x in team_out["started"] if x["pos"] in SLOTS and x["id"]), 2)
        team_out["tool_vs_started"] = sorted(flex_ids ^ {x["id"] for x in team_out["started"] if x["pos"] in SLOTS})

    # ── pick'em ──────────────────────────────────────────────────────────────
    games = []
    for g in espn_games(season, week):
        sched = next((s for s in ds.nfl_games(season) if s["week"] == week and s["home"] == g["home"]), None)
        spread = -sched["spread_line"] if sched and sched["spread_line"] is not None else None
        p_home = pickem.win_prob(spread) if spread is not None else None
        winner = "home" if g["hs"] > g["as"] else "away" if g["as"] > g["hs"] else "tie"
        games.append({"home": g["home"], "away": g["away"], "hs": g["hs"], "as": g["as"], "state": g["state"],
                      "p_home": round(p_home, 3) if p_home is not None else None,
                      "fav": None if p_home is None else ("home" if p_home >= 0.5 else "away"), "winner": winner})

    out = {"season": season, "week": week, "grade": grade,
           "big_misses": [{k: r[k] for k in ("n", "p", "t", "final", "a")} for r in misses[:8]],
           "big_hits": [{k: r[k] for k in ("n", "p", "t", "final", "a")} for r in misses[::-1][:8]],
           "projected_but_dnp": [{k: r[k] for k in ("n", "p", "t", "final")} for r in dnp],
           "my_team": team_out, "pickem": games}
    path = HERE / "data" / f"review_{season}_w{week}.json"
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps({"grade": grade, "my_points": team_out and team_out["points"],
                      "tool": team_out and team_out["tool_points"], "hindsight": team_out and team_out["hindsight_points"]}))


if __name__ == "__main__":
    main()
