#!/usr/bin/env python3
"""
Ramune: Discord alerts when a new episode of an anime on your MyAnimeList
"Watching" list airs. Built to run on GitHub Actions every 15 minutes.

Flow:  MAL (watching list)  ->  AniList (idMal -> airing schedule)
       ->  find episodes that aired since we last looked  ->  Discord webhook
       ->  remember what was announced in state/state.json (committed back).

Also: Plan to Watch premieres / full releases, Crunchyroll + Anikoto links in
the morning schedule, and one quiet "can't reach MAL/AniList" alert.

Standard library only, so the workflow needs no pip install.
"""

import hashlib
import html
import re
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state", "state.json")

MAL_API = "https://api.myanimelist.net/v2"
ANILIST_API = "https://graphql.anilist.co"
USER_AGENT = "Ramune/1.1 (anime episode notifier; GitHub Actions)"

BRAND_COLOR = 0x5EC8E5          # ramune soda blue
NEW_SHOW_GRACE = 30 * 60        # a show added between runs still gets an episode that aired in the last 30 min
ANNOUNCED_KEEP = 30 * 86400     # forget announced episodes after 30 days
HEARTBEAT_EVERY = 7 * 86400     # touch state weekly so the repo never looks inactive
FAIL_ALERT_AFTER = 8            # consecutive upstream failures (~2h) before the run goes red
ANIKOTO_DEFAULT = "https://anikototv.bz"   # search lives at /filter?keyword=
RELEASED = ("RELEASING", "FINISHED")
ANIKOTO_DOWN_PAUSE = 3600       # after Anikoto fails to load, skip lookups for an hour
ANIKOTO_MISS_RETRY = 24 * 3600  # a show Anikoto didn't have is searched again after a day


# --------------------------------------------------------------------------- utils

class ConfigError(Exception):
    """Something the user has to fix (secret, username, webhook). Fails the run."""


