import ast
import math
from pathlib import Path
import unittest
from unittest.mock import Mock
from datetime import datetime, timezone, timedelta

from report_delivery import report_slot, find_existing, publish

KINDS = {"🌅": "🌅 朝", "🌙": "🌙 夜", "📰": "📰 ニュース", "🛒": "🛒 週次"}


def response(body):
    r = Mock()
    r.json.return_value = body
    return r


def row(title, icon="🌅"):
    return {"id": "abc", "url": "https://notion.so/abc", "icon": {"emoji": icon},
            "properties": {"名前": {"title": [{"plain_text": title}]}}}


def extract(*names, **namespace):
    tree = ast.parse(Path("daily_report.py").read_text(encoding="utf-8-sig"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "daily_report.py", "exec"), namespace)
    return namespace


class DeliveryTests(unittest.TestCase):
    def find(self, api, title, icon="🌅", db="db"):
        return find_existing(api, {}, db, "parent", title, icon, KINDS)

    def test_rerun_same_date_edition_ignores_minute(self):
        self.assertEqual(report_slot("2026-10-27 07:00 朝の株レポート", "🌅"),
                         report_slot("2026-10-27 08:14 朝の株レポート", "🌅"))
        self.assertNotEqual(report_slot("2026-10-27 朝", "🌅"),
                            report_slot("2026-10-27 夜", "🌙"))

    def test_news_morning_evening_and_legacy(self):
        self.assertNotEqual(report_slot("2026-10-27 07:00 ニュース", "📰"),
                            report_slot("2026-10-27 19:00 ニュース", "📰"))
        self.assertEqual(report_slot("2026-10-27 ニュース", "📰"),
                         report_slot("2026-10-27 19:00 ニュース", "📰"))

    def test_manual_reload_is_preserved(self):
        api = Mock()
        self.assertIsNone(self.find(api, "2026-10-27 手動", "🔄"))
        api.post.assert_not_called()

    def test_existing_database_report_and_filter(self):
        api = Mock()
        api.post.return_value = response({"results": [row("2026-10-27 07:12 朝の株レポート")]})
        self.assertEqual(self.find(api, "2026-10-27 07:00 朝の株レポート"), "https://notion.so/abc")
        filters = api.post.call_args.kwargs["json"]["filter"]["and"]
        self.assertEqual(filters[0]["date"]["equals"], "2026-10-27")
        self.assertEqual(filters[1]["select"]["equals"], "🌅 朝")

    def test_news_looks_past_other_edition(self):
        api = Mock()
        api.post.side_effect = [
            response({"results": [row("2026-10-27 07:15 ニュース", "📰")],
                      "has_more": True, "next_cursor": "next"}),
            response({"results": [row("2026-10-27 19:21 ニュース", "📰")]})]
        self.assertEqual(self.find(api, "2026-10-27 19:00 ニュース", "📰"), "https://notion.so/abc")
        self.assertEqual(api.post.call_args.kwargs["json"]["start_cursor"], "next")

    def test_query_failure_stops_delivery(self):
        api = Mock()
        api.post.return_value.raise_for_status.side_effect = RuntimeError("403")
        with self.assertRaises(RuntimeError):
            self.find(api, "2026-10-27 朝の株レポート")

    def test_pending_report_stops_rerun(self):
        api = Mock()
        api.post.return_value = response({"results": [row("2026-10-27 朝の株レポート", "⏳")]})
        with self.assertRaises(RuntimeError):
            self.find(api, "2026-10-27 朝の株レポート")

    def test_parent_page_fallback_legacy_title(self):
        api = Mock()
        api.get.return_value = response({"results": [
            {"id": "abc", "type": "child_page",
             "child_page": {"title": "2026-10-27 07:01 朝の株レポート"}}]})
        self.assertEqual(self.find(api, "2026-10-27 07:00 朝の株レポート", db=""),
                         "https://www.notion.so/abc")

    def test_create_timeout_is_not_retried(self):
        api = Mock()
        api.post.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            publish(api, {}, {"icon": {"emoji": "🌅"}}, [])
        self.assertEqual(api.post.call_count, 1)
        api.patch.assert_not_called()

    def test_append_failure_is_not_success_and_icon_stays_pending(self):
        api = Mock()
        api.post.return_value = response({"id": "abc", "url": "url"})
        api.patch.return_value.raise_for_status.side_effect = RuntimeError("append failed")
        with self.assertRaises(RuntimeError):
            publish(api, {}, {"icon": {"emoji": "🌅"}}, [{}] * 101)
        self.assertEqual(api.patch.call_count, 1)
        self.assertEqual(api.post.call_args.kwargs["json"]["icon"]["emoji"], "⏳")

    def test_publish_batches_and_marks_complete(self):
        api = Mock()
        api.post.return_value = response({"id": "abc", "url": "url"})
        blocks = [{}] * 250
        self.assertEqual(publish(api, {}, {"icon": {"emoji": "🌅"}}, blocks), "url")
        self.assertEqual([len(c.kwargs["json"]["children"]) for c in api.patch.call_args_list[:2]],
                         [100, 50])
        self.assertEqual(api.patch.call_args.kwargs["json"], {"icon": {"emoji": "🌅"}})


