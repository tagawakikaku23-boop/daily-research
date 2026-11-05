import ast
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from news_delivery import JST, news_slot, route
from news_content import article_key, fresh_entry, canonical_url, collect_rss
from test_delivery import extract

class NewsSlotTests(TestCase):
    def test_manual_publication_requires_target_date(self):
        now = datetime(2026, 11, 5, 10, tzinfo=JST)
        with self.assertRaisesRegex(ValueError, "explicit JST"):
            route({"enabled": True}, "workflow_dispatch", "", "morning", "post",
                  "", "refs/heads/main", now, now)

    def test_evening_after_midnight_keeps_previous_day(self):
        created = datetime(2026, 11, 5, 2, 51, tzinfo=JST)
        slot = news_slot("evening", created, created)
        self.assertEqual(slot.isoformat(), "2026-11-04T19:00:00+09:00")

    def test_retry_next_day_keeps_original_key(self):
        created = datetime(2026, 11, 4, 8, tzinfo=JST)
        self.assertEqual(news_slot("morning", created, created),
                         news_slot("morning", created, created + timedelta(days=1)))

    def test_worker_queue_delay_does_not_change_edition(self):
        created = datetime(2026, 11, 4, 20, tzinfo=JST)
        self.assertEqual(news_slot("evening", created, created + timedelta(hours=14)).day, 4)

    def test_distinct_morning_evening(self):
        now = datetime(2026, 11, 4, 20, tzinfo=JST)
        self.assertNotEqual(news_slot("morning", now, now), news_slot("evening", now, now))

    def test_stale_future_and_naive_rejected(self):
        now = datetime(2026, 11, 5, 10, tzinfo=JST)
        for day in ["2026-11-01", "2026-11-06"]:
            with self.assertRaises(ValueError):
                news_slot("morning", now, now, day)
        with self.assertRaises(ValueError):
            news_slot("morning", now.replace(tzinfo=None), now)

    def test_disabled_weekly_push_and_branch_do_not_publish(self):
        now = datetime(2026, 11, 5, 10, tzinfo=JST)
        for enabled, event, schedule, override, ref in [
            (False, "schedule", "0 22 * * *", "", "refs/heads/main"),
            (True, "schedule", "0 22 * * *", "false", "refs/heads/main"),
            (True, "schedule", "30 22 * * 0", "", "refs/heads/main"),
            (True, "push", "", "", "refs/heads/main"),
            (True, "workflow_dispatch", "", "", "refs/heads/test")]:
            self.assertIsNone(route({"enabled": enabled}, event, schedule, "morning",
                                    "post", override, ref, now, now))

    def test_delayed_schedule_and_explicit_retry(self):
        now = datetime(2026, 11, 5, 2, 51, tzinfo=JST)
        args = ({"enabled": True}, "schedule", "0 10 * * *", "", "", "", "refs/heads/main", now, now)
        self.assertEqual(route(*args)[1].day, 4)
        self.assertEqual(route({"enabled": True}, "workflow_dispatch", "", "evening", "post",
                              "", "refs/heads/main", now, now, "2026-11-04")[1].day, 4)

class NewsBoundaryTests(TestCase):
    def test_news_import_assignment_never_calls_portfolio(self):
        tree = ast.parse(Path("daily_report.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Tuple) and any(isinstance(e, ast.Name) and e.id == "HOLDINGS"
                            for e in t.elts) for t in n.targets))
        loader = Mock(side_effect=AssertionError("private source touched"))
        ns = {"os": os, "load_watchlist": loader}
        with patch.dict(os.environ, {"REPORT_COMPONENT": "news"}):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "", "exec"), ns)
        loader.assert_not_called()
        self.assertEqual(ns["HOLDINGS"], [])

    def test_news_main_only_calls_news_with_fixed_slot(self):
        clock = Mock()
        clock.now.return_value = datetime(2026, 11, 5, 2, 51, tzinfo=JST)
        news = Mock()
        ns = extract("main", datetime=clock, timezone=__import__("datetime").timezone,
                     timedelta=timedelta, REPORT_COMPONENT="news", os=os, create_news_page=news)
        with patch.dict(os.environ, {"NEWS_EDITION": "evening", "NEWS_DATE": "2026-11-04"}):
            ns["main"]()
        self.assertEqual(news.call_args.args, ("2026-11-04", "19:00"))
        self.assertIn("2026-11-05", news.call_args.kwargs["retrieved_at"])

    def test_independent_workflow_has_no_stock_dependencies(self):
        text = Path(".github/workflows/daily_report.yml").read_text(encoding="utf-8")
        news_job = text.split("\n  news:")[1]
        self.assertNotIn("needs:", news_job)
        self.assertNotIn("state_sync", news_job)
        self.assertNotIn("WATCHLIST", news_job)
        self.assertIn("REPORT_COMPONENT: news", news_job)
        self.assertEqual(text.count("cron:"), 3)

