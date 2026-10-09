"""
Diamond Scout projection model. Standard library only.

How a projection is built (per player, per week):

  1. SHARE: the player's recency-weighted share of his team's targets, carries,
     and pass attempts, over the games he actually played.
  2. INJURIES: teammates who are out free up their share. That share goes mostly
     to same-position teammates (in proportion to their roles), the rest to
     everyone. A player who's already been out for weeks frees up little, since
     his teammates' recent shares already reflect his absence. Returning players
     reclaim share (team totals get capped at 100%).
  3. TEAM VOLUME: the team's recent targets/carries/attempts per game, adjusted
     for this week's Vegas total (pace) and spread (underdogs throw more,
     favorites run more).
  4. EFFICIENCY: fantasy points per opportunity. Starts from what that kind of
     usage usually produces (deep targets, red zone looks, etc.), then moves
     toward the player's own results as his sample grows. This is what keeps
     a hot streak or a TD drought from swinging the projection too far.
  5. MATCHUP + GAME ENVIRONMENT: points the opponent allows to the position
     (shrunk toward average early in the season) and the team's Vegas implied
     points vs its usual scoring.

Every row passed in is one player-game with these keys:
  pid pos team opp season week tgt air rz_tgt car rz_car patt rz_patt
  rec rec_yd rec_td rush_yd rush_td pass_yd pass_td pass_int fum_lost two_pt
"""

from collections import defaultdict

SCORINGS = ("ppr", "half", "std")
REC_VALUE = {"ppr": 1.0, "half": 0.5, "std": 0.0}

# Expected fantasy points per opportunity, fit on every 2024-2025 player-game.
#   rec  = [per target, per air yard, per red zone target]
#   rush = [per carry, per red zone carry]
#   pass = [per attempt, per red zone attempt]
COEF = {"ppr": {"WR": {"rec": [1.371, 0.0194, 1.1579], "rush": [0.5284, 0.8444], "pass": [0.0, 0.0]}, "TE": {"rec": [1.3667, 0.0251, 1.5294], "rush": [0.5284, 0.8444], "pass": [0.0, 0.0]}, "RB": {"rec": [1.4702, 0.0289, 0.7855], "rush": [0.5125, 0.7394], "pass": [0.0, 0.0]}, "QB": {"rec": [1.394, 0.0175, 1.2808], "rush": [0.5422, 1.0169], "pass": [0.3119, 0.7469]}}, "half": {"WR": {"rec": [0.9669, 0.0271, 1.1997], "rush": [0.5284, 0.8444], "pass": [0.0, 0.0]}, "TE": {"rec": [0.9503, 0.0328, 1.5542], "rush": [0.5284, 0.8444], "pass": [0.0, 0.0]}, "RB": {"rec": [1.0752, 0.0357, 0.7859], "rush": [0.5125, 0.7394], "pass": [0.0, 0.0]}, "QB": {"rec": [0.9826, 0.0256, 1.3169], "rush": [0.5422, 1.0169], "pass": [0.3119, 0.7469]}}, "std": {"WR": {"rec": [0.5629, 0.0348, 1.2416], "rush": [0.5284, 0.8444], "pass": [0.0, 0.0]}, "TE": {"rec": [0.5338, 0.0406, 1.579], "rush": [0.5284, 0.8444], "pass": [0.0, 0.0]}, "RB": {"rec": [0.6803, 0.0425, 0.7864], "rush": [0.5125, 0.7394], "pass": [0.0, 0.0]}, "QB": {"rec": [0.5712, 0.0338, 1.353], "rush": [0.5422, 1.0169], "pass": [0.3119, 0.7469]}}}

PARAMS = {  # tuned on 2024, validated on 2025 (see backtest)
    "decay": 0.9,                 # recency weight per older game (usage share)
    "eff_decay": 0.97,            # recency weight per older game (efficiency)
    "prior_season_w": 0.5,        # last season's games count this much for efficiency
    "team_decay": 0.7,            # team volume recency
    "team_prior_games": 2.0,      # shrink team volume toward league average
    "same_pos_share": 0.8,        # vacated share that goes to same-position teammates
    "pace": 0.008,                # volume change per point of game total vs league avg
    "script_pass": 0.0,           # pass volume change per point of spread (backtest: no effect)
    "script_run": 0.03,           # run volume change per point of spread (favorites run more)
    "k_rec": 40.0,                # targets of 'expected' efficiency mixed into each player
    "k_rush": 80.0,               # carries, same idea
    "k_pass": 150.0,              # pass attempts, same idea
    "def_prior": 8.0,             # games of league-average defense mixed in
    "def_weight": 0.25,           # how hard matchup bites (backtest: weak signal, kept light)
    "env_weight": 0.5,            # how hard implied points vs normal bites
    "env_prior": 3.0,             # games of league-average scoring mixed in
    "q_mult": 0.93,               # questionable players
}

