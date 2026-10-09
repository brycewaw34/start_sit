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
3. After a minute the page is live at `https://<your-username>.github.io/diamond-scout/`. Bookmark it.

## 4. Kick off the first update
1. Repo **Actions** tab. If it asks, click the button to enable workflows.
2. Click **Update Diamond Scout data**, then **Run workflow**.
3. Green check means it worked. Refresh your bookmark and the header shows the new "updated" time.

## What runs and when
- Every day around 6am Pacific, plus Sunday around 9am Pacific for final injury news.
- Each run pulls: current week, every player's last 3 games (snaps, volume, red zone, fantasy pts),
  defensive ranks vs each position (last 4 weeks), injury tags, and DraftKings lines via ESPN.
- If Sleeper or ESPN is down, the run fails safely and the page keeps its last good data.
  GitHub emails you about failed runs, so you'll know.

## Manual refresh
Actions tab > Update Diamond Scout data > Run workflow. Or locally: `python update_stats.py`.

## Notes
- Stat boxes are still editable. Anything you type overrides the auto-filled number until you
  pick a different player.
- The header turns red if the data is more than 2 days old, which means the daily run is failing.
