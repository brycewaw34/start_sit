Project: Diamond Scout (fantasy start/sit + NFL pick'em)

Owner: Bryce (Sleeper username bdubs34). Two pages, hosted free on GitHub Pages and updated automatically. No manual weekly updates.

Live site
- Start/Sit: https://brycewaw34.github.io/start_sit/
- Pick'em: https://brycewaw34.github.io/start_sit/picks.html
- Model report (backtest + live accuracy): https://brycewaw34.github.io/start_sit/backtest.html
- Repo: github.com/brycewaw34/start_sit (Claude GitHub app installed, Claude can push directly)

How it updates
- GitHub Actions runs update_stats.py daily ~6am PT, plus Sunday ~9am and ~12:30pm PT after inactives. Also runs on any push that changes the code.
- backtest.py runs Tuesdays ~8am PT and regrades everything.
- Python standard library only, nothing to install. If a data source fails, the run exits without touching the pages.
- Data is baked into each HTML file between /*@@DATA@@*/ and /*@@END@@*/ markers. Never hand-edit that block. The pages make no network calls.

Data sources (all free)
- Sleeper: player database, weekly stats (api.sleeper.app/stats/nfl/{season}/{week}?season_type=regular&position[]=X, includes team and opponent), weekly projections (same path with /projections/), current week (/v1/state/nfl), league settings and rosters.
- ESPN scoreboard: Vegas lines (DraftKings), kickoff times, scores. ESPN fantasy projections are collected but only blended in once live grading shows they help.
- nflverse (GitHub releases): schedule with closing lines and scores, injury reports, Next Gen Stats (separation, YAC over expected, rush yards over expected).
- Open-Meteo: weather forecasts for open-air stadiums.

Start/sit projections
- Final projection = 75% Sleeper + 25% our model (model.py). The blend weight comes from the Tuesday backtest.
- Model: recency-weighted share of team targets/carries/attempts x team volume (adjusted for Vegas total and spread) x points per opportunity (expected from usage, shrunk toward the player's own results), plus light matchup, implied-points, and wind adjustments.
- Injuries: an out teammate's share goes mostly to same-position teammates; a missing QB's role goes to the next QB on the depth chart.
- Backtest (2025 + 2026 so far, about 3,100 player-weeks): final projection 67.2% of start/sit pairs right, Sleeper alone 67.2%, our model alone 65.1%, last-3-games average 62.1%. Final has the smallest average miss (5.50 pts).
- Findings: defense-vs-position matchups barely predict anything (about a 1 pt RB swing between soft and tough defenses), so they're weighted lightly. "Due for more" (usage says he should've scored 4+ more per game) predicted about +3 pts the next week; "running hot" about -5.
- League: G-unit regular (Sleeper league 1389704120419495936), 10 teams, full PPR, 6-pt pass TD, -1 INT, yardage bonuses. Lineup: QB, RB, RB, WR, WR, FLEX, W/T flex, K, DEF. "My League" scoring is the default. config.json holds the league ID and my team owner.
- Page also shows: best lineup for my team vs my current Sleeper lineup, floor/ceiling (20th-80th percentile), roster tags (MINE / FA / other team), weather, Next Gen Stats.

Pick'em
- My pool: straight-up winners, no confidence points, weekly and season prizes, small group run by text.
- Win probability = Phi(-spread / 11.25), fit on 2021-2025 (as accurate as moneylines).
- The page simulates the week against my pool size to find upset picks worth taking. Modes: Most wins, Balanced (max 2 cheap upsets), Win the week. In a small pool, upsets rarely help.

Files
- diamond_scout_startsit.html, picks.html, backtest.html: the pages
- update_stats.py (daily updater), model.py (projections), datasrc.py (data sources), pickem.py (pick'em data), backtest.py (testing)
- config.json, data/ (cached seasons, live projection log, backtest summary), SETUP.md (full notes)

When I ask for changes: edit the repo, test before pushing, push to main, and confirm the GitHub run succeeded. Be honest about what the data does and doesn't show. No em dashes.
