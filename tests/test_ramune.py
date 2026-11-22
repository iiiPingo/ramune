"""Ramune test suite. Simulates MyAnimeList, AniList and Discord, so it runs offline.

    python3 -m unittest discover -s tests -v
"""

import copy
import json
import os
import sys
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ramune  # noqa: E402

DUBAI = timezone(timedelta(hours=4))
WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def at(day, hour, minute=0):
    """Unix time for 2026-11-<day> hour:minute in Dubai."""
    return int(datetime(2026, 11, day, hour, minute, tzinfo=DUBAI).timestamp())


class World:
    """Fake MAL + AniList + Discord."""

    def __init__(self):
        self.lists = {"watching": [], "plan_to_watch": []}   # MAL rows (mal_id, title)
        self.media = {}            # mal_id -> AniList media dict
        self.schedule = []         # {"mediaId", "episode", "airingAt"}
        self.down = set()          # {"MyAnimeList", "AniList", "Discord"}
        self.posts = []            # Discord payloads (POST)
        self.patches = []
        self.requests = []         # (what, detail)
        self.webhook_gone = False
        self.anikoto = []          # (english, romaji, slug) results Anikoto's search knows
        self.anikoto_down = False
        self.anikoto_requests = []

    def fetch_text(self, url, what):
        self.anikoto_requests.append(url)
        if self.anikoto_down:
            return None
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("keyword", [""])[0].lower()
        words = [w for w in q.split() if len(w) > 2]
        items = "".join(
            f'<div class="item "><div class="inner"><div class="ani poster tip" data-tip="8">'
            f'<a href="https://anikototv.bz/watch/{slug}/ep-1"><img alt="{en}" /></a></div>'
            f'<div class="info"><div class="b1"><a class="name d-title" href="https://anikototv.bz/watch/{slug}/ep-1"'
            f' data-jp="{jp}">{en}</a></div></div></div></div>'
            for en, jp, slug in self.anikoto
            if any(w in (en + " " + jp).lower() for w in words))
        sidebar = ('<a href="https://anikototv.bz/watch/one-piece-odmau">One Piece</a>'
                   '<a class="name" href="https://anikototv.bz/watch/kill-blue-gcqj5">Kill Blue</a>')
        return f'<html><body><div id="list-items" class="ani items">{items}</div>{sidebar}</body></html>'

    # ---- builders
    def add(self, list_name, mal_id, title, aid=None, status="RELEASING", episodes=12, fmt="TV",
            next_ep=None, links=None, start=(2026, 10, 1), english=None):
        aid = aid or mal_id + 100000
        self.lists[list_name].append({"node": {"id": mal_id, "title": title,
                                               "alternative_titles": {"en": english or ""}},
                                      "list_status": {"num_episodes_watched": 0}})
        self.media[mal_id] = {
            "id": aid, "idMal": mal_id, "status": status, "episodes": episodes, "format": fmt,
            "siteUrl": f"https://anilist.co/anime/{aid}",
            "startDate": {"year": start[0], "month": start[1], "day": start[2]} if start else
                         {"year": None, "month": None, "day": None},
            "title": {"romaji": title, "english": english},
            "coverImage": {"large": f"https://img/{aid}.jpg", "color": "#ff8800"},
            "nextAiringEpisode": next_ep,
            "externalLinks": links if links is not None else [],
        }
        return aid

    def remove(self, list_name, mal_id):
        self.lists[list_name] = [r for r in self.lists[list_name] if r["node"]["id"] != mal_id]

    # ---- fake http
    def http(self, method, url, headers=None, body=None, what="request", attempts=5):
        self.requests.append((what, url if what != "AniList" else body["query"][:40]))
        if what in self.down:
            raise ramune.UpstreamError(f"{what} unreachable: simulated", what)
        if what == "MyAnimeList":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            status = q["status"][0]
            return 200, {"data": copy.deepcopy(self.lists.get(status, [])), "paging": {}}
        if what == "AniList":
            v = body["variables"]
            if "airingSchedules" in body["query"]:
                eps = [e for e in self.schedule if e["mediaId"] in v["ids"]
                       and v["from"] < e["airingAt"] < v["to"]]
                return 200, {"data": {"Page": {"pageInfo": {"hasNextPage": False},
                                               "airingSchedules": copy.deepcopy(eps)}}}
            media = [copy.deepcopy(m) for k, m in self.media.items() if k in v["ids"]]
            return 200, {"data": {"Page": {"pageInfo": {"hasNextPage": False}, "media": media}}}
        if what == "Discord":
            if self.webhook_gone:
                return 404, {"code": 10015}
            if method == "PATCH":
                self.patches.append(body)
                return 200, {}
            self.posts.append(body)
            if "wait=true" in url:
                return 200, {"id": str(len(self.posts))}
            return 204, None
        raise AssertionError(what)

    # ---- helpers for assertions
    def texts(self):
        out = []
        for p in self.posts:
            parts = [p.get("content") or ""]
            for e in p.get("embeds") or []:
                parts.append(e.get("title") or "")
                parts.append(e.get("description") or "")
                parts += [f["value"] for f in e.get("fields", [])]
            out.append("\n".join(parts))
        return out

    def outage_posts(self):
        return [p for p in self.posts if "can't reach" in (p.get("content") or "")]

    def ptw_posts(self):
        return [p for p in self.posts if "Plan to Watch" in (p.get("content") or "")]

    def summaries(self):
        return [p for p in self.posts if (p.get("content") or "").startswith("🫧 Today's schedule")]


