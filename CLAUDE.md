# Ramune

Anime episode notifier. It reads the owner's MyAnimeList "Watching" list, looks up each show's airing schedule on AniList, and sends Discord messages (a message per new episode, optional morning schedule, a quiet alert if MAL or AniList is unreachable). Python standard library only. It runs on GitHub Actions every 15 minutes. There is no Windows app, no tray, no release zip and no Minibar entry.

## Run and test
- `python3 -m unittest discover -s tests` runs the offline suite (fake MAL, AniList and Discord). Add a test for every fix.
- `.github/workflows/ramune.yml` runs `ramune.py` on a 15 minute schedule and from the manual "Run workflow" button (modes `normal`, `test`, `summary`). The README lists the options and what each does.

## Gotchas
- `state/state.json` is written by the workflow and committed back to the repo. Never edit it by hand and never reset it: that would re-announce old episodes. Because the workflow commits often, `git pull --rebase` before you push.
- Secrets (`MAL_CLIENT_ID`, `DISCORD_WEBHOOK_URL`) live only in GitHub Actions secrets. Settings like `MAL_USERNAME`, `DAILY_SUMMARY`, `EPISODE_ALERTS` live in Actions variables (README table). Never put any of them in code, tests, README text, logs or commits.
- Keep it standard library only, so the workflow needs no pip install.
- Don't change the workflow's schedule, permissions or secrets unless the owner asks.

## Working rules
- This repo is public: never commit secrets, webhook URLs, tokens, personal data or cache files.
- Work on a branch. Don't push or open a PR until the owner says so. "Push", "release" and "update GitHub" count as that go-ahead: then push to `main`. Never force-push.
- There are no releases here. Pushing to `main` is the whole delivery.
- If unsure a fix is safe, report it instead of changing it.
- Keep README text in plain words and mention what's new.
- Explain changes simply, in plain language. Finish with what changed, the tests run, and anything that can only be checked on the live schedule (a real run on GitHub, real Discord messages).
