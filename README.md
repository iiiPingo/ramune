# 🫧 Ramune · Stage 1: cloud notifier

Sends a Discord message the moment (well, within a few minutes) a new episode of anything on your
MyAnimeList **Watching** list airs. Runs on GitHub Actions every 15 minutes, 24/7, PC on or off.

```
MAL "Watching" list ──► AniList (idMal → airing schedule) ──► new episodes? ──► Discord webhook
                                                                  │
                                                state/state.json (committed back, no duplicates)
```

## Files

| File | What it does |
|---|---|
| `ramune.py` | The notifier. Python standard library only, nothing to install. |
| `.github/workflows/ramune.yml` | Runs it every 15 min + a manual "Run workflow" button. |
| `state/state.json` | Created on the first run. Remembers what was already announced. |
| `tests/test_ramune.py` | Offline test suite (fake MAL, AniList and Discord): `python3 -m unittest discover -s tests` |

---

## Setup (about 10 minutes)

### 1. Get your two secrets ready (keep them to yourself)

- **MAL Client ID:** <https://myanimelist.net/apiconfig> → *Create ID*. App Type: **other**.
  Redirect URL and Homepage URL can both be your repo URL (e.g. `https://github.com/<you>/ramune`).
  Copy the **Client ID** (you don't need the Client Secret).
- **Discord webhook:** in your private server → *Server Settings → Integrations → Webhooks → New Webhook* →
  pick the channel → name it `Ramune` → *Copy Webhook URL*.

### 2. Create the repo

1. Go to <https://github.com/new>.
2. Name: `ramune`. Visibility: **Public** (see "Public or private?" below).
3. Leave "Add a README" **unchecked**. Click **Create repository**.

### 3. Upload the files

1. Unzip `ramune-stage1.zip`. Open the `ramune` folder so you can see `ramune.py`, `README.md`,
   `.gitignore`, and the `.github` and `state` folders.
2. On the new repo page, click **uploading an existing file**.
3. Select **everything inside** the `ramune` folder (Ctrl+A) and drag it onto the page.
4. Click **Commit changes**.
5. Check that the repo now shows a `.github/workflows/ramune.yml` file. If the `.github` folder didn't come
   through, use *Add file → Create new file*, type `.github/workflows/ramune.yml` as the name, paste the file's
   contents, and commit.

### 4. Add the secrets

Repo → **Settings → Secrets and variables → Actions → Secrets** tab → **New repository secret**:

| Name | Value |
|---|---|
| `MAL_CLIENT_ID` | your MAL Client ID |
| `DISCORD_WEBHOOK_URL` | your webhook URL |

### 5. Add the variables

Same page → **Variables** tab → **New repository variable**:

| Name | Value | Required? |
|---|---|---|
| `MAL_USERNAME` | your MAL username | **yes** |
| `DAILY_SUMMARY` | `true` to get a "today's schedule" message each morning | optional (default off) |
| `EPISODE_ALERTS` | `false` to skip per-episode messages (morning schedule only) | optional (default on) |
| `SCHEDULE_CHANGES` | `false` to stop "schedule change" messages when an episode in today's digest moves or is pulled | optional (default on) |
| `UP_NEXT` | `false` to stop the pinned "Up next" message (one message, edited in place when the list changes) | optional (default on) |
| `SUMMARY_HOUR` | hour for the summary, `0`–`23` local time | optional (default `9`) |
| `TIMEZONE` | e.g. `Asia/Dubai` | optional (default `Asia/Dubai`) |
| `ANNOUNCE_DELAY_MINUTES` | wait this long after the Japan TV air time (see notes) | optional (default `0`) |
| `FAIL_ALERT` | `false` to stop the one-time "can't reach MyAnimeList / AniList" message | optional (default on) |
| `FAIL_ALERT_FROM` / `FAIL_ALERT_TO` | local hours that message may be sent in (`13` to `23` = 1 PM up to 11 PM) | optional (default `13` / `23`) |
| `SUMMARY_LINKS` | `false` to drop the Crunchyroll / Anikoto links from the morning schedule | optional (default on) |
| `ANIKOTO_URL` | Anikoto's current domain, e.g. `anikototv.bz`, or a full search URL with `{q}` for the title, or `off` | optional (default `https://anikototv.bz`) |
| `ANIKOTO_DIRECT` | `false` to skip the Anikoto lookup and always link a title search | optional (default on) |
| `PTW_ALERTS` | `false` to stop Plan to Watch alerts | optional (default on) |

### 6. Run the test

1. Repo → **Actions** tab. If GitHub asks you to enable workflows, click the green button.
2. Click **Ramune** in the left list → **Run workflow** → mode **test** → **Run workflow**.
3. Within about a minute Discord gets a "Ramune is connected" message showing how many shows it's tracking,
   plus a preview of what an episode alert will look like (using your next upcoming episode).
4. Click the finished run → the **Summary** page shows a table of every show with its next episode time.

That's it. From now on it checks every 15 minutes on its own. The first run only records a starting point,
so you won't get spammed with episodes that already aired.

### 7. Make sure your phone actually buzzes

Webhook messages don't @mention you, so in the Discord app:
**long-press the channel → Notification settings → All Messages.**
Discord also holds mobile pushes while you're active on desktop; that's
*User Settings → Notifications → Push Notification Inactive Timeout*.

---

## Manual modes (Actions → Ramune → Run workflow)

| Mode | Does |
|---|---|
| `test` | Normal check + sends a status message and an alert preview |
| `summary` | Normal check + sends today's schedule right now |
| `normal` | Exactly what the 15-minute schedule does |

## How it avoids duplicates and misses

- Every announced episode is saved as `anilistId:episode` in `state/state.json` and committed back, so it's
  never sent twice.
- Each run looks back 72 hours, not just 15 minutes. If GitHub delays or skips runs (it does under load),
  the next run still catches the episode and the alert says how long ago it aired.
- A show you add to your Watching list starts being tracked from that moment. Its old episodes aren't announced.
- If MAL is down, it uses the last known Watching list. If AniList or Discord is down, it retries with backoff,
  and the next run catches up.
- If MyAnimeList or AniList stays unreachable for 8 runs in a row (~2 hours), Ramune sends **one** short
  "can't reach it" message, only between `FAIL_ALERT_FROM` and `FAIL_ALERT_TO` (1 PM to 11 PM by default).
  An outage that starts at night is reported when the window opens, if it's still going. No repeats and no
  "it's back" message; a new outage later gets a new message. If **Discord** is what's broken, Ramune can't
  message you, so the run goes red instead (GitHub emails you), same as before.

## Morning schedule links

Each show in the morning schedule gets two links:
- **Crunchyroll:** taken from AniList's streaming links when the show has one. It goes to the series page;
  AniList doesn't have per-episode Crunchyroll links.
- **Anikoto:** a link straight to that day's episode (`/watch/overgeared-a487c/ep-9`). Anikoto's show
  pages end in a random code, so the first time a show appears Ramune searches Anikoto for it and only
  accepts a result whose English or Japanese title matches the show's MAL/AniList titles exactly
  ("Season 2", "2nd Season", "II" and "Part 2" count as the same; season 1 never matches season 2).
  The code is saved in `state.json`, so each show is searched once, ever. No exact match, or Anikoto
  unreachable from GitHub: you get a search link for the title instead, and a missing show is retried a day
  later. Set `ANIKOTO_DIRECT=false` to always use search links. When Anikoto moves domains, change
  `ANIKOTO_URL`; saved codes keep working on the new domain.

## Plan to Watch alerts

Ramune also reads your MAL **Plan to Watch** list and sends one message when a show actually comes out:
- **Weekly shows** (AniList has an airing schedule): when episode 1 airs.
- **Released all at once** (movies, Netflix-style batches, which AniList gives no schedule): when AniList
  marks it Releasing or Finished and its start date has arrived.

Each show is alerted once. Anything already out the first time Ramune sees it (including everything on your
list when this feature first runs) is recorded silently. Weak spots: AniList status for unscheduled
releases is updated by volunteers, so a batch alert can be hours or a day late; a movie counts as "out" when
it opens in Japanese cinemas, not when it streams; and a show AniList lists as Releasing with no schedule
(common for some web shows) is treated as released all at once.
- State is committed whenever something changes, plus at least once a week, so GitHub never disables the
  schedule for inactivity (it does that after 60 days without activity on public repos).

## Shows that won't alert (by design)

Finished shows you're catching up on, shows with no announced schedule yet, and the rare MAL entry that
AniList doesn't have. They're listed in the test message and in each run's Summary table.

## Public or private?

**Public is recommended.** Standard GitHub Actions runners are free and unlimited for public repos.
Your secrets stay encrypted and are masked in logs; the only things visible are the code and
`state.json` (show IDs and titles, which are already public on your MAL profile).

A private repo on the free plan gets about 2,000 Actions minutes a month, and every run is billed as at
least one minute. Every 15 minutes is ~2,900 runs/month, which is over that. If you go private,
change the cron line in `.github/workflows/ramune.yml` to `"*/30 * * * *"`.

## Notes

- **Timing:** AniList air times are the Japanese broadcast time. Crunchyroll and others often post 0–60 min
  later. If alerts arrive before the episode is on your streaming site, set `ANNOUNCE_DELAY_MINUTES`
  (e.g. `30`). GitHub's 15-minute schedule also isn't exact; expect alerts within roughly 5–20 minutes
  of air time.
- **Changing your list:** just update MAL. The next run picks it up.
- **Webhook avatar:** set it in Discord's webhook settings (we'll make a Ramune logo in Stage 2).

## Troubleshooting

| Run shows | Fix |
|---|---|
| `Missing secret MAL_CLIENT_ID` / `variable MAL_USERNAME` | Names must match exactly (Secrets vs Variables tab matters). |
| `MyAnimeList rejected the Client ID (HTTP 401)` | Re-copy the Client ID into the secret. |
| `MyAnimeList user ... not found` | Check `MAL_USERNAME`. |
| `HTTP 403` from MAL | Your list must be public. |
| `Discord rejected the webhook` | Webhook was deleted or regenerated. Update `DISCORD_WEBHOOK_URL`. |
| "Save state" step fails with 403 | *Settings → Actions → General → Workflow permissions* → **Read and write permissions** → Save. |
| Schedule stopped running | Actions tab → Ramune → **Enable workflow**. |