BASE_ENV = {
    "MAL_CLIENT_ID": "id", "DISCORD_WEBHOOK_URL": WEBHOOK, "MAL_USERNAME": "iPingo",
    "TIMEZONE": "Asia/Dubai", "EPISODE_ALERTS": "true", "DAILY_SUMMARY": "true",
    "UP_NEXT": "false", "SUMMARY_HOUR": "9",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.state = {}
        self.env = dict(BASE_ENV)
        self.logs = []
        patches = [
            mock.patch.object(ramune, "http", self.world.http),
            mock.patch.object(ramune, "fetch_text", self.world.fetch_text),
            mock.patch.object(ramune.time, "sleep", lambda s: None),
            mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}),
            mock.patch.object(ramune, "log", self.logs.append),
            mock.patch.object(ramune, "warn", self.logs.append),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_at(self, now, **env):
        e = dict(self.env, **{k: str(v) for k, v in env.items()})
        with mock.patch.dict(os.environ, e, clear=False):
            for k in [k for k, v in e.items() if v == ""]:
                os.environ.pop(k, None)
            cfg = ramune.Config()
        out = ramune.run(cfg, self.state, now)
        json.dumps(self.state)  # state must stay JSON-serialisable
        return out


# =========================================================================== existing behaviour

class ExistingBehaviour(Base):
    def setUp(self):
        super().setUp()
        self.aid = self.world.add("watching", 1, "Blue Box Season 2", english="Blue Box Season 2",
                                  next_ep={"episode": 8, "airingAt": at(23, 19)})
        self.env["PTW_ALERTS"] = "false"

    def test_first_run_is_silent(self):
        self.world.schedule.append({"mediaId": self.aid, "episode": 7, "airingAt": at(22, 7)})
        self.assertEqual(self.run_at(at(22, 8)), 0)
        self.assertFalse([p for p in self.world.posts if "is out" in (p.get("content") or "")])
        self.assertIn("initialized_at", self.state)

    def test_episode_announced_once(self):
        self.run_at(at(22, 6), DAILY_SUMMARY="false")
        self.world.schedule.append({"mediaId": self.aid, "episode": 7, "airingAt": at(22, 7)})
        self.run_at(at(22, 7, 15), DAILY_SUMMARY="false")
        self.run_at(at(22, 7, 30), DAILY_SUMMARY="false")
        eps = [p for p in self.world.posts if "Episode 7" in (p.get("content") or "")]
        self.assertEqual(len(eps), 1)

    def test_episode_alerts_off_records_without_posting(self):
        self.run_at(at(22, 6), DAILY_SUMMARY="false", EPISODE_ALERTS="false")
        self.world.schedule.append({"mediaId": self.aid, "episode": 7, "airingAt": at(22, 7)})
        self.run_at(at(22, 7, 15), DAILY_SUMMARY="false", EPISODE_ALERTS="false")
        self.assertEqual(self.world.posts, [])
        self.assertIn(f"{self.aid}:7", self.state["announced"])

    def test_summary_once_per_day(self):
        self.world.schedule.append({"mediaId": self.aid, "episode": 7, "airingAt": at(22, 19)})
        self.run_at(at(22, 8))
        self.assertEqual(len(self.world.summaries()), 0)
        self.run_at(at(22, 9, 5))
        self.run_at(at(22, 9, 20))
        self.assertEqual(len(self.world.summaries()), 1)
        self.assertIn("Ep 7", self.world.texts()[0])

    def test_schedule_change(self):
        ep = {"mediaId": self.aid, "episode": 7, "airingAt": at(22, 19)}
        self.world.schedule.append(ep)
        self.run_at(at(22, 9, 5))
        ep["airingAt"] = at(22, 20)
        self.run_at(at(22, 9, 20))
        self.run_at(at(22, 9, 35))
        changes = [t for t in self.world.texts() if "moved" in t]
        self.assertEqual(len(changes), 1)

    def test_up_next_posts_then_edits_only_on_change(self):
        self.run_at(at(22, 6), UP_NEXT="true", DAILY_SUMMARY="false")
        self.run_at(at(22, 6, 15), UP_NEXT="true", DAILY_SUMMARY="false")
        self.assertEqual(len(self.world.posts), 1)
        self.assertEqual(len(self.world.patches), 0)
        self.world.media[1]["nextAiringEpisode"] = {"episode": 8, "airingAt": at(23, 20)}
        self.run_at(at(22, 6, 30), UP_NEXT="true", DAILY_SUMMARY="false")
        self.assertEqual(len(self.world.patches), 1)

    def test_test_mode(self):
        self.run_at(at(22, 6), MODE="test", DAILY_SUMMARY="false")
        self.assertTrue(any("Ramune test" in (p.get("content") or "") for p in self.world.posts))

    def test_mal_down_uses_cache(self):
        self.run_at(at(22, 6), DAILY_SUMMARY="false")
        self.world.down.add("MyAnimeList")
        self.assertEqual(self.run_at(at(22, 6, 15), DAILY_SUMMARY="false"), 0)
        self.assertIn(str(self.aid), self.state["shows"])

    def test_red_run_after_threshold(self):
        self.run_at(at(22, 6))
        self.world.down.add("AniList")
        codes = [self.run_at(at(22, 1) + i * 900, FAIL_ALERT="false") for i in range(8)]
        self.assertEqual(codes, [0] * 7 + [1])

    def test_webhook_deleted_is_config_error(self):
        self.world.webhook_gone = True
        self.world.schedule.append({"mediaId": self.aid, "episode": 7, "airingAt": at(22, 7)})
        self.assertEqual(self.run_at(at(22, 9, 5)), 1)

    def test_missing_secret(self):
        with mock.patch.dict(os.environ, dict(BASE_ENV, MAL_CLIENT_ID="")):
            with self.assertRaises(ramune.ConfigError):
                ramune.Config()