class Series:
    def __init__(self, values):
        self.values = values
        self.iloc = self
        self.index = [datetime(2026, 10, 26)] * len(values)
    def __len__(self):
        return len(self.values)
    def __getitem__(self, i):
        return self.values[i]
    def dropna(self):
        return Series([v for v in self.values if not math.isnan(v)])
    def max(self):
        return max(self.values)


class PriceTests(unittest.TestCase):
    def stock(self, values, info):
        api = Mock()
        api.Ticker.return_value.info = info
        api.Ticker.return_value.history.return_value = {"Close": Series(values)}
        # history needs dataframe-style len (one column is sufficient here).
        return extract("get_stock_data", yf=api, math=math)["get_stock_data"]("TEST")

    def test_nan_close_and_missing_52week_values(self):
        data = self.stock([100., 110., float("nan")],
                          {"fiftyTwoWeekLow": None, "fiftyTwoWeekHigh": float("nan")})
        self.assertEqual(data["price"], 110.)
        self.assertTrue(math.isfinite(data["position"]))
        self.assertEqual(int(data["position"] * 100), 50)

    def test_all_nan_is_data_error(self):
        self.assertIn("error", self.stock([float("nan")], {}))

    def test_normal_prices_keep_calculation(self):
        data = self.stock([100., 110.], {"fiftyTwoWeekLow": 80., "fiftyTwoWeekHigh": 140.})
        self.assertEqual(data["position"], 0.5)
        self.assertAlmostEqual(data["chg_pct"], 10.)

    def test_infinite_price_is_data_error(self):
        self.assertIn("error", self.stock([float("inf")], {}))


class MainTests(unittest.TestCase):
    def main_namespace(self, mode):
        clock = Mock()
        clock.now.return_value = datetime(2026, 10, 27, 7 if mode == "morning" else 19)
        ns = {name: Mock(return_value="url") for name in (
            "create_morning_page", "create_evening_page", "create_news_page", "create_weekly_page")}
        ns.update(datetime=clock, timezone=timezone, timedelta=timedelta,
                  resolve_mode=Mock(return_value=mode))
        return extract("main", **ns)

    def test_morning_delivers_stock_and_news(self):
        ns = self.main_namespace("morning")
        ns["main"]()
        ns["create_morning_page"].assert_called_once()
        ns["create_news_page"].assert_called_once()

    def test_evening_news_failure_fails_job(self):
        ns = self.main_namespace("evening")
        ns["create_news_page"].side_effect = RuntimeError("news failed")
        with self.assertRaises(RuntimeError):
            ns["main"]()
        ns["create_evening_page"].assert_called_once()

    def test_notion_failure_has_no_csv_fallback(self):
        ns = extract("load_watchlist", load_watchlist_from_notion=Mock(return_value=None))
        with self.assertRaises(RuntimeError):
            ns["load_watchlist"]()

    def test_notion_holdings_remain_authoritative(self):
        expected = ([("TEST", "Test", 1, 100)], [], [])
        ns = extract("load_watchlist", load_watchlist_from_notion=Mock(return_value=expected))
        self.assertEqual(ns["load_watchlist"](), expected)

    def test_source_has_no_hardcoded_holdings(self):
        tree = ast.parse(Path("daily_report.py").read_text(encoding="utf-8-sig"))
        assignments = {target.id for node in tree.body if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Name)}
        self.assertNotIn("_DEFAULT_HOLDINGS", assignments)
        self.assertNotIn("WATCHLIST_FILE", assignments)

    def test_monthly_projection_uses_notion_contribution_even_if_zero(self):
        source = Path("monthly_review.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "monthly" for t in node.targets))
        self.assertIsInstance(assignment.value, ast.Name)
        self.assertEqual(assignment.value.id, "tsumitate_total")


if __name__ == "__main__":
    unittest.main()

