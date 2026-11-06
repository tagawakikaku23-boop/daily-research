from unittest import TestCase
from unittest.mock import Mock
from test_delivery import extract, response, row, KINDS
import test_news_resilience as pipeline
from datetime import datetime, timezone
from news_content import NewsHeadline, RssItems
from report_delivery import find_existing, publish
from ops_health import completed_editions


class PartialNewsTests(TestCase):
    def runner(self, failed=0):
        now = datetime.now(timezone.utc)
        link = "https://example.com/story"
        headline = NewsHeadline("Economic headline: GDP", {"link": link, "published": now.isoformat()}, link, now)
        return pipeline.NewsPipelineTests().setup_runner(RssItems([(headline, link)], 1, failed))

    def test_partial_rss_is_saved_once_and_execution_fails(self):
        ns, saved = self.runner(failed=1)
        with self.assertRaisesRegex(RuntimeError, "部分失敗"):
            ns["create_news_page"]("2026-11-05", "07:00")
        saved.assert_called_once()
        self.assertEqual(saved.call_args.kwargs["completion_icon"], "⚠️")
        self.assertIn("部分失敗", str(saved.call_args.args[1]))

    def test_ai_failure_keeps_headlines_without_provider_details(self):
        for digest in ["", None, "ダイジェスト生成失敗: sensitive provider response"]:
            ns, saved = self.runner()
            ns["generate_news_digest"].return_value = digest
            with self.assertRaises(RuntimeError):
                ns["create_news_page"]("2026-11-05", "07:00")
            self.assertEqual(saved.call_args.kwargs["completion_icon"], "⚠️")
            self.assertNotIn("sensitive provider response", str(saved.call_args))
            self.assertIn("Economic headline", str(saved.call_args))

    def test_healthy_news_keeps_normal_completion(self):
        ns, saved = self.runner()
        self.assertEqual(ns["create_news_page"]("2026-11-05", "07:00"), "saved")
        self.assertNotIn("completion_icon", saved.call_args.kwargs)

    def test_partial_existing_page_blocks_rerun(self):
        api = Mock()
        api.post.return_value = response({"results": [row("2026-11-05 07:00 ニュース", "⚠️")]})
        with self.assertRaisesRegex(RuntimeError, "部分失敗"):
            find_existing(api, {}, "db", "parent", "2026-11-05 07:00 ニュース", "📰", KINDS)
        api.patch.assert_not_called()

    def test_partial_news_not_counted_but_stock_behavior_preserved(self):
        def page(kind, icon):
            return {"icon": {"emoji": icon}, "properties": {
                "種類": {"select": {"name": kind}},
                "名前": {"title": [{"plain_text": "2026-11-05 07:00 ニュース"}]}}}
        self.assertEqual(completed_editions([page("📰 ニュース", "⚠️")]), set())
        self.assertEqual(completed_editions([page("📰 ニュース", "📰")]), {"news_morning"})
        self.assertEqual(completed_editions([page("🌅 朝", "⚠️")]), {"morning"})

    def test_publish_marks_partial_only_after_append(self):
        api = Mock()
        api.post.return_value = response({"id": "abc", "url": "saved"})
        publish(api, {}, {"icon": {"type": "emoji", "emoji": "📰"}}, [{}]*101,
                completion_icon="⚠️")
        self.assertEqual(api.post.call_args.kwargs["json"]["icon"]["emoji"], "⏳")
        self.assertIn("children", api.patch.call_args_list[0].kwargs["json"])
        self.assertEqual(api.patch.call_args.kwargs["json"]["icon"]["emoji"], "⚠️")

    def test_ai_provider_exception_is_sanitized(self):
        ns = extract("generate_news_digest", Groq=Mock(side_effect=RuntimeError("private-body")),
                     GROQ_API_KEY="unused")
        result = ns["generate_news_digest"]("public headline")
        self.assertTrue(result.startswith("ダイジェスト生成失敗"))
        self.assertNotIn("private-body", result)