# =========================================================================== 1) failure alert

class FailureAlert(Base):
    def setUp(self):
        super().setUp()
        self.world.add("watching", 1, "Overgeared", next_ep={"episode": 9, "airingAt": at(25, 19)})
        self.env.update(DAILY_SUMMARY="false", PTW_ALERTS="false")
        self.run_at(at(22, 12))

    def down_runs(self, start, count):
        return [self.run_at(start + i * 900) for i in range(count)]

    def test_one_alert_no_repeats(self):
        self.world.down.add("AniList")
        self.down_runs(at(22, 14), 7)
        self.assertEqual(self.world.outage_posts(), [])
        self.down_runs(at(22, 16), 20)
        posts = self.world.outage_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("**AniList**", posts[0]["content"])
        self.assertLess(len(posts[0]["content"]), 200)
        self.assertNotIn("embeds", posts[0])

    def test_no_recovered_message_and_new_outage_alerts_again(self):
        self.world.down.add("AniList")
        self.down_runs(at(22, 14), 8)
        self.world.down.clear()
        before = len(self.world.posts)
        self.run_at(at(22, 17))
        self.assertEqual(len(self.world.posts), before)          # silent recovery
        self.assertNotIn("outages", self.state)
        self.world.down.add("AniList")
        self.down_runs(at(22, 18), 8)
        self.assertEqual(len(self.world.outage_posts()), 2)

    def test_night_outage_waits_for_window(self):
        self.world.down.add("AniList")
        self.down_runs(at(23, 2), 30)                            # 02:00 .. 09:15
        self.assertEqual(self.world.outage_posts(), [])
        self.down_runs(at(23, 12, 45), 1)
        self.assertEqual(self.world.outage_posts(), [])
        self.down_runs(at(23, 13), 3)
        self.assertEqual(len(self.world.outage_posts()), 1)
        self.assertIn("since Mon 23 Nov, 2:00 AM", self.world.outage_posts()[0]["content"])

    def test_night_outage_that_recovers_sends_nothing(self):
        self.world.down.add("AniList")
        self.down_runs(at(23, 2), 20)
        self.world.down.clear()
        self.run_at(at(23, 13, 30))
        self.assertEqual(self.world.outage_posts(), [])

    def test_no_alert_at_or_after_23(self):
        self.world.down.add("AniList")
        self.down_runs(at(22, 23), 12)
        self.assertEqual(self.world.outage_posts(), [])

    def test_custom_window(self):
        self.world.down.add("AniList")
        codes = [self.run_at(at(23, 2) + i * 900, FAIL_ALERT_FROM=1, FAIL_ALERT_TO=5) for i in range(8)]
        self.assertEqual(len(self.world.outage_posts()), 1)
        self.assertEqual(codes[-1], 1)

    def test_mal_down_with_cache_alerts_and_stays_green(self):
        self.world.down.add("MyAnimeList")
        codes = self.down_runs(at(22, 14), 10)
        self.assertEqual(codes, [0] * 10)
        self.assertEqual(len(self.world.outage_posts()), 1)
        self.assertIn("**MyAnimeList**", self.world.outage_posts()[0]["content"])

    def test_both_down_is_one_message(self):
        self.world.down.update({"MyAnimeList", "AniList"})
        self.down_runs(at(22, 14), 10)
        posts = self.world.outage_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("**AniList** and **MyAnimeList**", posts[0]["content"])

    def test_discord_down_keeps_red_run(self):
        self.world.down.add("Discord")
        self.world.schedule.append({"mediaId": 100001, "episode": 9, "airingAt": at(22, 13)})
        codes = self.down_runs(at(22, 14), 8)
        self.assertEqual(codes[-1], 1)
        self.assertNotIn("Discord", self.state.get("outages", {}))

    def test_alert_retried_if_discord_also_fails(self):
        self.world.down.update({"AniList", "Discord"})
        self.down_runs(at(22, 14), 9)
        self.world.down.discard("Discord")
        self.down_runs(at(22, 17), 2)
        self.assertEqual(len(self.world.outage_posts()), 1)

    def test_toggle_off(self):
        self.world.down.add("AniList")
        codes = [self.run_at(at(22, 14) + i * 900, FAIL_ALERT="false") for i in range(10)]
        self.assertEqual(self.world.outage_posts(), [])
        self.assertEqual(codes[7], 1)                            # red run still happens

    def test_in_window(self):
        self.assertTrue(ramune.in_window(13, 13, 23))
        self.assertTrue(ramune.in_window(22, 13, 23))
        self.assertFalse(ramune.in_window(23, 13, 23))
        self.assertFalse(ramune.in_window(12, 13, 23))
        self.assertTrue(ramune.in_window(1, 22, 6))
        self.assertFalse(ramune.in_window(12, 22, 6))
        self.assertTrue(ramune.in_window(5, 0, 0))