class NewsArticleTests(TestCase):
    def test_update_time_never_replaces_publication_time(self):
        now = datetime(2026, 11, 5, 10, tzinfo=JST)
        self.assertFalse(fresh_entry({"updated": now.isoformat()}, now))
        self.assertFalse(fresh_entry({"published": (now-timedelta(days=4)).isoformat(),
                                     "updated": now.isoformat()}, now))

    def test_unsafe_urls_are_rejected(self):
        for url in [None, "javascript:alert(1)", "https://user:password@example.com/a", "https://example.com:22/a"]:
            self.assertIsNone(canonical_url(url))

    def test_rss_metadata_and_partial_failure_are_retained(self):
        now = datetime(2026, 11, 5, 10, tzinfo=JST)
        class Feed(dict):
            @property
            def entries(self):
                return self["entries"]
        entries = [{"title": "Economic headline", "published": now.isoformat(),
                    "link": "https://news.google.com/rss/articles/test?utm_source=one", "source": {"title": "Publisher"}},
                   {"title": "Economic headline", "published": now.isoformat(),
                    "link": "https://news.google.com/rss/articles/test?utm_source=two"},
                   {"title": "Unsafe headline", "published": now.isoformat(), "link": "javascript:alert(1)"}]
        parser = Mock()
        parser.parse.return_value = Feed(entries=entries)
        response = Mock(content=b"rss")
        fetch = Mock(side_effect=[TimeoutError(), response])
        items = collect_rss(["https://example.com/fail", "https://example.com/rss"], fetch, parser, now)
        self.assertEqual((len(items), items.completed_feeds, items.failed_feeds), (1, 1, 1))
        title, url = items[0]
        self.assertEqual(title.published_at, now.isoformat())
        self.assertEqual(title.source, "Publisher")
        self.assertIn("原文URL未確認", title.url_kind)
        self.assertNotIn("utm_", url)
        self.assertEqual(fetch.call_args.kwargs["timeout"], (5, 20))
        rendered = extract("bul")["bul"](title, url)
        text = rendered["bulleted_list_item"]["rich_text"][0]["text"]["content"]
        self.assertIn("公開: " + now.isoformat(), text)
        self.assertIn("出典: Publisher", text)

    def test_valid_empty_feed_is_distinct_from_all_failed(self):
        class Feed(dict):
            @property
            def entries(self):
                return []
        parser = Mock()
        parser.parse.return_value = Feed()
        empty = collect_rss(["https://example.com/rss"], Mock(return_value=Mock(content=b"rss")), parser)
        failed = collect_rss(["https://example.com/rss"], Mock(side_effect=TimeoutError()), parser)
        self.assertEqual((empty.completed_feeds, empty.failed_feeds), (1, 0))
        self.assertEqual((failed.completed_feeds, failed.failed_feeds), (0, 1))

    def test_tracking_urls_are_same_article(self):
        self.assertEqual(article_key("a", "https://example.com/a?id=1&utm_source=x#top"),
                         article_key("b", "https://example.com/a?utm_source=y&id=1"))

    def test_old_unknown_future_articles_rejected(self):
        now = datetime(2026, 11, 5, 10, tzinfo=JST)
        self.assertTrue(fresh_entry({"published": now.isoformat()}, now))
        for value in ["", "invalid", (now-timedelta(days=4)).isoformat(),
                      (now+timedelta(hours=1)).isoformat(), "2026-11-05T10:00:00"]:
            self.assertFalse(fresh_entry({"published": value}, now))

class NewsPipelineTests(TestCase):
    def setup_runner(self, items):
        saved = Mock(return_value="saved")
        ns = extract("create_news_page", existing_report=Mock(return_value=None),
                     get_nikkei_news=Mock(return_value=[]), get_niigata_nippo_news=Mock(return_value=[]),
                     NEWS_FEEDS={"one": ["https://example.com"]}, get_rss_news=Mock(return_value=items),
                     generate_news_digest=Mock(return_value="summary"), create_page=saved,
                     os=os, print=Mock(), freshness_note=Mock(return_value={}))
        for name in ["callout_rt", "rt", "h2", "divider", "toggle", "para", "bul"]:
            ns[name] = lambda *args, **kw: {"text": str(args)}
        ns["long_callout"] = lambda *args, **kw: [{"summary": str(args)}]
        return ns, saved

    def test_collect_generate_save_and_deduplicate(self):
        ns, saved = self.setup_runner([("headline one", "https://example.com/a?utm_source=one"),
                                       ("headline one", "https://example.com/a?utm_source=two")])
        self.assertEqual(ns["create_news_page"]("2026-11-04", "19:00", "2026-11-05T02:51+09:00"), "saved")
        self.assertEqual(saved.call_args.args[0], "2026-11-04 19:00 ニュースダイジェスト")
        ns["generate_news_digest"].assert_called_once_with("one: headline one")

    def test_empty_fetch_does_not_publish_or_use_cached_news(self):
        ns, saved = self.setup_runner([])
        with self.assertRaises(RuntimeError):
            ns["create_news_page"]("2026-11-04", "19:00")
        saved.assert_not_called()

    def test_existing_report_skips_collection_and_write(self):
        ns, saved = self.setup_runner([])
        ns["existing_report"].return_value = "existing"
        self.assertEqual(ns["create_news_page"]("2026-11-04", "19:00"), "existing")
        ns["get_rss_news"].assert_not_called()
        saved.assert_not_called()

