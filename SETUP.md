# Diamond Scout setup (one time, about 5 minutes)

After this, the page updates itself every morning. No more running scripts or pasting JSON.

## 1. Create the repo
1. Go to github.com, click **+** (top right), then **New repository**.
2. Name it `diamond-scout`. Set it to **Public** (free GitHub Pages needs public).
3. Don't add a README. Click **Create repository**.

## 2. Add the files
Either tell Claude "the repo is created" and it will push the files for you, or upload them yourself:
1. On the new repo page click **uploading an existing file**.
2. Drag in `diamond_scout_startsit.html`, `update_stats.py`, `index.html`, and `SETUP.md`. Commit.
3. The workflow file has to live in a folder. Click **Add file > Create new file**, type the name
   `.github/workflows/update.yml` (the slashes create the folders), paste in the contents of
   `update.yml`, and commit.

## 3. Turn on the website
1. Repo **Settings > Pages**.
2. Under "Build and deployment", Source: **Deploy from a branch**, Branch: **main**, folder **/ (root)**. Save.
3. After a minute the page is live at `https://brycewaw34.github.io/start_sit/`. Bookmark it.

## 4. Kick off the first update
1. Repo **Actions** tab. If it asks, click the button to enable workflows.
2. Click **Update Diamond Scout data**, then **Run workflow**.
3. Green check means it worked. Refresh your bookmark and the header shows the new "updated" time.

## What runs and when
- **Every day around 6am Pacific:** new stats, injury tags, Vegas lines, and projections.
- **Sunday around 9am and 12:30pm Pacific:** refreshes after early and late game inactives come out.
- **Tuesdays:** the model report (`backtest.html`) re-grades Sleeper vs our model vs actual results.
- If Sleeper or another source is down, the run fails safely and the page keeps its last good data.
  GitHub emails you about failed runs.

## How projections work
- **Final projection = 75% Sleeper + 25% our model.** That mix tested best (see the model report).
  The weight updates itself from the Tuesday report.
- **Our model (`model.py`):** each player's recent share of team targets/carries/attempts, times
  the team's expected volume this week (adjusted for the Vegas total and spread), times points per
  opportunity (what that kind of usage usually produces, blended toward the player's own results).
  Then light adjustments for matchup and implied team points.
- **Injuries:** when a teammate is out, most of his share goes to same-position teammates.
  A missing QB's whole role goes to the next QB on the depth chart.
- **Due for more / Running hot:** expected points from usage vs actual points over the last 3 games.
  In testing, players 4+ pts/G below expectation scored about 3 more the next week; players 4+ above
  scored about 5 fewer.
- **Advanced stats** (separation, YAC over expected, rush yards over expected) come from NFL Next Gen
  Stats via nflverse. They're shown for context but not weighted, since testing shows they add little
  once you know a player's usage.

## Files
- `diamond_scout_startsit.html`: the tool. Data at the top is rewritten automatically.
- `update_stats.py`: daily updater. `model.py`: projection model. `datasrc.py`: data sources.
- `backtest.py` / `backtest.html`: the head-to-head test and its report.
- `data/`: cached past seasons so runs stay fast.

## Manual refresh
Actions tab > Update Diamond Scout data > Run workflow. Or locally: `python update_stats.py`.