# =========================================================================== 2) morning links

CR = [{"site": "Crunchyroll", "url": "https://www.crunchyroll.com/series/GX9UQE1WK/blue-box", "type": "STREAMING"},
      {"site": "Netflix", "url": "https://www.netflix.com/title/1", "type": "STREAMING"},
      {"site": "Official Site", "url": "https://aonohako.jp", "type": "INFO"}]


class SummaryLinks(Base):
    def setUp(self):
        super().setUp()
        self.env["PTW_ALERTS"] = "false"
        a = self.world.add("watching", 1, "Ao no Hako Season 2", english="Blue Box Season 2", links=CR)
        b = self.world.add("watching", 2, "Tempal: Item no Chikara", english="Overgeared")
        self.world.schedule += [{"mediaId": a, "episode": 8, "airingAt": at(22, 19)},
                                {"mediaId": b, "episode": 9, "airingAt": at(22, 21)}]

    def summary(self, **env):
        self.run_at(at(22, 9, 5), **env)
        return self.world.summaries()[-1]["embeds"][0]["description"]

    def test_crunchyroll_and_anikoto(self):
        d = self.summary()
        self.assertIn("[Crunchyroll](https://www.crunchyroll.com/series/GX9UQE1WK/blue-box)", d)
        self.assertIn("[Anikoto](https://anikototv.bz/filter?keyword=Blue+Box+Season+2)", d)
        self.assertIn("[Anikoto](https://anikototv.bz/filter?keyword=Overgeared)", d)
        self.assertNotIn("Netflix", d)
        self.assertEqual(d.count("Crunchyroll"), 1)              # only shows that have it

    def test_custom_domain_and_template(self):
        self.assertIn("https://anikoto.example/filter?keyword=Overgeared", self.summary(ANIKOTO_URL="anikoto.example"))
        self.world.posts.clear()
        self.state.pop("last_summary")
        d = self.summary(ANIKOTO_URL="https://kotoplay.to/search?keyword={q}")
        self.assertIn("https://kotoplay.to/search?keyword=Overgeared", d)

    def test_anikoto_off(self):
        d = self.summary(ANIKOTO_URL="off")
        self.assertNotIn("Anikoto", d)
        self.assertIn("Crunchyroll", d)

    def test_links_toggle(self):
        d = self.summary(SUMMARY_LINKS="false")
        self.assertNotIn("Anikoto", d)
        self.assertNotIn("Crunchyroll", d)

    def test_markdown_safe_urls(self):
        cfg = type("C", (), {"anikoto": ramune.ANIKOTO_DEFAULT})()
        url = ramune.anikoto_url(cfg, "Kaguya-sama (Movie) & Friends")
        self.assertNotIn("(", url)
        self.assertNotIn(" ", url)
        self.assertEqual(ramune.md_url("https://a/b_(c)"), "https://a/b_%28c%29")


# =========================================================================== 3) plan to watch

