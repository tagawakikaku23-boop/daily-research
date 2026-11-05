import copy
import io
import json
import unittest
from datetime import datetime, timedelta
from urllib.error import HTTPError
from unittest.mock import patch
from research_core import *
from research_store import Store
from research_briefing import news_report, investment_report, quote_text, research_priority, portfolio_rows

NOW = datetime(2026, 11, 4, 8, tzinfo=JST)
DS = "00000000-0000-0000-0000-000000000001"
PAGE = "00000000-0000-0000-0000-000000000002"


class CoreTests(unittest.TestCase):
    def test_superseded_news_publisher_cannot_write_even_on_enabled_main(self):
        from research_run import run
        with patch.dict("os.environ", {"GITHUB_REF": "refs/heads/main", "RESEARCH_PUBLISH_ENABLED": "true"}):
            with patch("research_run.Store") as client:
                with self.assertRaisesRegex(ValueError, "PR #7"):
                    run("news", NOW, "2026-11-04", write=True)
                client.assert_not_called()

    def test_publication_requires_date_before_any_api_access(self):
        from research_run import run
        with patch.dict("os.environ", {"GITHUB_REF": "refs/heads/main", "RESEARCH_PUBLISH_ENABLED": "true"}):
            with patch("research_run.Store") as client:
                with self.assertRaisesRegex(ValueError, "explicit report date"):
                    run("news", NOW, write=True)
                client.assert_not_called()

    def test_degraded_existing_report_does_not_become_success(self):
        from research_run import run
        from research_briefing import title_key
        _, key = title_key("news", scheduled_slot("news", NOW))
        blocks = complete_blocks([block("partial")], {"key": key, "degraded": True})
        with patch.dict("os.environ", {"NOTION_API_KEY": "fake"}):
            with patch("research_run.Store") as client:
                client.return_value.find.return_value = {"id": PAGE}
                client.return_value.children.return_value = blocks
                with self.assertRaisesRegex(RuntimeError, "degraded"):
                    run("news", NOW)
                client.return_value.save.assert_not_called()

    def test_category_mapping_is_independent_of_legacy_dictionary_order(self):
        from research_sources import collect_news, FEED_CATEGORIES
        feeds = {key: [key] for key in reversed(FEED_CATEGORIES)}
        entry = self.entry()
        with patch("research_sources.legacy_constant", return_value=feeds):
            with patch("research_sources.rss", side_effect=lambda url, *_: [dict(entry, link="https://example.org/" + str(list(feeds).index(url)))] if url in feeds else []):
                articles, _ = collect_news(NOW, session=object(), parser=object())
        self.assertEqual({a["category"] for a in articles}, set(FEED_CATEGORIES.values()))

    def test_investment_news_uses_history_and_deduplicates_within_report(self):
        a = article(self.entry(), "投資ニュース", "source", NOW)
        page = {"properties": {"コード": {"rich_text": [{"plain_text": "TEST"}]},
                               "銘柄名": {"title": [{"plain_text": "Example"}]},
                               "区分": {"select": {"name": "保有"}}}}
        def quote(*_):
            return {"error": "取得失敗"}
        def news(*_):
            return [a, a]
        args = (scheduled_slot("investment", NOW), NOW, [page], [], [], [])
        blocks = investment_report(*args, [], quote=quote, news=news)[2]
        self.assertEqual(read_manifest(blocks)["article_ids"], [a["id"]])
        blocks = investment_report(*args, [{"article_ids": [a["id"]]}], quote=quote, news=news)[2]
        self.assertEqual(read_manifest(blocks)["article_ids"], [])

    def test_news_cross_midnight_maps_previous_scheduled_day(self):
        self.assertEqual(str(scheduled_slot("news", NOW.replace(hour=2)).date()), "2026-11-03")

    def test_investment_monday_is_utc_sunday_and_weekend_no_fabricated_run(self):
        monday = NOW.replace(day=2, hour=8)
        self.assertEqual(scheduled_slot("investment", monday).astimezone(timezone.utc).weekday(), 6)
        self.assertEqual(scheduled_slot("investment", NOW.replace(day=7)).weekday(), 4)

    def test_explicit_date_window(self):
        with self.assertRaises(ValueError):
            scheduled_slot("news", NOW, "2026-11-01")
        with self.assertRaises(ValueError):
            scheduled_slot("news", NOW, "2026-11-05")

    def entry(self, **kw):
        return dict(title="Economic release", link="https://example.org/a?utm_source=x&item=1",
                    published=NOW.isoformat(), summary="<p>Fact</p>", **kw)

    def test_url_tracking_and_fragment(self):
        self.assertEqual(canonical_url("https://example.org/a?utm_source=x&item=1#x"), "https://example.org/a?item=1")

    def test_bad_url(self):
        for url in ["javascript:alert(1)", "https://user:pass@example.org", "https://example.org:22/a"]:
            self.assertIsNone(canonical_url(url))

    def test_old_future_naive_and_undated_articles_rejected(self):
        for date in [None, "", "2026-11-04", (NOW+timedelta(hours=1)).isoformat(),
                     (NOW-timedelta(days=4)).isoformat()]:
            e = self.entry()
            e["published"] = date
            self.assertIsNone(article(e, "金融", "source", NOW))

    def test_updated_does_not_replace_publication_date(self):
        e = self.entry()
        del e["published"]
        e["updated"] = NOW.isoformat()
        self.assertIsNone(article(e, "金融", "source", NOW))

    def test_aggregator_is_not_original(self):
        e = self.entry()
        e["link"] = "https://news.google.com/rss/articles/foo"
        a = article(e, "金融", "source", NOW)
        self.assertIsNone(a["original_url"])
        self.assertIn("原文URL未確認", a["url_kind"])

    def test_tips_do_not_repeat(self):
        tip = choose_tip([], NOW)
        next_tip = choose_tip([{"tip_id": tip["id"], "retrieved_at": NOW.isoformat()}], NOW)
        self.assertNotEqual(tip["id"], next_tip["id"])

    def test_manifest_detects_changed_body(self):
        body = complete_blocks([block("hello")], {"key": "test"})
        self.assertEqual(read_manifest(body)["key"], "test")
        body[0] = block("changed")
        with self.assertRaises(ValueError):
            read_manifest(body)

    def test_atomic_limit(self):
        with self.assertRaises(ValueError):
            complete_blocks([block("a")] * 100, {"key": "k"})

    def test_reuses_constants_without_import_credentials(self):
        self.assertTrue(legacy_constant("NEWS_FEEDS"))
        self.assertTrue(legacy_constant("DISCOVERY_POOL"))

    def test_stale_prices_not_published_as_current(self):
        d = {"price": 100, "session_date": "2026-01-01"}
        self.assertIn("掲載しません", quote_text(d, NOW))
        self.assertIsNone(research_priority(d, NOW))

    def test_missing_price_timestamp_stays_session_date(self):
        d = {"price": 100, "session_date": "2026-11-02", "retrieved_at": NOW.isoformat(),
             "provider_note": "delayed", "source_url": "https://example.org"}
        self.assertIn("時刻不明", quote_text(d, NOW))

    def test_no_source_schema_assumptions(self):
        rows = portfolio_rows([{"properties": {"コード": {"rich_text": [{"plain_text": "TEST"}]},
                                               "区分": {"select": {"name": "保有"}}}}])
        self.assertIsNone(rows[0]["holdings_asof"])

    def test_news_builder_does_not_touch_portfolio(self):
        a = article(self.entry(), "金融", "source", NOW)
        title, key, blocks = news_report(scheduled_slot("news", NOW), NOW, [a], [], {})
        self.assertEqual(read_manifest(blocks)["article_ids"], [a["id"]])

    def test_investment_missing_data_and_source_failures_explicit(self):
        def quote(t, now):
            return {"error": "株価・指標取得失敗"}
        _, _, blocks = investment_report(scheduled_slot("investment", NOW), NOW, [], [], [], [], [], quote=quote)
        text = " ".join(block_text(b) for b in blocks)
        self.assertIn("算定保留", text)
        self.assertIn("取得失敗", text)
        self.assertIn("6. 今日の投資Tips", text)
        self.assertIn("買い時とは判定しません", text)

    def test_publish_disabled_on_branch(self):
        from research_run import run
        with patch.dict("os.environ", {"GITHUB_REF": "refs/heads/test", "RESEARCH_PUBLISH_ENABLED": "true"}):
            with self.assertRaises(ValueError):
                run("news", NOW, write=True)


