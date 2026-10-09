#!/usr/bin/env python3
"""
Head-to-head test: Sleeper's projections vs Diamond Scout's model vs what
players actually scored, for every week of last season and this season so far.

Each week is predicted using only what was known before that week:
games already played, that week's Vegas lines, and that week's official
injury report. Writes backtest.html (the report page) and data/backtest_summary.json.

Run: python backtest.py         (GitHub Actions runs it weekly)
"""

import itertools
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import datasrc as ds
import model

HERE = Path(__file__).resolve().parent
OUT_HTML = HERE / "backtest.html"
OUT_JSON = HERE / "data" / "backtest_summary.json"
BLENDS = (0.25, 0.5, 0.75)  # share of the final number that comes from our model


def week_players(rows_before, injuries_week, gmap, team_weeks, by_player):
    last = {}
    for r in sorted(rows_before, key=lambda r: r["week"]):
        last[r["pid"]] = r
    sleeper_status = {gmap[g]: s for g, s in injuries_week.items() if g in gmap}
    players = {}
    for pid, r in last.items():
        status = sleeper_status.get(pid, "")
        tw = team_weeks.get(r["team"], [])[:2]
        played = {g["week"] for g in by_player.get(pid, [])}
        if not status and len(tw) == 2 and not (set(tw) & played):
            status = "ir"  # missed two straight with no report: almost always IR
        players[pid] = {"pos": r["pos"], "team": r["team"], "status": status}
    return players


def evaluate_season(season, weeks, rows, prior, proj_by_week, games, injuries, gmap, names):
    recs = []
    for W in weeks:
        hist = [r for r in rows if r["week"] < W]
        actual = {r["pid"]: r for r in rows if r["week"] == W}
        if not hist or not actual:
            continue
        P = model.Projector(hist, season, W, prior_rows=prior)
        players = week_players(hist, injuries.get(W, {}), gmap, P.team_weeks, P.by_player)
        lines = ds.lines_from_games(games, W)
        proj = P.project(players, lines, ds.team_points(games, W))
        sp = proj_by_week.get(W, {})
        for pid, a in actual.items():
            pr, g = proj.get(pid), P.by_player.get(pid, [])
            if not pr or pr.get("bye") or len(g) < 2 or players[pid]["team"] != a["team"] or pid not in sp:
                continue
            l3 = g[:3]
            l3avg = sum(x["pts_ppr"] for x in l3) / len(l3)
            if max(l3avg, pr["ppr"], sp[pid]["ppr"]) < 6:
                continue  # nobody's starting him
            recs.append({
                "season": season, "week": W, "pid": pid, "name": names.get(pid, pid), "pos": a["pos"],
                "team": a["team"], "status": players[pid]["status"],
                "actual": round(a["pts_ppr"], 2), "model": pr["ppr"], "sleeper": round(sp[pid]["ppr"], 2),
                "L3": round(l3avg, 2), "seasonavg": round(sum(x["pts_ppr"] for x in g) / len(g), 2),
            })
        print(f"  {season} week {W}: {sum(1 for r in recs if r['season'] == season and r['week'] == W)} players scored")
    return recs


def metrics(recs, method):
    mae = st.mean(abs(r[method] - r["actual"]) for r in recs)
    groups = defaultdict(list)
    for r in recs:
        groups[(r["season"], r["week"], r["pos"])].append(r)
    right = total = c_right = c_total = 0
    for lst in groups.values():
        for x, y in itertools.combinations(lst, 2):
            if x["actual"] == y["actual"]:
                continue
            ok = (x[method] - y[method]) * (x["actual"] - y["actual"]) > 0
            right += ok; total += 1
            if abs(x["sleeper"] - y["sleeper"]) < 3:  # Sleeper itself had them close
                c_right += ok; c_total += 1
    return {"mae": round(mae, 2), "pairs": round(right / total, 4) if total else None,
            "close": round(c_right / c_total, 4) if c_total else None, "n": len(recs), "n_pairs": total,
            "n_close": c_total}