class PlanToWatch(Base):
    def setUp(self):
        super().setUp()
        self.env.update(DAILY_SUMMARY="false")
        self.world.add("watching", 1, "Overgeared", next_ep={"episode": 9, "airingAt": at(29, 19)})

    def first_run(self):
        self.run_at(at(22, 10))
        self.assertEqual(self.world.ptw_posts(), [])

    def test_first_run_silent_for_released_and_pending(self):
        self.world.add("plan_to_watch", 10, "Old Finished Show", status="FINISHED")
        self.world.add("plan_to_watch", 11, "Released Movie", status="FINISHED", fmt="MOVIE", episodes=1)
        self.world.add("plan_to_watch", 12, "Airing Already", next_ep={"episode": 5, "airingAt": at(25, 1)})
        self.world.add("plan_to_watch", 13, "Next Season", status="NOT_YET_RELEASED",
                       next_ep={"episode": 1, "airingAt": at(30, 1)}, start=(2026, 11, 30))
        self.first_run()
        self.run_at(at(22, 10, 15))
        self.assertEqual(self.world.ptw_posts(), [])
        self.assertEqual(set(self.state["ptw"]["done"]), {"10", "11", "12"})
        self.assertEqual(set(self.state["ptw"]["seen"]), {"13"})

    def test_weekly_premiere_alert_once(self):
        aid = self.world.add("plan_to_watch", 20, "Hotaru no Yomeiri", english="Firefly Wedding",
                             status="NOT_YET_RELEASED", next_ep={"episode": 1, "airingAt": at(23, 1)},
                             links=CR[:1], start=(2026, 11, 23))
        self.world.schedule.append({"mediaId": aid, "episode": 1, "airingAt": at(23, 1)})
        self.first_run()
        self.run_at(at(23, 0, 45))
        self.assertEqual(self.world.ptw_posts(), [])
        self.world.media[20]["nextAiringEpisode"] = {"episode": 2, "airingAt": at(30, 1)}
        self.run_at(at(23, 1, 5))
        self.run_at(at(23, 1, 20))
        self.world.media[20]["status"] = "RELEASING"
        self.run_at(at(24, 1, 20))
        posts = self.world.ptw_posts()
        self.assertEqual(len(posts), 1)
        e = posts[0]["embeds"][0]
        self.assertEqual(e["title"], "Firefly Wedding")
        self.assertIn("Episode 1 is out", e["description"])
        self.assertEqual(e["thumbnail"]["url"], f"https://img/{aid}.jpg")
        watch = [f for f in e["fields"] if f["name"] == "Watch"][0]["value"]
        self.assertIn("[Crunchyroll](", watch)
        self.assertIn("[Anikoto](https://anikototv.bz/filter?keyword=Firefly+Wedding)", watch)
        self.assertEqual(e["footer"]["text"], "Ramune · Plan to Watch")

    def test_respects_announce_delay(self):
        aid = self.world.add("plan_to_watch", 21, "Delayed", status="NOT_YET_RELEASED",
                             next_ep={"episode": 1, "airingAt": at(23, 1)})
        self.world.schedule.append({"mediaId": aid, "episode": 1, "airingAt": at(23, 1)})
        self.run_at(at(22, 10), ANNOUNCE_DELAY_MINUTES=30)
        self.world.media[21]["nextAiringEpisode"] = {"episode": 2, "airingAt": at(30, 1)}
        self.run_at(at(23, 1, 15), ANNOUNCE_DELAY_MINUTES=30)
        self.assertEqual(self.world.ptw_posts(), [])
        self.run_at(at(23, 1, 35), ANNOUNCE_DELAY_MINUTES=30)
        self.assertEqual(len(self.world.ptw_posts()), 1)

    def test_status_lag_still_caught_by_schedule(self):
        aid = self.world.add("plan_to_watch", 22, "Lagging", status="NOT_YET_RELEASED",
                             next_ep={"episode": 1, "airingAt": at(23, 1)})
        self.world.schedule.append({"mediaId": aid, "episode": 1, "airingAt": at(23, 1)})
        self.first_run()
        self.world.media[22]["nextAiringEpisode"] = None      # AniList hasn't caught up at all
        self.run_at(at(23, 1, 10))
        self.assertEqual(len(self.world.ptw_posts()), 1)

    def test_postponed_premiere_not_alerted(self):
        self.world.add("plan_to_watch", 23, "Postponed", status="NOT_YET_RELEASED",
                       next_ep={"episode": 1, "airingAt": at(23, 1)})
        self.first_run()
        self.world.media[23]["nextAiringEpisode"] = None       # schedule pulled, no new date
        self.run_at(at(23, 1, 10))
        self.run_at(at(24, 1, 10))
        self.assertEqual(self.world.ptw_posts(), [])

    def test_movie_release(self):
        self.world.add("plan_to_watch", 30, "Kimi no Movie", fmt="MOVIE", episodes=1,
                       status="NOT_YET_RELEASED", start=(2026, 11, 27))
        self.first_run()
        self.run_at(at(26, 10))
        self.world.media[30]["status"] = "FINISHED"
        self.run_at(at(27, 10))
        self.run_at(at(27, 10, 15))
        posts = self.world.ptw_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("movie is out", posts[0]["embeds"][0]["description"])

    def test_netflix_batch(self):
        self.world.add("plan_to_watch", 31, "Netflix Drop", fmt="ONA", episodes=10,
                       status="NOT_YET_RELEASED", start=(2026, 11, 26))
        self.first_run()
        self.world.media[31]["status"] = "FINISHED"
        self.run_at(at(26, 12))
        posts = self.world.ptw_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("All 10 episodes are out", posts[0]["embeds"][0]["description"])
        fmt = [f for f in posts[0]["embeds"][0]["fields"] if f["name"] == "Format"][0]["value"]
        self.assertEqual(fmt, "ONA · 10 episodes")

    def test_batch_with_wrong_future_start_date_waits(self):
        self.world.add("plan_to_watch", 32, "Early Status", fmt="ONA", status="NOT_YET_RELEASED",
                       start=(2026, 12, 5))
        self.first_run()
        self.world.media[32]["status"] = "RELEASING"
        self.run_at(at(23, 12))
        self.assertEqual(self.world.ptw_posts(), [])

    def test_batch_with_schedule_all_same_time(self):
        aid = self.world.add("plan_to_watch", 33, "Batch With Schedule", fmt="ONA", episodes=3,
                             status="NOT_YET_RELEASED", next_ep={"episode": 1, "airingAt": at(23, 11)})
        self.world.schedule += [{"mediaId": aid, "episode": n, "airingAt": at(23, 11)} for n in (1, 2, 3)]
        self.first_run()
        self.world.media[33].update(status="FINISHED", nextAiringEpisode=None)
        self.run_at(at(23, 11, 10))
        posts = self.world.ptw_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("All 3 episodes", posts[0]["embeds"][0]["description"])

    def test_continuing_episode_numbers(self):
        self.world.add("plan_to_watch", 40, "Part 2", status="NOT_YET_RELEASED",
                       next_ep={"episode": 13, "airingAt": at(24, 1)})
        self.first_run()
        self.assertIn("40", self.state["ptw"]["seen"])
        self.world.media[40].update(status="RELEASING", nextAiringEpisode={"episode": 14, "airingAt": at(30, 23)})
        self.run_at(at(24, 1, 10))
        self.assertEqual(len(self.world.ptw_posts()), 1)

    def test_added_after_release_is_silent(self):
        self.first_run()
        self.world.add("plan_to_watch", 50, "Already Out", status="FINISHED")
        self.run_at(at(23, 10))
        self.assertEqual(self.world.ptw_posts(), [])
        self.assertIn("50", self.state["ptw"]["done"])

    def test_removed_then_readded_does_not_realert(self):
        self.world.add("plan_to_watch", 51, "Toggle", status="NOT_YET_RELEASED",
                       next_ep={"episode": 1, "airingAt": at(23, 1)})
        self.first_run()
        self.world.media[51].update(status="RELEASING", nextAiringEpisode={"episode": 2, "airingAt": at(30, 1)})
        self.run_at(at(23, 1, 10))
        self.assertEqual(len(self.world.ptw_posts()), 1)
        row = self.world.lists["plan_to_watch"][-1]
        self.world.remove("plan_to_watch", 51)
        self.run_at(at(23, 2))
        self.assertNotIn("51", self.state["ptw"]["done"])
        self.world.lists["plan_to_watch"].append(row)
        self.run_at(at(23, 3))
        self.assertEqual(len(self.world.ptw_posts()), 1)

    def test_many_at_once_batched_ten_per_message(self):
        for i in range(12):
            self.world.add("plan_to_watch", 60 + i, f"Show {i}", status="NOT_YET_RELEASED", start=(2026, 11, 25))
        self.first_run()
        for i in range(12):
            self.world.media[60 + i]["status"] = "FINISHED"
        self.run_at(at(25, 12))
        posts = self.world.ptw_posts()
        self.assertEqual([len(p["embeds"]) for p in posts], [10, 2])
        self.assertIn("10 shows", posts[0]["content"])

    def test_toggle_off_then_on_does_not_flood(self):
        self.world.add("plan_to_watch", 70, "Will Release", status="NOT_YET_RELEASED", start=(2026, 11, 24))
        self.run_at(at(22, 10), PTW_ALERTS="false")
        self.assertFalse(any("plan_to_watch" in str(r[1]) for r in self.world.requests))
        self.world.media[70]["status"] = "FINISHED"
        self.run_at(at(25, 10))                                   # turned on after it released
        self.assertEqual(self.world.ptw_posts(), [])

    def test_only_unreleased_are_rechecked(self):
        self.world.add("plan_to_watch", 80, "Old", status="FINISHED")
        self.world.add("plan_to_watch", 81, "New", status="NOT_YET_RELEASED")
        self.first_run()
        seen = []
        orig = ramune.fetch_anilist_media
        with mock.patch.object(ramune, "fetch_anilist_media", lambda ids: seen.append(sorted(ids)) or orig(ids)):
            self.run_at(at(22, 10, 15))
        self.assertIn([81], seen)

    def test_ptw_fetch_failure_does_not_break_run(self):
        self.first_run()
        real = self.world.http

        def flaky(method, url, *a, **k):
            if "plan_to_watch" in url:
                raise ramune.UpstreamError("MAL flaked", "MyAnimeList")
            return real(method, url, *a, **k)
        with mock.patch.object(ramune, "http", flaky):
            self.assertEqual(self.run_at(at(22, 10, 15)), 0)

    def test_start_reached(self):
        now = at(22, 10)
        self.assertTrue(ramune.start_reached({"start": {"year": 2026, "month": 11, "day": 22}}, now))
        self.assertFalse(ramune.start_reached({"start": {"year": 2026, "month": 11, "day": 23}}, now))
        self.assertTrue(ramune.start_reached({"start": {"year": 2026, "month": None, "day": None}}, now))
        self.assertTrue(ramune.start_reached({"start": {}}, now))


