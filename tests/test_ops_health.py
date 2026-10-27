from datetime import datetime
import unittest
from unittest.mock import patch
import ops_health as m


def run(conclusion="success", date="2026-10-28T08:00:00Z"):
    return {"conclusion": conclusion, "created_at": date, "status": "completed"}


class HealthTests(unittest.TestCase):
    def test_disabled_daily_is_reported_as_stopped_not_delivered(self):
        runs = {k: [run()] for k in m.WORKFLOWS}
        lines, problems = m.summarize(runs, False, datetime(2026, 10, 28, 22, tzinfo=m.JST), set())
        self.assertIn("停止中", lines[0])
        self.assertFalse(any("未配信" in x for x in problems))

    def test_delivery_check_waits_for_scheduling_grace(self):
        self.assertEqual(m.required_editions(datetime(2026, 10, 28, 9)), set())
        self.assertEqual(m.required_editions(datetime(2026, 10, 28, 21)),
                         {"morning", "news_morning"})
        self.assertEqual(len(m.required_editions(datetime(2026, 10, 28, 22))), 4)

    def test_missing_edition_is_detected_when_enabled(self):
        runs = {k: [run()] for k in m.WORKFLOWS}
        _, problems = m.summarize(runs, True, datetime(2026, 10, 28, 22, tzinfo=m.JST),
                                 {"morning", "news_morning"})
        self.assertTrue(any("news_evening" in x for x in problems))

    def test_pending_page_does_not_count_as_delivered(self):
        row = {"icon": {"emoji": "⏳"}, "properties": {"種類": {"select": {"name": "🌅 朝"}}}}
        self.assertEqual(m.completed_editions([row]), set())

    def test_news_editions_are_distinct(self):
        def news(time):
            return {"properties": {"種類": {"select": {"name": "📰 ニュース"}},
                    "名前": {"title": [{"plain_text": "2026-10-28 " + time + " ニュース"}]}}}
        self.assertEqual(m.completed_editions([news("07:05"), news("19:20")]),
                         {"news_morning", "news_evening"})

    def test_monthly_failure_is_not_hidden_by_other_successes(self):
        runs = {k: [run()] for k in m.WORKFLOWS}
        runs["monthly_review.yml"] = [run("failure")]
        _, problems = m.summarize(runs, False, datetime(2026, 10, 28, 12, tzinfo=m.JST), set())
        self.assertTrue(any("monthly_review" in x for x in problems))

    def test_rerun_does_not_create_another_status_page(self):
        now = datetime(2026, 10, 28, 22, tzinfo=m.JST)
        row = {"child_page": {"title": "2026-10-28 稼働点検（実行test）"}}
        with patch.object(m, "api", return_value={"results": [row]}) as api:
            self.assertFalse(m.save_snapshot("unused", "test", now, [], []))
            self.assertEqual(api.call_count, 1)

    def test_failed_lookup_must_not_publish(self):
        with patch.object(m, "api", side_effect=TimeoutError()) as api:
            with self.assertRaises(TimeoutError):
                m.save_snapshot("unused", "test", datetime(2026, 10, 28), [], [])
            self.assertEqual(api.call_count, 1)