def main():
    state = ds.fetch_json(ds.SLEEPER_STATE)
    season, cur_week = int(state["season"]), int(state["week"])
    print(f"Backtest. Current: {season} week {cur_week}")
    db = ds.fetch_json(ds.SLEEPER_PLAYERS)
    names = {pid: (p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}").strip() for pid, p in db.items()}
    gmap = ds.gsis_map(db)

    plan = [(season - 1, list(range(4, 19)))]
    if cur_week > 3:
        plan.append((season, list(range(3, cur_week))))

    recs = []
    for yr, weeks in plan:
        print(f"Season {yr}: loading")
        prior = ds.cached_season(yr - 1)
        rows = ds.cached_season(yr) if yr < season else [r for w in range(1, cur_week) for r in ds.sleeper_rows(yr, w)]
        projs = ds.cached_projections(yr, weeks)
        games = ds.nfl_games(yr)
        injuries = ds.nfl_injuries(yr)
        recs += evaluate_season(yr, weeks, rows, prior, projs, games, injuries, gmap, names)

    for w in BLENDS:
        for r in recs:
            r[f"blend{int(w * 100)}"] = round(w * r["model"] + (1 - w) * r["sleeper"], 2)
    methods = ["sleeper", "model"] + [f"blend{int(w * 100)}" for w in BLENDS] + ["seasonavg", "L3"]

    summary = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "overall": {}, "by_season": {}, "by_pos": {}}
    for m in methods:
        summary["overall"][m] = metrics(recs, m)
        for yr in sorted({r["season"] for r in recs}):
            summary["by_season"].setdefault(str(yr), {})[m] = metrics([r for r in recs if r["season"] == yr], m)
        for pos in ds.POSITIONS:
            sub = [r for r in recs if r["pos"] == pos]
            if sub:
                summary["by_pos"].setdefault(pos, {})[m] = metrics(sub, m)
    best_blend = max(BLENDS, key=lambda w: summary["overall"][f"blend{int(w * 100)}"]["pairs"])
    summary["best_blend"] = best_blend

    OUT_JSON.parent.mkdir(exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=1))
    write_html(summary, recs, best_blend)
    print(json.dumps(summary["overall"], indent=1))
    print("best blend (model share):", best_blend)


LABELS = {"sleeper": "Sleeper projection", "model": "Our model", "blend25": "Blend 25% model",
          "blend50": "Blend 50/50", "blend75": "Blend 75% model", "seasonavg": "Season average", "L3": "Last 3 games avg"}


def write_html(summary, recs, best_blend):
    final = f"blend{int(best_blend * 100)}"
    for r in recs:
        r["final"] = r[final]
    slim = [[r["season"], r["week"], r["name"], r["pos"], r["team"], r["sleeper"], r["model"], r["final"], r["actual"], r["status"]]
            for r in sorted(recs, key=lambda r: (-r["season"], -r["week"], -r["actual"]))]
    html = TEMPLATE.replace("__SUMMARY__", json.dumps(summary)).replace("__ROWS__", json.dumps(slim, separators=(",", ":"))) \
                   .replace("__LABELS__", json.dumps(LABELS)).replace("__FINAL__", final)
    OUT_HTML.write_text(html, encoding="utf-8")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Diamond Scout — Model Report</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;800&family=IBM+Plex+Mono:wght@400;500&family=Barlow:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#05080d;--s1:#0b0f17;--s2:#111722;--b1:#1c2535;--b2:#27364f;--gold:#f0b429;--green:#3dd68c;--red:#f16b6b;--blue:#58a6e8;--text:#d4dce8;--t2:#8494ae;--t3:#5a6a82}