# =========================================================================== exact Anikoto episode links

REAL_ANIKOTO = [  # captured from anikototv.bz search results on 2026-11-22
    ("Blue Box", "Ao no Hako", "blue-box-ddjdm"),
    ("Blue Box Season 2", "Ao no Hako Season 2", "blue-box-season-2"),
    ("Blue Lock Season 2", "Blue Lock vs. U-20 Japan", "blue-lock-season-2-54rmn"),
    ("Overgeared", "Tempal: Item no Chikara", "overgeared-a487c"),
    ("Chitose Is in the Ramune Bottle", "Chitose-kun wa Ramune Bin no Naka", "chitose-is-in-the-ramune-bottle-lvlwo"),
    ("Chitose Is in the Ramune Bottle Part 2", "Chitose-kun wa Ramune Bin no Naka Part 2",
     "chitose-is-in-the-ramune-bottle-part-2-3dfd8"),
    ("Tensei Shitara Ken Deshita 2nd Season", "Tensei Shitara Ken Deshita 2nd Season",
     "tensei-shitara-ken-deshita-2nd-season"),
    ("The World&#039;s Finest Assassin Gets Reincarnated in a Different World as an Aristocrat",
     "Sekai Saikou no Ansatsusha, Isekai Kizoku ni Tensei suru",
     "the-world-s-finest-assassin-gets-reincarnated-in-a-different-world-as-an-aristocrat-z0xmr"),
    ("Firefly Wedding", "Hotaru no Yomeiri", "firefly-wedding-94459"),
]


