"""
Pick'em data for picks.html. Called by update_stats.py after the start/sit data is built.

Win probability comes from the Vegas spread: P(team wins) = Phi(-spread / 11.25).
11.25 was fit on 2021-2025 regular-season results and matched moneyline-based
probabilities exactly (log loss 0.610 vs 0.611 over 1,355 games).
"""

import json
from math import erf, sqrt
from pathlib import Path

HERE = Path(__file__).resolve().parent
PICKS_FILE = HERE / "picks.html"
SIGMA = 11.25
DATA_START, DATA_END = "/*@@DATA@@*/", "/*@@END@@*/"
KEY_STATUSES = {"Questionable", "Doubtful", "Out"}


def win_prob(spread):
    """spread from this team's side: negative = favored."""
    return 0.5 * (1 + erf(-spread / (SIGMA * sqrt(2))))


# Underdog spots that beat the moneyline on 2006-2026 regular-season games, in both halves of the data.
# Applied at HALF the measured size, since some of it is surely luck.
#   early season (wk 1-4) dogs: +2.8%   low total (<= 40) dogs: +2.9%
#   small dog (+3 or less) + low total: +5.5%   small dog + early season: +6.4%
def edge_adjustment(week, line, total):
    """Extra win chance (0-1) for the underdog, and the reasons."""
    early, low = week <= 4, total is not None and total <= 40
    small = line is not None and abs(line) <= 3
    adj, why = 0.0, []
    if early:
        adj += 0.014; why.append("early-season underdog")
    if low:
        adj += 0.0145; why.append("low-scoring game")
    if small and early:
        adj = max(adj, 0.032)
    if small and low:
        adj = max(adj, 0.0275)
    if small and why:
        why.insert(0, "small underdog")
    return min(adj, 0.04), why


def key_injuries(players, team):
    """Starters and difference-makers with an injury tag this week."""
    out = []
    for e in players:
        if e["t"] != team or e.get("inj") not in KEY_STATUSES:
            continue
        recent = e.get("ppr") or 0
        if (e["p"] == "QB" and recent >= 8) or recent >= 10:
            out.append({"n": e["n"], "p": e["p"], "s": e["inj"]})
    out.sort(key=lambda x: (x["p"] != "QB", x["s"] == "Questionable"))
    return out[:4]


def build(season, week, lines, byes, games, players, updated):
    week_games = []
    for team, ln in lines.items():
        if not ln.get("home"):
            continue
        away = ln["opp"]
        al = lines.get(away, {})
        g = {"home": team, "away": away, "home_name": ln.get("name") or team, "away_name": al.get("name") or away,
             "kick": ln.get("kick"), "state": ln.get("state", "pre"), "spread": ln.get("spread"), "total": ln.get("total"),
             "wind": ln.get("wind"), "gust": ln.get("gust"), "rain": ln.get("rain"), "temp": ln.get("temp"), "dome": ln.get("dome"),
             "inj_home": key_injuries(players, team), "inj_away": key_injuries(players, away)}
        if ln.get("spread") is not None:
            raw = win_prob(ln["spread"])
            g["p_home_raw"] = round(raw, 4)
            adj, why = edge_adjustment(week, ln["spread"], ln.get("total"))
            if adj and abs(ln["spread"]) > 0:
                home_dog = ln["spread"] > 0
                g["edge"] = {"side": "home" if home_dog else "away", "adj": round(adj, 4), "why": why}
                raw = raw + adj if home_dog else raw - adj
            g["p_home"] = round(raw, 4)
        if "score" in ln:
            g["score_home"], g["score_away"] = ln["score"], ln.get("opp_score")
        week_games.append(g)
    week_games.sort(key=lambda g: (g.get("kick") or "", g["home"]))

    # Season so far: how Vegas favorites did each completed week (closing lines, nflverse)
    results = []
    for w in sorted({g["week"] for g in games if g["week"] < week}):
        wk = [g for g in games if g["week"] == w and g["home_score"] is not None and g["spread_line"]]
        if not wk:
            continue
        fav_wins, upsets = 0, []
        for g in wk:
            home_fav = g["spread_line"] > 0
            if g["home_score"] == g["away_score"]:
                continue
            home_won = g["home_score"] > g["away_score"]
            if home_won == home_fav:
                fav_wins += 1
            else:
                dog = g["home"] if home_won else g["away"]
                fav = g["away"] if home_won else g["home"]
                upsets.append({"dog": dog, "fav": fav, "line": abs(g["spread_line"]),
                               "p_fav": round(win_prob(-abs(g["spread_line"])), 3)})
        results.append({"week": w, "n": len(wk), "fav_wins": fav_wins,
                        "upsets": sorted(upsets, key=lambda u: -u["line"])})

    return {"season": season, "week": week, "updated": updated, "sigma": SIGMA,
            "byes": byes, "games": week_games, "results": results}


def write_page(season, week, lines, byes, games, players, updated):
    if not PICKS_FILE.exists():
        print("  picks.html not found, skipping pick'em")
        return
    data = build(season, week, lines, byes, games, players, updated)
    text = PICKS_FILE.read_text(encoding="utf-8")
    before, rest = text.split(DATA_START, 1)
    block = DATA_START + "\n// Auto-generated by update_stats.py\nconst PICK=" + json.dumps(data, separators=(",", ":")) + ";\n" + DATA_END
    PICKS_FILE.write_text(before + block + rest.split(DATA_END, 1)[1], encoding="utf-8")
    print(f"  pick'em: {len(data['games'])} games, {len(data['results'])} past weeks")
