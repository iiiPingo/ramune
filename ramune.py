#!/usr/bin/env python3
"""
Ramune: Discord alerts when a new episode of an anime on your MyAnimeList
"Watching" list airs. Built to run on GitHub Actions every 15 minutes.

Flow:  MAL (watching list)  ->  AniList (idMal -> airing schedule)
       ->  find episodes that aired since we last looked  ->  Discord webhook
       ->  remember what was announced in state/state.json (committed back).

Standard library only, so the workflow needs no pip install.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state", "state.json")

MAL_API = "https://api.myanimelist.net/v2"
ANILIST_API = "https://graphql.anilist.co"
USER_AGENT = "Ramune/1.0 (anime episode notifier; GitHub Actions)"

BRAND_COLOR = 0x5EC8E5          # ramune soda blue
NEW_SHOW_GRACE = 30 * 60        # a show added between runs still gets an episode that aired in the last 30 min
ANNOUNCED_KEEP = 30 * 86400     # forget announced episodes after 30 days
HEARTBEAT_EVERY = 7 * 86400     # touch state weekly so the repo never looks inactive
FAIL_ALERT_AFTER = 8            # consecutive upstream failures (~2h) before the run goes red


# --------------------------------------------------------------------------- utils

class ConfigError(Exception):
    """Something the user has to fix (secret, username, webhook). Fails the run."""


class UpstreamError(Exception):
    """MAL / AniList / Discord temporarily unavailable. Retried next run."""


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(f"::warning::{msg}", flush=True)


def env(name, default=""):
    value = os.environ.get(name, "")
    return value.strip() if value and value.strip() else default


def env_int(name, default, lo, hi):
    raw = env(name)
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        warn(f"{name}={raw!r} is not a number, using {default}")
        return default


def env_bool(name, default=False):
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def load_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        if name == "Asia/Dubai":  # fixed UTC+4, no DST
            return timezone(timedelta(hours=4), "Dubai")
        warn(f"Unknown TIMEZONE {name!r}, falling back to UTC")
        return timezone.utc


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fmt_clock(dt):
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def fmt_local(ts, tz):
    dt = datetime.fromtimestamp(ts, tz)
    return f"{dt.strftime('%a %d %b')}, {fmt_clock(dt)}"


def fmt_span(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- http

def _retry_after(err, payload):
    header = err.headers.get("Retry-After") if err.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    if isinstance(payload, dict) and "retry_after" in payload:  # Discord
        try:
            return float(payload["retry_after"])
        except (TypeError, ValueError):
            pass
    return None


def http(method, url, headers=None, body=None, what="request", attempts=5):
    """Returns (status, parsed_json_or_None). Retries 429 / 5xx / network errors.
    Other 4xx are returned to the caller to interpret."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})

    backoff = 2.0
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = None
            if err.code == 429 or err.code >= 500:
                wait = _retry_after(err, payload) or backoff
                if attempt < attempts:
                    log(f"  {what}: HTTP {err.code}, retrying in {wait:.0f}s ({attempt}/{attempts})")
                    time.sleep(min(wait, 60))
                    backoff = min(backoff * 2, 60)
                    continue
                raise UpstreamError(f"{what} kept failing (HTTP {err.code})")
            return err.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            if attempt < attempts:
                log(f"  {what}: network error ({err}), retrying in {backoff:.0f}s ({attempt}/{attempts})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise UpstreamError(f"{what} unreachable: {err}")
    raise UpstreamError(f"{what} failed")


# --------------------------------------------------------------------------- config

class Config:
    def __init__(self):
        self.mal_client_id = env("MAL_CLIENT_ID")
        self.webhook = env("DISCORD_WEBHOOK_URL")
        self.username = env("MAL_USERNAME")
        missing = [label for label, value in (
            ("secret MAL_CLIENT_ID", self.mal_client_id),
            ("secret DISCORD_WEBHOOK_URL", self.webhook),
            ("variable MAL_USERNAME", self.username),
        ) if not value]
        if missing:
            raise ConfigError("Missing " + ", ".join(missing)
                              + ". Add it under Settings > Secrets and variables > Actions (see README).")
        if "/api/webhooks/" not in self.webhook:
            raise ConfigError("DISCORD_WEBHOOK_URL doesn't look like a Discord webhook URL "
                              "(it should contain /api/webhooks/).")

        self.tz_name = env("TIMEZONE", "Asia/Dubai")
        self.tz = load_tz(self.tz_name)
        self.tz_label = self.tz_name.split("/")[-1].replace("_", " ")
        self.episode_alerts = env_bool("EPISODE_ALERTS", True)
        self.daily_summary = env_bool("DAILY_SUMMARY", False)
        self.schedule_changes = env_bool("SCHEDULE_CHANGES", True)
        self.summary_hour = env_int("SUMMARY_HOUR", 9, 0, 23)
        self.delay = env_int("ANNOUNCE_DELAY_MINUTES", 0, 0, 24 * 60) * 60
        self.lookback = env_int("LOOKBACK_HOURS", 72, 6, 24 * 14) * 3600
        self.mode = env("MODE", "normal").lower()


# --------------------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
            if isinstance(state, dict):
                return state
    except FileNotFoundError:
        pass
    except ValueError:
        warn("state.json was unreadable, starting fresh (no old episodes will be announced)")
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    text = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            if fh.read() == text:
                return
    except FileNotFoundError:
        pass
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------- MAL

def fetch_mal_watching(cfg):
    query = urllib.parse.urlencode({
        "status": "watching",
        "limit": 1000,
        "nsfw": "true",
        "fields": "list_status,num_episodes,alternative_titles,status",
    })
    url = f"{MAL_API}/users/{urllib.parse.quote(cfg.username)}/animelist?{query}"
    shows = []
    while url:
        code, data = http("GET", url, {"X-MAL-CLIENT-ID": cfg.mal_client_id}, what="MyAnimeList")
        if code == 401:
            raise ConfigError("MyAnimeList rejected the Client ID (HTTP 401). Check the MAL_CLIENT_ID secret.")
        if code == 403:
            raise ConfigError(f"MyAnimeList refused access to {cfg.username}'s list (HTTP 403). "
                              "Is the list public, and is the Client ID correct?")
        if code == 404:
            raise ConfigError(f"MyAnimeList user {cfg.username!r} not found. Check the MAL_USERNAME variable.")
        if code != 200 or not isinstance(data, dict):
            raise UpstreamError(f"MyAnimeList returned HTTP {code}")
        for row in data.get("data", []):
            node = row.get("node") or {}
            if "id" not in node:
                continue
            alt = node.get("alternative_titles") or {}
            shows.append({
                "mal_id": node["id"],
                "title": node.get("title") or "",
                "title_en": alt.get("en") or "",
                "watched": (row.get("list_status") or {}).get("num_episodes_watched"),
            })
        url = (data.get("paging") or {}).get("next")
    return shows


# --------------------------------------------------------------------------- AniList

MEDIA_QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(idMal_in: $ids, type: ANIME) {
      id idMal status episodes format siteUrl
      title { romaji english }
      coverImage { large color }
      nextAiringEpisode { episode airingAt }
      externalLinks { site url type }
    }
  }
}
"""

SCHEDULE_QUERY = """
query ($ids: [Int], $from: Int, $to: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    airingSchedules(mediaId_in: $ids, airingAt_greater: $from, airingAt_lesser: $to, sort: TIME) {
      mediaId episode airingAt
    }
  }
}
"""


def anilist(query, variables):
    code, data = http("POST", ANILIST_API, body={"query": query, "variables": variables}, what="AniList")
    if code != 200 or not isinstance(data, dict) or data.get("errors"):
        errors = (data or {}).get("errors") if isinstance(data, dict) else None
        raise UpstreamError(f"AniList HTTP {code}: {errors}")
    return data["data"]


def anilist_paged(query, variables, key):
    out, page = [], 1
    while True:
        result = anilist(query, dict(variables, page=page))["Page"]
        out.extend(result.get(key) or [])
        if not (result.get("pageInfo") or {}).get("hasNextPage") or page >= 20:
            return out
        page += 1


def fetch_anilist_media(mal_ids):
    by_mal = {}
    for group in chunks(sorted(set(mal_ids)), 50):
        for m in anilist_paged(MEDIA_QUERY, {"ids": group}, "media"):
            if m.get("idMal"):
                by_mal.setdefault(m["idMal"], m)
    return by_mal


def fetch_schedules(media_ids, start, end):
    eps = []
    for group in chunks(sorted(set(media_ids)), 50):
        eps.extend(anilist_paged(SCHEDULE_QUERY, {"ids": group, "from": start - 1, "to": end + 1},
                                 "airingSchedules"))
    return sorted(eps, key=lambda e: (e["airingAt"], e["mediaId"], e["episode"]))


# --------------------------------------------------------------------------- shows

def build_show(mal, media):
    title = media.get("title") or {}
    cover = media.get("coverImage") or {}
    color = cover.get("color")
    try:
        color = int(color.lstrip("#"), 16) if color else BRAND_COLOR
    except ValueError:
        color = BRAND_COLOR
    streams = [l for l in (media.get("externalLinks") or [])
               if (l.get("type") == "STREAMING") and l.get("url") and l.get("site")]
    romaji = title.get("romaji") or mal.get("title") or ""
    return {
        "anilist_id": media["id"],
        "mal_id": mal["mal_id"],
        "title": title.get("english") or mal.get("title_en") or romaji,
        "romaji": romaji,
        "status": media.get("status") or "UNKNOWN",
        "episodes": media.get("episodes"),
        "cover": cover.get("large"),
        "color": color,
        "anilist_url": media.get("siteUrl") or f"https://anilist.co/anime/{media['id']}",
        "mal_url": f"https://myanimelist.net/anime/{mal['mal_id']}",
        "next": media.get("nextAiringEpisode"),
        "watched": mal.get("watched"),
        "streams": streams[:3],
    }


# --------------------------------------------------------------------------- discord

def discord_post(cfg, payload):
    payload = dict(payload, username="Ramune", allowed_mentions={"parse": []})
    code, data = http("POST", cfg.webhook, body=payload, what="Discord")
    if code in (200, 204):
        return
    if code in (401, 403, 404):
        raise ConfigError(f"Discord rejected the webhook (HTTP {code}). If you deleted or regenerated it, "
                          "update the DISCORD_WEBHOOK_URL secret.")
    if code == 400 and payload.get("embeds"):
        log(f"  Discord refused the embed ({data}); sending plain text instead")
        fallback = {"content": clip(payload.get("content") or "New episode", 2000)}
        return discord_post(cfg, fallback)
    raise UpstreamError(f"Discord HTTP {code}: {data}")


def episode_embed(cfg, show, ep, now, preview=False):
    n = ep["episode"]
    total = show["episodes"]
    final = bool(total) and n == total
    aired_at = ep["airingAt"]

    if preview:
        headline = f"**Episode {n}** airs {fmt_local(aired_at, cfg.tz)} ({cfg.tz_label})"
    else:
        headline = f"**Episode {n} is out!**" + ("  🎉 Final episode" if final else "")

    fields = []
    if show["romaji"] and show["romaji"] != show["title"]:
        fields.append({"name": "Romaji", "value": clip(show["romaji"], 1024), "inline": False})

    when = f"{fmt_local(aired_at, cfg.tz)} ({cfg.tz_label})"
    late = now - aired_at
    if not preview and late > 2 * 3600:
        when += f"\n{fmt_span(late)} ago"
    fields.append({"name": "Airs" if preview else "Aired", "value": when, "inline": True})
    fields.append({"name": "Episode", "value": f"{n} / {total}" if total else str(n), "inline": True})

    watched = show.get("watched")
    if isinstance(watched, int) and not preview:
        behind = n - watched
        if behind > 0:
            fields.append({"name": "Your progress",
                           "value": f"{watched} watched · {behind} to catch up", "inline": True})

    links = [f"[MyAnimeList]({show['mal_url']})", f"[AniList]({show['anilist_url']})"]
    links += [f"[{s['site']}]({s['url']})" for s in show["streams"]]
    fields.append({"name": "Links", "value": clip(" · ".join(links), 1024), "inline": False})

    embed = {
        "title": clip(show["title"], 256),
        "url": show["mal_url"],
        "description": headline,
        "color": show["color"],
        "fields": fields,
        "footer": {"text": "Ramune · preview" if preview else "Ramune"},
        "timestamp": iso(aired_at),
    }
    if show["cover"]:
        embed["thumbnail"] = {"url": show["cover"]}
    return embed


def announce(cfg, show, ep, now):
    total = show["episodes"]
    tail = " (final)" if total and ep["episode"] == total else ""
    content = f"🫧 **{show['title']}** · Episode {ep['episode']}{tail} is out"
    discord_post(cfg, {"content": clip(content, 2000), "embeds": [episode_embed(cfg, show, ep, now)]})


def send_summary(cfg, shows, schedule, now):
    local_now = datetime.fromtimestamp(now, cfg.tz)
    day_start = int(local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    day_end = int((datetime.fromtimestamp(day_start, cfg.tz) + timedelta(days=1)).timestamp())
    today = [e for e in schedule if day_start <= e["airingAt"] < day_end and e["mediaId"] in shows]

    if today:
        lines = []
        for e in today:
            show = shows[e["mediaId"]]
            mark = "✅" if e["airingAt"] + cfg.delay <= now else "🕒"
            clock = fmt_clock(datetime.fromtimestamp(e["airingAt"], cfg.tz))
            lines.append(f"{mark} `{clock:>8}`  [{clip(show['title'], 80)}]({show['mal_url']}) · Ep {e['episode']}")
        desc = "\n".join(lines)
    else:
        desc = "Nothing from your Watching list airs today."

    airing = sum(1 for s in shows.values() if s["next"])
    upcoming = sorted((s for s in shows.values() if s["next"]), key=lambda s: s["next"]["airingAt"])
    after_today = [s for s in upcoming if s["next"]["airingAt"] >= day_end]
    if not today and after_today:
        s = after_today[0]
        desc += (f"\nNext: **{clip(s['title'], 80)}** Ep {s['next']['episode']}, "
                 f"{fmt_local(s['next']['airingAt'], cfg.tz)}")

    embed = {
        "title": f"Today · {local_now.strftime('%a %d %b')}",
        "description": clip(desc, 4096),
        "color": BRAND_COLOR,
        "footer": {"text": f"Ramune · {airing} airing shows tracked · times in {cfg.tz_label}"},
    }
    content = f"🫧 Today's schedule: {len(today)} episode{'s' if len(today) != 1 else ''}"
    discord_post(cfg, {"content": content, "embeds": [embed]})
    return today


def check_schedule_changes(cfg, digest, shows, schedule, now):
    """After the morning digest: one message if any of today's episodes moved or was pulled."""
    eps = digest["eps"]
    current = {f"{e['mediaId']}:{e['episode']}": e["airingAt"] for e in schedule}
    lines, updates = [], {}
    for key, was in sorted(eps.items(), key=lambda kv: kv[1]):
        mid, ep = (int(x) for x in key.split(":"))
        show = shows.get(mid)
        if not show:  # you dropped it from your list, not a schedule change
            updates[key] = None
            continue
        if was < now - 2 * 3600:  # aired a while ago
            continue
        title = f"**{clip(show['title'], 80)}** Ep {ep}"
        old = fmt_clock(datetime.fromtimestamp(was, cfg.tz))
        if key in current:
            new = current[key]
            if abs(new - was) < 5 * 60:
                continue
            new_dt = datetime.fromtimestamp(new, cfg.tz)
            if new_dt.strftime("%Y-%m-%d") == digest["date"]:
                lines.append(f"⏰ {title} moved: {old} → {fmt_clock(new_dt)}")
                updates[key] = new
            else:
                lines.append(f"📅 {title} moved to {fmt_local(new, cfg.tz)} (was {old})")
                updates[key] = None
            continue
        nxt = show["next"]
        if nxt and nxt["episode"] == ep:
            lines.append(f"📅 {title} postponed to {fmt_local(nxt['airingAt'], cfg.tz)} (was {old})")
        elif was > now:
            lines.append(f"❌ {title} pulled from today's schedule (was {old}, no new date yet)")
        else:
            continue
        updates[key] = None

    if lines:
        log("Schedule changed: " + " | ".join(lines))
        embed = {
            "title": f"Schedule change · {datetime.fromtimestamp(now, cfg.tz).strftime('%a %d %b')}",
            "description": clip("\n".join(lines), 4096),
            "color": BRAND_COLOR,
            "footer": {"text": f"Ramune · times in {cfg.tz_label}"},
        }
        discord_post(cfg, {"content": "🫧 Schedule change in today's lineup", "embeds": [embed]})
    for key, value in updates.items():
        if value is None:
            eps.pop(key, None)
        else:
            eps[key] = value