class AnikotoEpisodeLinks(Base):
    def setUp(self):
        super().setUp()
        self.env["PTW_ALERTS"] = "false"
        self.world.anikoto = list(REAL_ANIKOTO)

    def add_today(self, mal_id, mal_title, english=None, romaji=None, ep=7, hour=19, synonyms=None):
        aid = self.world.add("watching", mal_id, mal_title, english=english)
        if romaji:
            self.world.media[mal_id]["title"]["romaji"] = romaji
        self.world.media[mal_id]["synonyms"] = synonyms or []
        self.world.schedule.append({"mediaId": aid, "episode": ep, "airingAt": at(22, hour)})
        return aid

    def summary(self, day=22, **env):
        self.run_at(at(day, 9, 5), **env)
        return self.world.summaries()[-1]["embeds"][0]["description"]

    def test_exact_episode_links_for_real_titles(self):
        self.add_today(1, "Ao no Hako Season 2", "Blue Box Season 2", ep=8)
        self.add_today(2, "Tempal: Item no Chikara", "Overgeared", ep=9, hour=20)
        self.add_today(3, "Chitose-kun wa Ramune Bin no Naka Part 2", "Chitose Is in the Ramune Bottle 2nd Cour",
                       ep=5, hour=21)
        self.add_today(4, "Tensei shitara Ken deshita II", "Reincarnated as a Sword Season 2",
                       romaji="Tensei Shitara Ken Deshita 2nd Season", ep=7, hour=22)
        self.add_today(5, "Sekai Saikou no Ansatsusha, Isekai Kizoku ni Tensei suru",
                       "The World's Finest Assassin Gets Reincarnated in Another World as an Aristocrat", ep=3, hour=23)
        d = self.summary()
        self.assertIn("[Anikoto Ep 8](https://anikototv.bz/watch/blue-box-season-2/ep-8)", d)
        self.assertIn("[Anikoto Ep 9](https://anikototv.bz/watch/overgeared-a487c/ep-9)", d)
        self.assertIn("(https://anikototv.bz/watch/chitose-is-in-the-ramune-bottle-part-2-3dfd8/ep-5)", d)
        self.assertIn("(https://anikototv.bz/watch/tensei-shitara-ken-deshita-2nd-season/ep-7)", d)
        self.assertIn("aristocrat-z0xmr/ep-3)", d)
        self.assertNotIn("blue-box-ddjdm", d)                  # season 1 never picked for season 2
        self.assertNotIn("chitose-is-in-the-ramune-bottle-lvlwo", d)

    def test_slug_cached_no_repeat_searches(self):
        self.add_today(1, "Ao no Hako Season 2", "Blue Box Season 2")
        self.summary(22)
        n = len(self.world.anikoto_requests)
        self.assertEqual(n, 1)
        self.world.schedule.append({"mediaId": 100001, "episode": 8, "airingAt": at(23, 19)})
        d = self.summary(23)
        self.assertEqual(len(self.world.anikoto_requests), n)
        self.assertIn("blue-box-season-2/ep-8", d)

    def test_no_match_falls_back_to_search_and_retries_after_a_day(self):
        self.add_today(1, "Hotaru no Yomeiri 2", "Firefly Wedding 2")
        d = self.summary(22)
        self.assertIn("[Anikoto](https://anikototv.bz/filter?keyword=Firefly+Wedding+2)", d)
        self.assertNotIn("firefly-wedding-94459", d)           # near miss is not used
        self.assertEqual(len(self.world.anikoto_requests), 2)  # MAL title, then English
        self.run_at(at(22, 15))
        self.assertEqual(len(self.world.anikoto_requests), 2)
        self.world.schedule.append({"mediaId": 100001, "episode": 8, "airingAt": at(23, 19)})
        self.summary(23)
        self.assertEqual(len(self.world.anikoto_requests), 4)

    def test_site_down_falls_back_without_caching_a_miss(self):
        self.add_today(1, "Ao no Hako Season 2", "Blue Box Season 2")
        self.world.anikoto_down = True
        d = self.summary()
        self.assertIn("[Anikoto](https://anikototv.bz/filter?keyword=Blue+Box+Season+2)", d)
        self.assertNotIn("100001", self.state.get("anikoto", {}))
        self.assertEqual(self.run_at(at(22, 9, 20)), 0)

    def test_domain_change_keeps_slugs(self):
        self.add_today(1, "Ao no Hako Season 2", "Blue Box Season 2")
        self.summary(22)
        self.world.schedule.append({"mediaId": 100001, "episode": 8, "airingAt": at(23, 19)})
        d = self.summary(23, ANIKOTO_URL="kotoplay.example")
        self.assertIn("(https://kotoplay.example/watch/blue-box-season-2/ep-8)", d)

    def test_direct_toggle_off(self):
        self.add_today(1, "Ao no Hako Season 2", "Blue Box Season 2")
        d = self.summary(ANIKOTO_DIRECT="false")
        self.assertIn("filter?keyword=", d)
        self.assertEqual(self.world.anikoto_requests, [])

    def test_ptw_alert_links_episode_1(self):
        self.env["PTW_ALERTS"] = "true"
        self.env["DAILY_SUMMARY"] = "false"
        self.world.add("plan_to_watch", 9, "Hotaru no Yomeiri", english="Firefly Wedding",
                       status="NOT_YET_RELEASED", next_ep={"episode": 1, "airingAt": at(23, 1)})
        self.run_at(at(22, 10))
        self.world.media[9].update(status="RELEASING", nextAiringEpisode={"episode": 2, "airingAt": at(30, 1)})
        self.run_at(at(23, 1, 10))
        e = self.world.ptw_posts()[0]["embeds"][0]
        watch = [f for f in e["fields"] if f["name"] == "Watch"][0]["value"]
        self.assertIn("[Anikoto Ep 1](https://anikototv.bz/watch/firefly-wedding-94459/ep-1)", watch)

    def test_norm_title(self):
        n = ramune.norm_title
        self.assertEqual(n("Tensei shitara Ken deshita II"), n("Tensei Shitara Ken Deshita 2nd Season"))
        self.assertEqual(n("Chitose Is in the Ramune Bottle 2nd Cour"), n("Chitose Is in the Ramune Bottle Part 2"))
        self.assertEqual(n("Blue Box Season 2"), n("Blue Box 2nd Season"))
        self.assertNotEqual(n("Blue Box"), n("Blue Box Season 2"))
        self.assertEqual(n("The World&#039;s Finest"), n("The World's Finest"))
        self.assertEqual(n(""), "")

    def test_parser_ignores_sidebar_links(self):
        p = ramune._AnikotoResults()
        p.feed(self.world.fetch_text("https://anikototv.bz/filter?keyword=zzzz", "Anikoto"))
        self.assertEqual(p.items, [])


# =========================================================================== upgrade from the live state

class Upgrade(Base):
    def test_existing_state_upgrades_quietly(self):
        with open(os.path.join(os.path.dirname(ramune.STATE_PATH), "state.json"), encoding="utf-8") as fh:
            self.state = json.load(fh)
        for mal_id, title in self.state["watching_cache"].items():
            self.world.add("watching", int(mal_id), title)
        self.world.add("plan_to_watch", 900, "Released Long Ago", status="FINISHED")
        self.world.add("plan_to_watch", 901, "Airing Now", next_ep={"episode": 6, "airingAt": at(26, 1)})
        self.assertEqual(self.run_at(at(22, 15), UP_NEXT="false"), 0)
        self.assertEqual(self.world.ptw_posts(), [])
        self.assertEqual(self.world.outage_posts(), [])


if __name__ == "__main__":
    unittest.main()
