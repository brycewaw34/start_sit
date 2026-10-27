#!/usr/bin/env python3
"""One-off: pull every league matchup so far (users, rosters, matchups, player names)."""
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
cfg = json.load(open(Path(__file__).resolve().parent.parent / "config.json"))
L = f"https://api.sleeper.app/v1/league/{cfg['league_id']}"
def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "diamond-scout"}), timeout=60) as r:
        return json.load(r)
state = get("https://api.sleeper.app/v1/state/nfl")
league = get(L); users = get(L + "/users"); rosters = get(L + "/rosters")
last = int(state.get("week") or 1)
mus = {}
for w in range(1, last + 1):
    m = get(f"{L}/matchups/{w}")
    if m and any((x.get("points") or 0) > 0 for x in m):
        mus[w] = m
ids = set()
for m in mus.values():
    for x in m:
        ids.update(x.get("players") or []); ids.update(x.get("starters") or [])
for r in rosters:
    ids.update(r.get("players") or [])
players = get("https://api.sleeper.app/v1/players/nfl")
names = {i: {"n": p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or i,
             "pos": p.get("position"), "team": p.get("team")} for i, p in players.items() if i in ids}
out = {"state": state, "league": {k: league.get(k) for k in ("name", "settings", "scoring_settings", "roster_positions")},
       "users": [{"user_id": u["user_id"], "display_name": u.get("display_name"), "team_name": (u.get("metadata") or {}).get("team_name")} for u in users],
       "rosters": [{"roster_id": r["roster_id"], "owner_id": r.get("owner_id"), "settings": r.get("settings"), "players": r.get("players")} for r in rosters],
       "matchups": mus, "names": names}
Path("recap_out").mkdir(exist_ok=True)
json.dump(out, open("recap_out/league_2026.json", "w"))
print("weeks", list(mus), "players", len(names))