def send_test(cfg, watching, shows, unmapped, now):
    airing = sorted((s for s in shows.values() if s["next"]), key=lambda s: s["next"]["airingAt"])
    quiet = len(shows) - len(airing)
    lines = [
        f"Reading **{cfg.username}**'s Watching list: **{len(watching)}** shows.",
        f"📡 {len(airing)} airing with a schedule",
        f"💤 {quiet} finished or no schedule yet",
    ]
    if unmapped:
        lines.append(f"❓ {len(unmapped)} not found on AniList: " + clip(", ".join(unmapped), 300))
    if airing:
        s = airing[0]
        lines.append(f"\nNext up: **{clip(s['title'], 100)}** Ep {s['next']['episode']}, "
                     f"{fmt_local(s['next']['airingAt'], cfg.tz)} (in {fmt_span(s['next']['airingAt'] - now)})")
    status = {
        "title": "Ramune is connected",
        "description": clip("\n".join(lines), 4096),
        "color": BRAND_COLOR,
        "footer": {"text": "Ramune · test message"},
    }
    embeds = [status]
    if airing:
        embeds.append(episode_embed(cfg, airing[0], airing[0]["next"], now, preview=True))
    discord_post(cfg, {"content": "🫧 Ramune test: MAL, AniList and Discord are all working.",
                       "embeds": embeds})