*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--text);font-family:'Barlow',sans-serif;font-size:14px}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px}
h1{font-family:'Barlow Condensed';font-weight:800;font-size:1.8rem;letter-spacing:1px;text-transform:uppercase}
h2{font-family:'Barlow Condensed';font-weight:800;font-size:1.15rem;letter-spacing:1px;text-transform:uppercase;margin:28px 0 10px;color:var(--gold)}
.sub{font-family:'IBM Plex Mono';font-size:.7rem;color:var(--t3);margin:4px 0 18px}
a{color:var(--gold)}
p.note{color:var(--t2);line-height:1.6;max-width:760px;margin-bottom:10px}
table{width:100%;border-collapse:collapse;background:var(--s1);border:1px solid var(--b1);border-radius:4px;overflow:hidden}
th,td{padding:8px 10px;text-align:right;font-family:'IBM Plex Mono';font-size:.72rem;border-bottom:1px solid var(--b1);white-space:nowrap}
th{background:var(--s2);color:var(--t3);font-weight:500;cursor:pointer;user-select:none}
td:first-child,th:first-child{text-align:left}
tr.best td{color:var(--green)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.card{background:var(--s2);border:1px solid var(--b1);border-radius:4px;padding:14px}
.card .k{font-family:'IBM Plex Mono';font-size:.6rem;color:var(--t3);letter-spacing:1.5px;text-transform:uppercase}
.card .v{font-family:'Barlow Condensed';font-weight:800;font-size:2rem;color:var(--gold)}
.card .d{font-size:.8rem;color:var(--t2)}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,select{background:var(--s1);border:1px solid var(--b2);color:var(--text);padding:7px 10px;border-radius:3px;font-family:'IBM Plex Mono';font-size:.75rem}
.scroll{overflow-x:auto}
.good{color:var(--green)}.bad{color:var(--red)}
</style></head><body><div class="wrap">
<h1>Model Report</h1>
<div class="sub" id="gen"></div>
<p class="note">Every week below was predicted using only what was known before kickoff: games already played, that week's Vegas lines, and that week's official injury report. "Start/sit calls right" takes every pair of players at the same position in the same week and checks whether the projection ranked them in the right order. "Close calls" are pairs Sleeper had within 3 points of each other, the decisions that actually keep you up at night. PPR scoring, players anyone would plausibly start.</p>
<p class="note"><a href="diamond_scout_startsit.html">Back to the start/sit tool</a></p>
<div class="cards" id="cards"></div>
<h2>Overall</h2><div class="scroll"><table id="overall"></table></div>
<h2>By position</h2><div class="scroll" id="bypos"></div>
<h2>Every player, every week</h2>
<div class="controls"><input id="q" placeholder="Search player…"><select id="fs"></select><select id="fw"></select><select id="fp"><option value="">All pos</option><option>QB</option><option>RB</option><option>WR</option><option>TE</option></select></div>
<div class="scroll"><table id="rows"></table></div>
<div class="sub" id="count"></div>
</div>
<script>
const S=__SUMMARY__, ROWS=__ROWS__, L=__LABELS__, FINAL="__FINAL__";
const pct=v=>v==null?'—':(v*100).toFixed(1)+'%';
document.getElementById('gen').textContent='Generated '+new Date(S.generated).toLocaleString()+' · '+S.overall.model.n+' player-weeks · '+S.overall.model.n_pairs.toLocaleString()+' start/sit pairs';
const o=S.overall, f=o[FINAL];
document.getElementById('cards').innerHTML=[
 ['Final projection (used in the tool)',pct(f.pairs),`start/sit calls right · ${L[FINAL]}`],
 ['Sleeper alone',pct(o.sleeper.pairs),'start/sit calls right'],
 ['Our model alone',pct(o.model.pairs),'start/sit calls right'],
 ['Close calls, final vs Sleeper',`${pct(f.close)}`,`vs ${pct(o.sleeper.close)} for Sleeper`],
].map(([k,v,d])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join('');
function table(sum){
  const ms=Object.keys(sum); const best=ms.reduce((a,b)=>sum[b].pairs>sum[a].pairs?b:a);
  return '<tr><th>Method</th><th>Start/sit calls right</th><th>Close calls right</th><th>Avg miss (pts)</th><th>Player-weeks</th></tr>'+
   ms.map(m=>`<tr class="${m===best?'best':''}"><td>${L[m]||m}${m===FINAL?' ★':''}</td><td>${pct(sum[m].pairs)}</td><td>${pct(sum[m].close)}</td><td>${sum[m].mae}</td><td>${sum[m].n}</td></tr>`).join('');
}
document.getElementById('overall').innerHTML=table(o);
document.getElementById('bypos').innerHTML=Object.entries(S.by_pos).map(([p,s])=>`<h2 style="font-size:.9rem;color:var(--text)">${p}</h2><table>${table(s)}</table>`).join('');
const seasons=[...new Set(ROWS.map(r=>r[0]))], weeks=[...new Set(ROWS.map(r=>r[1]))].sort((a,b)=>b-a);
document.getElementById('fs').innerHTML='<option value="">All seasons</option>'+seasons.map(s=>`<option>${s}</option>`).join('');
document.getElementById('fw').innerHTML='<option value="">All weeks</option>'+weeks.map(w=>`<option>${w}</option>`).join('');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let sortK=null,sortD=-1;
function render(){
  const q=document.getElementById('q').value.toLowerCase(),fs=document.getElementById('fs').value,fw=document.getElementById('fw').value,fp=document.getElementById('fp').value;
  let r=ROWS.filter(x=>(!q||x[2].toLowerCase().includes(q))&&(!fs||x[0]==fs)&&(!fw||x[1]==fw)&&(!fp||x[3]==fp));
  if(sortK!=null) r=[...r].sort((a,b)=>(a[sortK]>b[sortK]?1:-1)*sortD);
  const shown=r.slice(0,400);
  const H=['Season','Wk','Player','Pos','Team','Sleeper','Model','Final','Actual','Final miss'];
  document.getElementById('rows').innerHTML='<tr>'+H.map((h,i)=>`<th data-k="${i}">${h}</th>`).join('')+'</tr>'+shown.map(x=>{
    const miss=x[8]-x[7], best=Math.abs(x[5]-x[8])<Math.abs(x[7]-x[8])?'sleeper':'final';
    return `<tr><td>${x[0]}</td><td>${x[1]}</td><td style="text-align:left">${esc(x[2])}${x[9]?` <span class="bad">${esc(x[9].toUpperCase())}</span>`:''}</td><td>${x[3]}</td><td>${x[4]}</td><td class="${best==='sleeper'?'good':''}">${x[5].toFixed(1)}</td><td>${x[6].toFixed(1)}</td><td class="${best==='final'?'good':''}">${x[7].toFixed(1)}</td><td>${x[8].toFixed(1)}</td><td class="${miss>=0?'good':'bad'}">${miss>=0?'+':''}${miss.toFixed(1)}</td></tr>`}).join('');
  document.getElementById('count').textContent=`Showing ${shown.length} of ${r.length}. Green projection = closer to what actually happened. Click a column to sort.`;
  document.querySelectorAll('#rows th').forEach(th=>th.onclick=()=>{const k=+th.dataset.k;sortD=sortK===k?-sortD:-1;sortK=k;render();});
}
['q','fs','fw','fp'].forEach(id=>document.getElementById(id).addEventListener('input',render));
render();
</script></body></html>"""


if __name__ == "__main__":
    main()