OUT_STATUSES = {"out", "ir", "pup", "sus", "na", "dnr", "cov", "doubtful"}


def comp_points(r, sc):
    rec = r["rec"] * REC_VALUE[sc] + r["rec_yd"] / 10 + 6 * r["rec_td"]
    rush = r["rush_yd"] / 10 + 6 * r["rush_td"] - 2 * r["fum_lost"] + 2 * r.get("two_pt", 0)
    pas = r["pass_yd"] / 25 + 4 * r["pass_td"] - 2 * r["pass_int"]
    return rec, rush, pas


def x_points(r, pos, sc):
    """Expected points from usage alone (the 'should have scored' number)."""
    c = COEF[sc].get(pos, COEF[sc]["WR"])
    rec = c["rec"][0] * r["tgt"] + c["rec"][1] * r["air"] + c["rec"][2] * r["rz_tgt"]
    rush = c["rush"][0] * r["car"] + c["rush"][1] * r["rz_car"]
    pas = c["pass"][0] * r["patt"] + c["pass"][1] * r["rz_patt"]
    return rec, rush, pas


def _wavg(pairs):
    tw = sum(w for w, _ in pairs)
    return sum(w * v for w, v in pairs) / tw if tw else 0.0


class Projector:
    def __init__(self, rows, season, week, params=None, prior_rows=None):
        """rows: this season's player-games before `week`. prior_rows: last season."""
        self.p = dict(PARAMS, **(params or {}))
        self.season, self.week = season, week
        self.rows = [r for r in rows if r["season"] == season and r["week"] < week]
        self.prior = prior_rows or []
        self._index()

    # ── precompute team and player tables ────────────────────────────────────
    def _index(self):
        tg = defaultdict(lambda: {"tgt": 0.0, "car": 0.0, "patt": 0.0})
        for r in self.rows:
            t = tg[(r["team"], r["week"])]
            t["tgt"] += r["tgt"]; t["car"] += r["car"]; t["patt"] += r["patt"]
        self.team_game = tg
        self.team_weeks = defaultdict(list)
        for (team, wk) in tg:
            self.team_weeks[team].append(wk)
        for t in self.team_weeks:
            self.team_weeks[t].sort(reverse=True)
        n = max(len(tg), 1)
        self.lg = {k: sum(v[k] for v in tg.values()) / n for k in ("tgt", "car", "patt")}
        if not tg:
            self.lg = {"tgt": 31.0, "car": 26.0, "patt": 33.0}

        self.by_player = defaultdict(list)
        for r in self.rows:
            self.by_player[r["pid"]].append(r)
        for v in self.by_player.values():
            v.sort(key=lambda r: r["week"], reverse=True)
        self.prior_by_player = defaultdict(list)
        for r in self.prior:
            self.prior_by_player[r["pid"]].append(r)
        for v in self.prior_by_player.values():
            v.sort(key=lambda r: r["week"], reverse=True)

        # points allowed to each position, per defense game, per scoring
        allowed = defaultdict(float)
        dgames = defaultdict(set)
        for r in self.rows:
            for sc in SCORINGS:
                allowed[(r["opp"], r["pos"], sc)] += sum(comp_points(r, sc))
            dgames[r["opp"]].add(r["week"])
        self.def_games = {t: len(w) for t, w in dgames.items()}
        self.def_allowed = allowed
        tot_games = sum(self.def_games.values()) or 1
        self.lg_allowed = defaultdict(float)
        for (opp, pos, sc), v in allowed.items():
            self.lg_allowed[(pos, sc)] += v / tot_games

    # ── team volume this week ────────────────────────────────────────────────
    def team_volume(self, team, line):
        p = self.p
        wks = self.team_weeks.get(team, [])
        vol = {}
        for k in ("tgt", "car", "patt"):
            pairs = [(p["team_decay"] ** i, self.team_game[(team, w)][k]) for i, w in enumerate(wks)]
            tw = sum(w for w, _ in pairs)
            vol[k] = (sum(w * v for w, v in pairs) + p["team_prior_games"] * self.lg[k]) / (tw + p["team_prior_games"])
        if line and line.get("total") is not None:
            pace = 1 + p["pace"] * (line["total"] - 44.5)
            spd = line.get("spread") or 0.0  # negative = favorite
            pass_m = min(1.25, max(0.8, pace * (1 + p["script_pass"] * spd)))
            run_m = min(1.25, max(0.8, pace * (1 - p["script_run"] * spd)))
            vol["tgt"] *= pass_m; vol["patt"] *= pass_m; vol["car"] *= run_m
        return vol

    # ── one player's base usage ──────────────────────────────────────────────
    def player_shares(self, pid, team):
        games = [g for g in self.by_player.get(pid, []) if g["team"] == team] or self.by_player.get(pid, [])
        if not games:
            return None
        d = self.p["decay"]
        sh = {}
        for k in ("tgt", "car", "patt"):
            pairs = []
            for i, g in enumerate(games):
                tot = self.team_game[(g["team"], g["week"])][k]
                pairs.append((d ** i, g[k] / tot if tot else 0.0))
            sh[k] = _wavg(pairs)
        # how present he's been in the team's most recent games (for vacated share)
        recent = self.team_weeks.get(team, [])[:3]
        played = {g["week"] for g in games}
        ws = [d ** i for i in range(len(recent))]
        sh["presence"] = (sum(w for w, wk in zip(ws, recent) if wk in played) / sum(ws)) if ws else 1.0
        # trend: last 2 games vs everything before
        def share_of(gs, k):
            vals = []
            for g in gs:
                tot = self.team_game[(g["team"], g["week"])][k]
                vals.append(g[k] / tot if tot else 0.0)
            return sum(vals) / len(vals) if vals else None
        main = "patt" if games[0]["pos"] == "QB" else ("car" if games[0]["pos"] == "RB" else "tgt")
        if games[0]["pos"] == "RB":
            main = "opp"
        if main == "opp":
            cur = [g["tgt"] / max(1, self.team_game[(g["team"], g["week"])]["tgt"]) + g["car"] / max(1, self.team_game[(g["team"], g["week"])]["car"]) for g in games[:2]]
            old = [g["tgt"] / max(1, self.team_game[(g["team"], g["week"])]["tgt"]) + g["car"] / max(1, self.team_game[(g["team"], g["week"])]["car"]) for g in games[2:]]
            sh["trend"] = (sum(cur) / len(cur) - sum(old) / len(old)) / 2 if cur and old else None
        else:
            a, b = share_of(games[:2], main), share_of(games[2:], main)
            sh["trend"] = (a - b) if a is not None and b is not None else None
        return sh

    # ── efficiency (points per opportunity) ──────────────────────────────────
    def efficiency(self, pid, pos, sc):
        p = self.p
        games = list(self.by_player.get(pid, []))
        prior = self.prior_by_player.get(pid, [])
        d = p["eff_decay"]
        acc = {"rec": [0, 0, 0], "rush": [0, 0, 0], "pass": [0, 0, 0]}  # actual, opps, expected
        seq = [(g, 1.0) for g in games] + [(g, p["prior_season_w"]) for g in prior]
        for i, (g, base) in enumerate(seq):
            w = base * d ** i
            a = comp_points(g, sc)
            x = x_points(g, pos, sc)
            for j, (k, opp) in enumerate((("rec", "tgt"), ("rush", "car"), ("pass", "patt"))):
                acc[k][0] += w * a[j]; acc[k][1] += w * g[opp]; acc[k][2] += w * x[j]
        c = COEF[sc].get(pos, COEF[sc]["WR"])
        default = {"rec": c["rec"][0] + c["rec"][1] * 8 + c["rec"][2] * 0.12,
                   "rush": c["rush"][0] + c["rush"][1] * 0.12,
                   "pass": c["pass"][0] + c["pass"][1] * 0.12}
        out = {}
        for k, K in (("rec", p["k_rec"]), ("rush", p["k_rush"]), ("pass", p["k_pass"])):
            actual, opps, exp = acc[k]
            xrate = exp / opps if opps > 0 else default[k]
            out[k] = (actual + K * xrate) / (opps + K)
            out["x_" + k] = xrate
        return out

    def def_factor(self, opp, pos, sc):
        p = self.p
        lg = self.lg_allowed.get((pos, sc), 0)
        n = self.def_games.get(opp, 0)
        if not lg or not opp:
            return 1.0
        f = (self.def_allowed.get((opp, pos, sc), 0) + p["def_prior"] * lg) / ((n + p["def_prior"]) * lg)
        return f ** p["def_weight"]

    def env_factor(self, team, line, team_pts):
        p = self.p
        if not line or line.get("impl") is None:
            return 1.0
        pts = team_pts.get(team, [])
        lg = 22.5
        avg = (sum(pts) + p["env_prior"] * lg) / (len(pts) + p["env_prior"])
        return (line["impl"] / avg) ** p["env_weight"]

    # ── full week ────────────────────────────────────────────────────────────
    def project(self, players, lines, team_pts):
        """
        players: {pid: {"pos","team","status"}} for everyone on a roster this week.
                 status: "" / "questionable" / "out" etc. Optional "depth" (int).
        lines:   {team: {"opp","impl","total","spread"}}; missing team = bye.
        team_pts:{team: [points scored in each game so far]}
        """
        p = self.p
        res = {}
        by_team = defaultdict(list)
        for pid, info in players.items():
            by_team[info["team"]].append(pid)

        for team, pids in by_team.items():
            line = lines.get(team)
            base = {pid: self.player_shares(pid, team) for pid in pids}
            st = {pid: (players[pid].get("status") or "").lower() for pid in pids}
            out = {pid for pid in pids if st[pid] in OUT_STATUSES}
            avail = [pid for pid in pids if pid not in out]
            shares = {pid: {k: (base[pid][k] if base[pid] else 0.0) for k in ("tgt", "car", "patt")} for pid in pids}
            boost = defaultdict(lambda: defaultdict(float))  # pid -> source -> share gained

            for k in ("tgt", "car", "patt"):
                for o in out:
                    if not base[o]:
                        continue
                    vac = base[o][k] * base[o]["presence"]
                    if vac <= 0.005:
                        continue
                    pos = players[o]["pos"]
                    same_frac = 1.0 if k == "patt" else p["same_pos_share"]
                    same = [a for a in avail if players[a]["pos"] == pos]
                    same_tot = sum(shares[a][k] for a in same)
                    if same and same_tot <= 0 and k == "patt":
                        # backup QB with no history: next on the depth chart takes over
                        nxt = min(same, key=lambda a: players[a].get("depth") or 99)
                        shares[nxt][k] += vac; boost[nxt][o] += vac
                        continue
                    targets = [(same, vac * same_frac, same_tot), (avail, vac * (1 - same_frac), sum(shares[a][k] for a in avail))]
                    if not same or same_tot <= 0:
                        targets = [(avail, vac, sum(shares[a][k] for a in avail))]
                    for group, amount, tot in targets:
                        if tot <= 0 or amount <= 0:
                            continue
                        gains = {a: amount * shares[a][k] / tot for a in group}
                        for a, gval in gains.items():
                            shares[a][k] += gval
                            if k != "patt" or players[a]["pos"] == "QB":
                                boost[a][o] += gval
                total = sum(shares[a][k] for a in avail)
                if total > 1.0:
                    for a in avail:
                        shares[a][k] /= total

            vol = self.team_volume(team, line)
            for pid in pids:
                info = players[pid]
                pos = info["pos"]
                r = {"pos": pos, "team": team, "status": st[pid], "has_hist": bool(base[pid])}
                if line is None:
                    r.update({"bye": True, **{sc: 0.0 for sc in SCORINGS}})
                    res[pid] = r
                    continue
                if pid in out:
                    r.update({"out": True, **{sc: 0.0 for sc in SCORINGS}})
                    res[pid] = r
                    continue
                opp_vol = {k: shares[pid][k] * vol[k] for k in ("tgt", "car", "patt")}
                envf = self.env_factor(team, line, team_pts)
                qm = p["q_mult"] if st[pid] == "questionable" else 1.0
                for sc in SCORINGS:
                    eff = self.efficiency(pid, pos, sc)
                    raw = opp_vol["tgt"] * eff["rec"] + opp_vol["car"] * eff["rush"] + opp_vol["patt"] * eff["pass"]
                    r[sc] = round(raw * self.def_factor(line.get("opp"), pos, sc) * envf * qm, 2)
                r["opps"] = {k: round(v, 1) for k, v in opp_vol.items()}
                r["share"] = {k: round(shares[pid][k], 3) for k in ("tgt", "car", "patt")}
                r["base_share"] = {k: round(base[pid][k], 3) for k in ("tgt", "car", "patt")} if base[pid] else None
                r["trend"] = round(base[pid]["trend"], 3) if base[pid] and base[pid]["trend"] is not None else None
                gains = sorted(((s, v) for s, v in boost[pid].items() if v > 0.01), key=lambda x: -x[1])
                r["boost"] = [(s, round(v, 3)) for s, v in gains[:3]]
                r["def_f"] = round(self.def_factor(line.get("opp"), pos, "ppr"), 3)
                r["env_f"] = round(envf, 3)
                res[pid] = r
        return res

    # ── luck check: expected vs actual over recent games ─────────────────────
    def luck(self, pid, n=3):
        games = self.by_player.get(pid, [])[:n]
        if not games:
            return None
        pos = games[0]["pos"]
        out = {"games": len(games)}
        for sc in SCORINGS:
            x = sum(sum(x_points(g, pos, sc)) for g in games) / len(games)
            a = sum(sum(comp_points(g, sc)) for g in games) / len(games)
            out["x_" + sc] = round(x, 1)
            out["a_" + sc] = round(a, 1)
        return out