# --------------------------------------------------------------------------- job summary

def write_job_summary(cfg, shows, unmapped, announced_now, now):
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = sorted(shows.values(), key=lambda s: (s["next"] or {}).get("airingAt", 10**12))
    out = ["## 🫧 Ramune", ""]
    if announced_now:
        out.append("**Announced this run:** " + ", ".join(announced_now))
        out.append("")
    out += [f"| Show | Status | Next | When ({cfg.tz_label}) |", "|---|---|---|---|"]
    for s in rows:
        nxt = s["next"]
        when = f"{fmt_local(nxt['airingAt'], cfg.tz)} (in {fmt_span(nxt['airingAt'] - now)})" if nxt else "-"
        title = s["title"].replace("|", "/")
        out.append(f"| [{title}]({s['mal_url']}) | {s['status'].title().replace('_', ' ')} | "
                   f"{'Ep ' + str(nxt['episode']) if nxt else '-'} | {when} |")
    for t in unmapped:
        out.append(f"| {t.replace('|', '/')} | Not on AniList | - | - |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


# --------------------------------------------------------------------------- main logic

def process(cfg, state, now):
    first_run = not state.get("initialized_at")

    # 1. MAL watching list (fall back to the last known list if MAL is down)
    try:
        watching = fetch_mal_watching(cfg)
        state["watching_cache"] = {str(w["mal_id"]): w["title"] for w in watching}
    except UpstreamError as err:
        cache = state.get("watching_cache") or {}
        if not cache:
            raise
        warn(f"{err}. Using the last known Watching list ({len(cache)} shows).")
        watching = [{"mal_id": int(k), "title": v, "title_en": "", "watched": None} for k, v in cache.items()]
    log(f"MAL: {len(watching)} shows on {cfg.username}'s Watching list")

    # 2. Map to AniList
    media_by_mal = fetch_anilist_media([w["mal_id"] for w in watching]) if watching else {}
    shows, unmapped = {}, []
    for w in watching:
        media = media_by_mal.get(w["mal_id"])
        if media:
            shows[media["id"]] = build_show(w, media)
        else:
            unmapped.append(w["title"] or f"MAL #{w['mal_id']}")
    if unmapped:
        log(f"Not on AniList (skipped): {', '.join(unmapped)}")

    # 3. Track baselines: only episodes airing after a show was first seen are announced
    tracked = state.setdefault("shows", {})
    for aid, show in shows.items():
        key = str(aid)
        if key not in tracked:
            since = now if first_run else now - NEW_SHOW_GRACE
            tracked[key] = {"mal_id": show["mal_id"], "title": show["title"], "since": since}
            if not first_run:
                log(f"Now tracking: {show['title']}")
        elif tracked[key].get("title") != show["title"]:
            tracked[key]["title"] = show["title"]
    for key in [k for k in tracked if int(k) not in shows]:
        log(f"No longer watching: {tracked[key].get('title')}")
        del tracked[key]

    for s in sorted(shows.values(), key=lambda s: s["title"].lower()):
        nxt = s["next"]
        extra = f"next Ep {nxt['episode']} {fmt_local(nxt['airingAt'], cfg.tz)}" if nxt else "no upcoming episode"
        log(f"  · {s['title']} [{s['status']}] {extra}")

    # 4. Airing schedule: lookback window (catch-up after delayed runs) + rest of today (summary)
    local_now = datetime.fromtimestamp(now, cfg.tz)
    day_start = int(local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    day_end = int((datetime.fromtimestamp(day_start, cfg.tz) + timedelta(days=1)).timestamp())
    schedule = fetch_schedules(list(shows), min(now - cfg.lookback, day_start), max(now, day_end)) if shows else []

    # 5. Announce new episodes
    announced = state.setdefault("announced", {})
    announced_now = []
    if first_run:
        state["initialized_at"] = now
        log("First run: tracking from now on, not announcing anything that already aired.")
    else:
        for ep in schedule:
            show = shows.get(ep["mediaId"])
            key = f"{ep['mediaId']}:{ep['episode']}"
            if not show or key in announced:
                continue
            if ep["airingAt"] + cfg.delay > now:
                continue
            if ep["airingAt"] <= tracked[str(ep["mediaId"])]["since"]:
                continue
            if cfg.episode_alerts:
                log(f"Announcing: {show['title']} Ep {ep['episode']}")
                announce(cfg, show, ep, now)
                time.sleep(1)
            else:
                log(f"Aired (episode alerts off): {show['title']} Ep {ep['episode']}")
            announced[key] = ep["airingAt"]
            announced_now.append(f"{show['title']} Ep {ep['episode']}")
    if not announced_now and not first_run:
        log("No new episodes.")

    # 6. Daily summary / manual modes
    today_str = local_now.strftime("%Y-%m-%d")
    if cfg.mode == "summary" or (
        cfg.daily_summary and local_now.hour >= cfg.summary_hour and state.get("last_summary") != today_str
    ):
        log("Sending today's schedule")
        today = send_summary(cfg, shows, schedule, now)
        state["last_summary"] = today_str
        state["digest"] = {"date": today_str, "eps": {f"{e['mediaId']}:{e['episode']}": e["airingAt"] for e in today}}
    elif cfg.schedule_changes and (state.get("digest") or {}).get("date") == today_str:
        check_schedule_changes(cfg, state["digest"], shows, schedule, now)
    if cfg.mode == "test":
        log("Sending test message")
        send_test(cfg, watching, shows, unmapped, now)

    # 7. Housekeeping
    for key in [k for k, ts in announced.items() if ts < now - ANNOUNCED_KEEP]:
        del announced[key]
    if now - state.get("heartbeat_at", 0) > HEARTBEAT_EVERY:
        state["heartbeat_at"] = now

    write_job_summary(cfg, shows, unmapped, announced_now, now)


def main():
    try:
        cfg = Config()
    except ConfigError as err:
        print(f"::error::{err}", flush=True)
        return 1

    state = load_state()
    now = int(time.time())
    try:
        process(cfg, state, now)
        state.pop("failures", None)
        save_state(state)
        return 0
    except ConfigError as err:
        save_state(state)
        print(f"::error::{err}", flush=True)
        return 1
    except UpstreamError as err:
        failures = state.get("failures", 0) + 1
        state["failures"] = failures
        save_state(state)
        warn(f"{err} (failure {failures} in a row; the next run will catch up on anything missed)")
        if failures == FAIL_ALERT_AFTER:
            try:
                discord_post(cfg, {"content": "⚠️ Ramune has failed to check for episodes for about 2 hours "
                                              f"in a row. Last error: {clip(str(err), 300)}"})
            except Exception:
                pass
        if failures == FAIL_ALERT_AFTER or (failures > FAIL_ALERT_AFTER and failures % 48 == 0):
            return 1  # red run -> GitHub emails you
        return 0


if __name__ == "__main__":
    sys.exit(main())