class FakeNotion:
    def __init__(self):
        self.pages = []
        self.blocks = {}
        self.calls = []
        self.corrupt = False
        self.timeout_after_create = False

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path.endswith("/query"):
            title = (body.get("filter") or {}).get("title", {}).get("equals")
            rows = self.pages
            if title:
                rows = [p for p in rows if p["properties"]["名前"]["title"][0]["text"]["content"] == title]
            return {"results": rows, "has_more": False}
        if method == "GET" and path.startswith("data_sources/"):
            return {"properties": {"名前": {"type": "title"}, "日付": {"type": "date"}}}
        if method == "POST" and path == "pages":
            p = {"id": PAGE, **copy.deepcopy(body)}
            self.pages.append(p)
            self.blocks[PAGE] = copy.deepcopy(body["children"])
            if self.timeout_after_create:
                raise TimeoutError()
            return p
        if path.startswith("blocks/"):
            blocks = copy.deepcopy(self.blocks[PAGE])
            if self.corrupt:
                blocks[0] = block("corrupted")
            return {"results": blocks, "has_more": False}
        if path.startswith("pages/"):
            if method == "PATCH":
                self.pages[0].update(body)
            return copy.deepcopy(self.pages[0])
        raise AssertionError(path)


class StoreTests(unittest.TestCase):
    def test_category_properties_saved_without_creating_schema_options(self):
        schema = {"分野": {"type": "multi_select", "multi_select": {"options": [{"name": "新潟"}]}}}
        self.blocks = complete_blocks([block("fact")], {"key": "k", "stream": "news", "categories": ["新潟"]})
        with patch.object(self.store, "check_destination", return_value=schema):
            self.save()
        self.assertEqual(self.api.pages[0]["properties"]["分野"], {"multi_select": [{"name": "新潟"}]})

    def test_unknown_category_blocks_creation(self):
        self.blocks = complete_blocks([block("fact")], {"key": "k", "categories": ["unknown"]})
        with patch.object(self.store, "check_destination", return_value={"分野": {"type": "multi_select", "multi_select": {"options": []}}}):
            with self.assertRaises(ValueError):
                self.save()
        self.assertFalse(self.api.pages)

    def setUp(self):
        self.api = FakeNotion()
        self.store = Store("fake", DS, transport=self.api, sleep=lambda _: None)
        self.blocks = complete_blocks([block("fact")], {"key": "k", "stream": "news"})

    def save(self):
        return self.store.save("t", "k", "2026-11-04", self.blocks, "news")

    def test_save_readback_and_rerun(self):
        self.assertEqual(self.save(), "created")
        self.assertEqual(self.save(), "already_exists")
        self.assertEqual(len(self.api.pages), 1)

    def test_create_timeout_reconciled_without_duplicate(self):
        self.api.timeout_after_create = True
        with self.assertRaises(RuntimeError):
            self.save()
        self.api.timeout_after_create = False
        self.assertEqual(self.save(), "already_exists")
        self.assertEqual(len(self.api.pages), 1)

    def test_corrupt_readback_never_marks_complete(self):
        self.api.corrupt = True
        with self.assertRaises(RuntimeError):
            self.save()
        self.assertEqual(self.api.pages[0]["icon"]["emoji"], "⏳")

    def test_pending_corrupt_prevents_republication(self):
        self.api.corrupt = True
        with self.assertRaises(RuntimeError):
            self.save()
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(len(self.api.pages), 1)

    def test_query_failure_prevents_post(self):
        def fail(*_):
            raise TimeoutError()
        self.store.transport = fail
        with self.assertRaises(RuntimeError):
            self.save()
        self.assertFalse(self.api.pages)

    def test_source_write_forbidden(self):
        with self.assertRaises(ValueError):
            self.store.request("PATCH", "pages/" + PAGE, {"properties": {}})
        with self.assertRaises(ValueError):
            self.store.request("POST", "pages", {"parent": {"data_source_id": PAGE}})

    def test_retry_after_read_only(self):
        calls, sleeps = [], []
        def transport(*args):
            calls.append(args)
            if len(calls) == 1:
                raise HTTPError("x", 429, "limited", {"Retry-After": "2"}, io.BytesIO(b"rate_limited"))
            return {"results": [], "has_more": False}
        client = Store("fake", DS, transport=transport, sleep=sleeps.append)
        self.assertEqual(client.query(DS), [])
        self.assertIn(2, sleeps)

    def test_blocked_no_retry(self):
        calls = []
        def transport(*args):
            calls.append(args)
            raise HTTPError("x", 429, "blocked", {}, io.BytesIO(b"public_api_request_blocked"))
        self.store.transport = transport
        with self.assertRaises(RuntimeError):
            self.store.query(DS)
        self.assertEqual(len(calls), 1)

    def test_pagination_loop_rejected(self):
        self.store.transport = lambda *_: {"results": [], "has_more": True, "next_cursor": "same"}
        with self.assertRaises(RuntimeError):
            self.store.query(DS)


if __name__ == "__main__":
    unittest.main()