class UpstreamError(Exception):
    """MAL / AniList / Discord temporarily unavailable. Retried next run.
    `service` is "MyAnimeList", "AniList" or "Discord" (the http() `what`)."""

    def __init__(self, msg, service=None):
        super().__init__(msg)
        self.service = service


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
                raise UpstreamError(f"{what} kept failing (HTTP {err.code})", what)
            return err.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            if attempt < attempts:
                log(f"  {what}: network error ({err}), retrying in {backoff:.0f}s ({attempt}/{attempts})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise UpstreamError(f"{what} unreachable: {err}", what)
    raise UpstreamError(f"{what} failed", what)


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
        self.up_next = env_bool("UP_NEXT", True)
        self.summary_hour = env_int("SUMMARY_HOUR", 9, 0, 23)
        self.delay = env_int("ANNOUNCE_DELAY_MINUTES", 0, 0, 24 * 60) * 60
        self.lookback = env_int("LOOKBACK_HOURS", 72, 6, 24 * 14) * 3600
        self.mode = env("MODE", "normal").lower()
        # 1) one-time "can't reach MAL / AniList" alert, only inside this local-hour window
        self.fail_alert = env_bool("FAIL_ALERT", True)
        self.fail_from = env_int("FAIL_ALERT_FROM", 13, 0, 23)
        self.fail_to = env_int("FAIL_ALERT_TO", 23, 0, 24)
        # 2) Crunchyroll + Anikoto links in the morning schedule
        self.summary_links = env_bool("SUMMARY_LINKS", True)
        self.anikoto = env("ANIKOTO_URL", ANIKOTO_DEFAULT)
        self.anikoto_direct = env_bool("ANIKOTO_DIRECT", True)   # exact episode links when found
        self.up_next_anikoto = env_bool("UP_NEXT_ANIKOTO", True)  # pinned list links to Anikoto, not MAL
        # 3) Plan to Watch: alert on premiere / full release
        self.ptw_alerts = env_bool("PTW_ALERTS", True)


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
    return fetch_mal_list(cfg, "watching")


def fetch_mal_list(cfg, status):
    query = urllib.parse.urlencode({
        "status": status,
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
            raise UpstreamError(f"MyAnimeList returned HTTP {code}", "MyAnimeList")
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
      startDate { year month day }
      title { romaji english }
      synonyms
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
        raise UpstreamError(f"AniList HTTP {code}: {errors}", "AniList")
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
    crunchyroll = next((l["url"] for l in streams if l["site"].strip().lower() == "crunchyroll"), None)
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
        "mal_title": mal.get("title"),
        "streams": streams[:3],
        "crunchyroll": crunchyroll,
        "format": media.get("format"),
        "start": media.get("startDate") or {},
        "names": [n for n in [title.get("english"), title.get("romaji"), mal.get("title"), mal.get("title_en")]
                  + list(media.get("synonyms") or []) if n],
    }


# --------------------------------------------------------------------------- watch links

def md_url(url):
    """Keep a URL from breaking Discord's [text](url) markdown."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def anikoto_parts(cfg):
    """(origin, search_template) from ANIKOTO_URL, or None if Anikoto is off.
    ANIKOTO_URL can be a domain (search at /filter?keyword=) or a full search URL with {q}."""
    base = (cfg.anikoto or "").strip()
    if not base or base.lower() in ("off", "false", "none", "0"):
        return None
    if not base.lower().startswith(("http://", "https://")):
        base = "https://" + base
    parsed = urllib.parse.urlsplit(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    template = base if "{q}" in base else f"{base.rstrip('/')}/filter?keyword={{q}}"
    return origin, template


def anikoto_url(cfg, title):
    """Search link for a title (the fallback when there's no exact match)."""
    parts = anikoto_parts(cfg)
    if not parts:
        return None
    return parts[1].replace("{q}", urllib.parse.quote_plus(title or ""))


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}


def norm_title(text):
    """Loose title key: case, punctuation, "Season 2" / "2nd Season" / "II" / "Part 2" all compare equal,
    but "Blue Box" and "Blue Box Season 2" stay different."""
    t = html.unescape(text or "").lower().replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", t)
    out = []
    for w in words:
        w = _ROMAN.get(w, w)
        w = re.sub(r"^(\d+)(st|nd|rd|th)$", r"\1", w)
        if w in ("season", "part", "cour", "the", "a"):
            continue
        out.append(w)
    return "".join(out)


class _AnikotoResults(HTMLParser):
    """Collects search results: <a class="name d-title" href=".../watch/<slug>/ep-1" data-jp="Romaji">English</a>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self._cur = [], None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if "d-title" in classes and "/watch/" in (a.get("href") or ""):
            self._cur = {"href": a["href"], "jp": a.get("data-jp") or "", "name": ""}

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["name"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._cur is not None:
            self.items.append(self._cur)
            self._cur = None


def anikoto_slug_from_href(href):
    m = re.search(r"/watch/([^/?#]+)", href or "")
    return m.group(1) if m else None


def fetch_text(url, what):
    """Best-effort GET of an HTML page. Never raises: a failure just means no direct link."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as err:
            if attempt:
                log(f"  {what}: {err}")
            else:
                time.sleep(2)
    return None


def anikoto_lookup(cfg, state, show, now):
    """Find the show's Anikoto slug (cached in state forever; misses retried after a day)."""
    if not cfg.anikoto_direct:
        return None
    parts = anikoto_parts(cfg)
    if not parts:
        return None
    cache = state.setdefault("anikoto", {})
    key = str(show["anilist_id"])
    hit = cache.get(key)
    if isinstance(hit, str):
        return hit
    if isinstance(hit, int) and now - hit < ANIKOTO_MISS_RETRY:
        return None
    if now - state.get("anikoto_down_at", 0) < ANIKOTO_DOWN_PAUSE:
        return None  # Anikoto was unreachable recently: don't slow every run down retrying
    wanted = {norm_title(n) for n in show.get("names") or [show["title"]]} - {""}
    queries = []
    for q in (show.get("mal_title"), show["title"], show.get("romaji")):
        if q and q not in queries:
            queries.append(q)
    for q in queries[:2]:
        page = fetch_text(parts[1].replace("{q}", urllib.parse.quote_plus(q)), "Anikoto")
        if page is None:
            state["anikoto_down_at"] = now  # unreachable: pause lookups for an hour, no miss recorded
            return None
        state.pop("anikoto_down_at", None)
        parser = _AnikotoResults()
        try:
            parser.feed(page)
        except Exception:
            continue
        for item in parser.items:
            if norm_title(item["name"]) in wanted or norm_title(item["jp"]) in wanted:
                slug = anikoto_slug_from_href(item["href"])
                if slug:
                    cache[key] = slug
                    log(f"Anikoto: {show['title']} -> {slug}")
                    return slug
    cache[key] = now
    log(f"Anikoto: no exact match for {show['title']} (using a search link)")
    return None


def anikoto_episode_url(cfg, slug, episode):
    parts = anikoto_parts(cfg)
    return f"{parts[0]}/watch/{slug}/ep-{int(episode)}" if parts and slug else None


def watch_links(cfg, show, episode=None, state=None, now=None):
    """Crunchyroll (from AniList, series page) and Anikoto, as markdown. Anikoto links straight to the
    episode when the show was matched on Anikoto, otherwise to a search for its title."""
    links = []
    if show.get("crunchyroll"):
        links.append(f"[Crunchyroll]({md_url(show['crunchyroll'])})")
    ak = None
    if episode is not None and state is not None:
        slug = anikoto_lookup(cfg, state, show, now or int(time.time()))
        ak = anikoto_episode_url(cfg, slug, episode)
        if ak:
            links.append(f"[Anikoto Ep {int(episode)}]({md_url(ak)})")
    if not ak:
        ak = anikoto_url(cfg, show["title"])
        if ak:
            links.append(f"[Anikoto]({md_url(ak)})")
    return links


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


def send_summary(cfg, shows, schedule, now, state=None):
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
            line = f"{mark} `{clock:>8}`  [{clip(show['title'], 80)}]({show['mal_url']}) · Ep {e['episode']}"
            if cfg.summary_links:
                links = watch_links(cfg, show, e["episode"], state, now)
                if links:
                    line += "\n\u2003\u2003" + " · ".join(links)
            lines.append(line)
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


def anikoto_show_url(cfg, state, show, now):
    """The show's page on Anikoto (no episode), a title search if it isn't matched, or MAL if Anikoto is off."""
    parts = anikoto_parts(cfg)
    slug = anikoto_lookup(cfg, state, show, now) if state is not None else None
    if parts and slug:
        return f"{parts[0]}/watch/{slug}"
    return anikoto_url(cfg, show["title"]) or show["mal_url"]


def up_next_text(cfg, shows, state=None, now=None):
    """Phone-friendly list: each show is its own bold, tappable title line (linking to Anikoto),
    with the time and episode underneath and a blank line between shows."""
    now = now or int(time.time())
    upcoming = sorted((s for s in shows.values() if s["next"]), key=lambda s: s["next"]["airingAt"])
    waiting = sorted((s for s in shows.values()
                      if not s["next"] and s["status"] in ("RELEASING", "NOT_YET_RELEASED", "HIATUS")),
                     key=lambda s: s["title"].lower())

    def link(s):
        url = anikoto_show_url(cfg, state, s, now) if cfg.up_next_anikoto else s["mal_url"]
        return f"**[{clip(s['title'], 80)}]({md_url(url)})**"

    blocks = []
    for s in upcoming:
        dt = datetime.fromtimestamp(s["next"]["airingAt"], cfg.tz)
        blocks.append(f"{link(s)}\n`{dt.strftime('%a %d %b')} · {fmt_clock(dt)}` · Ep {s['next']['episode']}")
    if not blocks:
        blocks.append("Nothing from your Watching list is scheduled right now.")
    if waiting:
        blocks.append("**No date yet**")
        blocks += [link(s) for s in waiting]

    out, used = [], 0
    for i, b in enumerate(blocks):          # never cut a link in half at Discord's 4096 limit
        if used + len(b) + 2 > 4000:
            out.append(f"…and {len(blocks) - i} more")
            break
        out.append(b)
        used += len(b) + 2
    return "\n\n".join(out)


def update_up_next(cfg, state, shows, now):
    """One pinned message, edited in place whenever the list changes."""
    desc = clip(up_next_text(cfg, shows, state, now), 4096)
    digest = hashlib.sha1(desc.encode("utf-8")).hexdigest()[:12]
    info = state.get("up_next") or {}
    if info.get("id") and info.get("hash") == digest:
        return
    embed = {
        "title": "Up next",
        "description": desc,
        "color": BRAND_COLOR,
        "footer": {"text": f"Ramune · times in {cfg.tz_label} · list last changed"},
        "timestamp": iso(now),
    }
    base = cfg.webhook.split("?")[0].rstrip("/")
    if info.get("id"):
        code, data = http("PATCH", f"{base}/messages/{info['id']}",
                          body={"embeds": [embed], "allowed_mentions": {"parse": []}}, what="Discord")
        if code in (200, 204):
            state["up_next"] = {"id": info["id"], "hash": digest}
            log("Up next: message updated")
            return
        if code == 404 and isinstance(data, dict) and data.get("code") == 10008:
            log("Up next: message was deleted, posting a new one")
        elif code in (401, 403, 404):
            raise ConfigError(f"Discord rejected the webhook (HTTP {code}). If you deleted or regenerated it, "
                              "update the DISCORD_WEBHOOK_URL secret.")
        else:
            raise UpstreamError(f"Discord HTTP {code}: {data}")
    code, data = http("POST", f"{base}?wait=true", what="Discord", body={
        "username": "Ramune",
        "content": "📌 Pin this one. Ramune keeps it updated.",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
        "flags": 4096,  # silent: no push notification
    })
    if code in (401, 403, 404):
        raise ConfigError(f"Discord rejected the webhook (HTTP {code}). If you deleted or regenerated it, "
                          "update the DISCORD_WEBHOOK_URL secret.")
    if code != 200 or not isinstance(data, dict) or "id" not in data:
        raise UpstreamError(f"Discord HTTP {code}: {data}")
    state["up_next"] = {"id": str(data["id"]), "hash": digest}
    log("Up next: posted a new message (pin it in Discord)")


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


# --------------------------------------------------------------------------- outage alert

def outage_failed(state, service, now):
    out = state.setdefault("outages", {})
    entry = out.setdefault(service, {"count": 0, "since": now, "alerted": False})
    entry["count"] += 1


def outage_ok(state, service):
    """Recovered: forget it silently (no "back up" message)."""
    out = state.get("outages") or {}
    if out.pop(service, None) is not None:
        log(f"{service} is reachable again")
    if not out:
        state.pop("outages", None)


def in_window(hour, start, end):
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window wraps past midnight


def maybe_outage_alert(cfg, state, now):
    """One short Discord message per outage, only inside FAIL_ALERT_FROM..FAIL_ALERT_TO.
    An outage that started at night is reported when the window opens, if it's still going."""
    if not cfg.fail_alert:
        return
    out = state.get("outages") or {}
    due = sorted(name for name, e in out.items()
                 if e.get("count", 0) >= FAIL_ALERT_AFTER and not e.get("alerted"))
    if not due:
        return
    hour = datetime.fromtimestamp(now, cfg.tz).hour
    if not in_window(hour, cfg.fail_from, cfg.fail_to):
        log(f"Outage alert for {', '.join(due)} waits for {cfg.fail_from}:00 ({cfg.tz_label})")
        return
    since = min(out[n].get("since", now) for n in due)
    names = " and ".join(f"**{n}**" for n in due)
    text = (f"⚠️ Ramune can't reach {names} (since {fmt_local(since, cfg.tz)}). "
            "It keeps retrying and catches up by itself.")
    try:
        discord_post(cfg, {"content": text})
    except Exception as err:  # Discord itself is down: try again next run
        log(f"Couldn't send the outage alert: {err}")
        return
    for n in due:
        out[n]["alerted"] = True
    log(f"Sent outage alert: {', '.join(due)}")


# --------------------------------------------------------------------------- plan to watch

def start_reached(show, now):
    """AniList startDate on or before today (UTC). Unknown parts count as reached."""
    st = show.get("start") or {}
    if not st.get("year"):
        return True
    today = datetime.fromtimestamp(now, timezone.utc).date()
    try:
        start = datetime(st["year"], st.get("month") or 1, st.get("day") or 1).date()
    except ValueError:
        return True
    return start <= today


def ptw_release(show, entry, ep1_at, now, delay):
    """Decide whether a Plan to Watch show is out. Returns (kind, when) or None.

    Weekly show = AniList has (or once had) an airing schedule for it.
      Out when episode 1's scheduled time has passed, or AniList marks it
      RELEASING/FINISHED and the next scheduled episode is past 1 (or there is none).
    All-at-once = AniList never showed a schedule (movies, Netflix-style drops).
      Out when the status turns RELEASING/FINISHED and the start date has come.
    """
    nxt = show.get("next")
    status = show.get("status")
    scheduled = bool(entry.get("sched") or nxt or ep1_at is not None)
    fmt = show.get("format")
    if scheduled:
        if ep1_at is not None:            # exact time known: trust it (and the delay)
            out = ep1_at + delay <= now
        else:
            out = status in RELEASED and (not nxt or nxt.get("episode", 1) > 1)
        if not out:
            return None
        if fmt == "MOVIE":
            kind = "movie"
        elif status == "FINISHED" and not nxt:
            kind = "batch"
        else:
            kind = "premiere"
        return kind, ep1_at
    if status in RELEASED and start_reached(show, now):
        return ("movie" if fmt == "MOVIE" else "batch"), None
    return None


FORMAT_NAMES = {"TV": "TV", "TV_SHORT": "TV short", "MOVIE": "Movie", "SPECIAL": "Special",
                "OVA": "OVA", "ONA": "ONA", "MUSIC": "Music"}


def ptw_embed(cfg, show, kind, when, now, state=None):
    total = show.get("episodes")
    if kind == "movie":
        headline = "**The movie is out!**"
    elif kind == "batch":
        headline = f"**All {total} episodes are out!**" if total and total > 1 else "**It's out!**"
    else:
        headline = "**Episode 1 is out!**"
    headline += "\nFrom your Plan to Watch list"

    fields = []
    if show["romaji"] and show["romaji"] != show["title"]:
        fields.append({"name": "Romaji", "value": clip(show["romaji"], 1024), "inline": False})
    fmt = FORMAT_NAMES.get(show.get("format") or "", (show.get("format") or "Anime").title())
    if total and kind != "movie":
        fmt += f" · {total} episode{'s' if total != 1 else ''}"
    fields.append({"name": "Format", "value": fmt, "inline": True})
    if when:
        fields.append({"name": "Aired", "value": f"{fmt_local(when, cfg.tz)} ({cfg.tz_label})", "inline": True})
    links = watch_links(cfg, show, 1, state, now) or [f"[MyAnimeList]({show['mal_url']})"]
    fields.append({"name": "Watch", "value": clip(" · ".join(links), 1024), "inline": False})

    embed = {
        "title": clip(show["title"], 256),
        "url": show["mal_url"],
        "description": headline,
        "color": show["color"],
        "fields": fields,
        "footer": {"text": "Ramune · Plan to Watch"},
        "timestamp": iso(when or now),
    }
    if show["cover"]:
        embed["thumbnail"] = {"url": show["cover"]}
    return embed


def check_plan_to_watch(cfg, state, now):
    """Alert once when a Plan to Watch show premieres or is released all at once.
    The first time a show is seen (including the very first run) it is only recorded,
    so anything already out is never announced."""
    try:
        ptw = fetch_mal_list(cfg, "plan_to_watch")
    except UpstreamError as err:
        warn(f"Plan to Watch skipped this run: {err}")
        return
    pstate = state.setdefault("ptw", {})
    seen = pstate.setdefault("seen", {})
    done = pstate.setdefault("done", {})
    on_list = {str(w["mal_id"]) for w in ptw}
    for store in (seen, done):           # removed from Plan to Watch: forget it
        for key in [k for k in store if k not in on_list]:
            del store[key]

    todo = [w for w in ptw if str(w["mal_id"]) not in done]
    if not todo:
        return
    media_by_mal = fetch_anilist_media([w["mal_id"] for w in todo])
    shows = {}
    for w in todo:
        media = media_by_mal.get(w["mal_id"])
        if media:
            shows[w["mal_id"]] = build_show(w, media)
    by_aid = {s["anilist_id"]: s for s in shows.values()}
    ep1 = {}
    if by_aid:
        for e in fetch_schedules(list(by_aid), now - cfg.lookback, now + 7 * 86400):
            if e["episode"] == 1:
                ep1[by_aid[e["mediaId"]]["mal_id"]] = e["airingAt"]

    alerts, first_seen = [], 0
    for mal_id, show in shows.items():
        key = str(mal_id)
        entry = seen.get(key)
        result = ptw_release(show, entry or {}, ep1.get(mal_id), now, cfg.delay)
        if entry is None:                 # new to us: record, never announce what's already out
            first_seen += 1
            if result:
                done[key] = now
            else:
                seen[key] = {"since": now, "sched": bool(show["next"] or mal_id in ep1)}
            continue
        if show["next"] or mal_id in ep1:
            entry["sched"] = True
        if result:
            alerts.append((show, result))
    if first_seen:
        log(f"Plan to Watch: recorded {first_seen} show(s) as a starting point")

    for group in chunks(alerts, 10):      # Discord allows 10 embeds per message
        names = ", ".join(clip(s["title"], 60) for s, _ in group)
        if len(group) == 1:
            content = f"🫧 **{clip(group[0][0]['title'], 200)}** from your Plan to Watch is out"
        else:
            content = f"🫧 {len(group)} shows from your Plan to Watch are out"
        log(f"Plan to Watch out: {names}")
        discord_post(cfg, {"content": clip(content, 2000),
                           "embeds": [ptw_embed(cfg, s, kind, when, now, state) for s, (kind, when) in group]})
        for s, _ in group:
            key = str(s["mal_id"])
            seen.pop(key, None)
            done[key] = now
        time.sleep(1)

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
    mal_ok = True
    try:
        watching = fetch_mal_watching(cfg)
        state["watching_cache"] = {str(w["mal_id"]): w["title"] for w in watching}
        outage_ok(state, "MyAnimeList")
    except UpstreamError as err:
        cache = state.get("watching_cache") or {}
        if not cache:
            raise
        mal_ok = False
        outage_failed(state, "MyAnimeList", now)
        warn(f"{err}. Using the last known Watching list ({len(cache)} shows).")
        watching = [{"mal_id": int(k), "title": v, "title_en": "", "watched": None} for k, v in cache.items()]
    log(f"MAL: {len(watching)} shows on {cfg.username}'s Watching list")

    # 2. Map to AniList
    media_by_mal = fetch_anilist_media([w["mal_id"] for w in watching]) if watching else {}
    if watching:
        outage_ok(state, "AniList")
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
        today = send_summary(cfg, shows, schedule, now, state)
        state["last_summary"] = today_str
        state["digest"] = {"date": today_str, "eps": {f"{e['mediaId']}:{e['episode']}": e["airingAt"] for e in today}}
    elif cfg.schedule_changes and (state.get("digest") or {}).get("date") == today_str:
        check_schedule_changes(cfg, state["digest"], shows, schedule, now)
    if cfg.mode == "test":
        log("Sending test message")
        send_test(cfg, watching, shows, unmapped, now)

    if cfg.ptw_alerts and mal_ok:
        check_plan_to_watch(cfg, state, now)

    if cfg.up_next:
        update_up_next(cfg, state, shows, now)

    maybe_outage_alert(cfg, state, now)

    # 7. Housekeeping
    for key in [k for k, ts in announced.items() if ts < now - ANNOUNCED_KEEP]:
        del announced[key]
    if now - state.get("heartbeat_at", 0) > HEARTBEAT_EVERY:
        state["heartbeat_at"] = now

    write_job_summary(cfg, shows, unmapped, announced_now, now)


def run(cfg, state, now):
    """One check. Returns the exit code; the caller saves state."""
    try:
        process(cfg, state, now)
        state.pop("failures", None)
        return 0
    except ConfigError as err:
        print(f"::error::{err}", flush=True)
        return 1
    except UpstreamError as err:
        failures = state.get("failures", 0) + 1
        state["failures"] = failures
        warn(f"{err} (failure {failures} in a row; the next run will catch up on anything missed)")
        if err.service in ("MyAnimeList", "AniList"):
            outage_failed(state, err.service, now)
            maybe_outage_alert(cfg, state, now)
        # Discord down: no message possible, the red run (GitHub email) is the alert
        if failures == FAIL_ALERT_AFTER or (failures > FAIL_ALERT_AFTER and failures % 48 == 0):
            return 1  # red run -> GitHub emails you
        return 0


def main():
    try:
        cfg = Config()
    except ConfigError as err:
        print(f"::error::{err}", flush=True)
        return 1

    state = load_state()
    code = run(cfg, state, int(time.time()))
    save_state(state)
    return code


if __name__ == "__main__":
    sys.exit(main())
