import ast
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock
from investment_analysis import (annual_dividend, candidate, company_matches, convert_jpy,
                                 dashboard, fresh, gain_percent, require_fx, weekly_key, yield_percent)
from investment_home import NotionSandbox, demo_rows, preview, render

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def extract(file, names, **namespace):
    tree = ast.parse(Path(file).read_text(encoding="utf-8-sig"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, "exec"), namespace)
    return namespace


class FinancialTests(TestCase):
    def test_fx_missing_or_invalid_never_becomes_one(self):
        for value in [None, 0, -1, float('nan'), float('inf'), True, "150"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                require_fx(value)

    def test_jpy_does_not_need_fx(self):
        self.assertEqual(annual_dividend(10, 40, 'JPY'), 400)

    def test_usd_cash_dividend(self):
        self.assertEqual(annual_dividend(10, 2, 'USD', 150), 3000)

    def test_unknown_currency_fails(self):
        with self.assertRaises(ValueError):
            convert_jpy(100, 'EUR', 150)

    def test_zero_is_distinct_from_missing(self):
        self.assertEqual(annual_dividend(10, 0, 'JPY'), 0)
        with self.assertRaises(ValueError):
            annual_dividend(10, None, 'JPY')

    def test_negative_and_nonfinite_shares_fail(self):
        for value in [-1, float('nan'), True, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                annual_dividend(value, 40, 'JPY')

    def test_yield_derived_from_cash_not_ambiguous_unit(self):
        self.assertEqual(yield_percent(40, 1000), 4)
        self.assertIsNone(yield_percent(None, 1000))
        self.assertIsNone(yield_percent(400, 1000))

    def test_gain_uses_same_currency(self):
        self.assertAlmostEqual(gain_percent(100, 'USD', 12000, 'JPY', 150), 25)
        self.assertIsNone(gain_percent(100, 'USD', 12000, 'JPY'))
        self.assertIsNone(gain_percent(100, 'JPY', 0, 'JPY'))

    def test_incomplete_portfolio_has_no_false_total_or_progress(self):
        rows = demo_rows(NOW)
        rows[1].update(kind='holding', shares=10, annual_dps=None)
        result = dashboard(rows, 1000, NOW)
        self.assertIsNone(result['annual_dividend_jpy'])
        self.assertIsNone(result['goal_progress_pct'])
        self.assertEqual(result['confirmed_subtotal_jpy'], 400)
        self.assertEqual(result['coverage'], '1/2')

    def test_goal_missing_and_empty_holdings(self):
        self.assertIsNone(dashboard(demo_rows(NOW), None, NOW)['goal_progress_pct'])
        self.assertIsNone(dashboard([], 1000, NOW)['annual_dividend_jpy'])

    def test_stale_fx_and_future_dates_rejected(self):
        row = demo_rows(NOW)[0]
        row['dividend_currency'] = 'USD'
        result = dashboard([row], 1000, NOW, 150, '2026-10-01T12:00:00+00:00')
        self.assertIsNone(result['annual_dividend_jpy'])
        self.assertFalse(fresh('2026-10-30T12:00:00+00:00', NOW, 7))
        self.assertFalse(fresh('2026-10-29', NOW, 7))

    def test_no_buy_for_missing_financial_fields(self):
        for field in ['payout_ratio_pct', 'net_income', 'operating_cf', 'free_cf', 'dividend_cut_5y', 'sector_room']:
            row = demo_rows(NOW)[0]
            row[field] = None
            with self.subTest(field=field):
                self.assertFalse(candidate(row, NOW)['eligible'])

    def test_no_buy_for_risk_or_overpriced(self):
        for field, value in [('free_cf', -1), ('net_income', 0), ('dividend_cut_5y', True),
                             ('payout_ratio_pct', 90), ('sector_room', False), ('max_price', 900),
                             ('asset_type', 'ETF'), ('price_asof', '2020-01-01T00:00:00+00:00')]:
            row = demo_rows(NOW)[0]
            row[field] = value
            with self.subTest(field=field):
                self.assertFalse(candidate(row, NOW)['eligible'])

    def test_top_three_diversifies_and_never_fills_unknowns(self):
        rows = demo_rows(NOW)
        rows[1]['sector'] = rows[0]['sector']
        result = dashboard(rows, 1000, NOW)
        self.assertEqual(len(result['top3']), 2)
        self.assertEqual(result['goal_progress_pct'], 40)

    def test_iso_year_and_jst_boundary(self):
        self.assertEqual(weekly_key(datetime(2021, 1, 1, tzinfo=timezone.utc)), 'investment-home:2020-W53')
        self.assertEqual(weekly_key(datetime(2026, 11, 1, 15, tzinfo=timezone.utc)), 'investment-home:2026-W45')
        with self.assertRaises(ValueError):
            weekly_key(datetime(2026, 1, 1))


class NewsTests(TestCase):
    def test_explicit_company_and_ascii_boundaries(self):
        self.assertTrue(company_matches('ACME raises dividends', ['ACME']))
        self.assertFalse(company_matches('MACMEX raises dividends', ['ACME']))
        self.assertFalse(company_matches('XX raises dividends', ['XX']))
        self.assertTrue(company_matches('架空工業の決算発表', ['架空工業']))
        self.assertFalse(company_matches('別企業が減配', ['架空工業']))

    def test_legacy_news_filters_before_classification(self):
        feed = Mock()
        feed.parse.return_value.entries = [dict(title='別企業が減配', link='https://example.com'),
                                          dict(title='架空工業が増配', link='https://example.com/a')]
        classifier = Mock(return_value=([], ['増配']))
        ns = extract('daily_report.py', {'get_ticker_news'}, feedparser=feed,
                     SPORTS_HEAVY=[], is_noise_news=lambda t: False, classify_news=classifier)
        items, risks, goods = ns['get_ticker_news']('架空工業')
        self.assertEqual(len(items), 1)
        classifier.assert_called_once_with('架空工業が増配')

    def test_legacy_52week_low_is_not_a_buy(self):
        ns = extract('daily_report.py', {'trade_signal'})
        self.assertIn('判断保留', ns['trade_signal'](.01, 10))
        self.assertIn('判断保留', ns['trade_signal'](.01, 10, risk_flags=['減配']))


class SandboxTests(TestCase):
    parent = '408fc83b-0e47-81da-b814-e8e4f351dbc9'

    def test_default_demo_contains_only_synthetic_data(self):
        result = preview(NOW)
        self.assertTrue(result['synthetic'])
        self.assertIn('架空', render(result))
        self.assertEqual(len(result['top3']), 3)

    def test_other_page_is_rejected(self):
        with self.assertRaises(ValueError):
            NotionSandbox('test', 'production-page')

    def test_existing_week_does_not_write(self):
        result = preview(NOW)
        api = Mock(return_value={'results': [{'child_page': {'title': '検証週次 ' + result['key']}}]})
        self.assertEqual(NotionSandbox('test', self.parent, api).save(result), 'already_exists')
        api.assert_called_once()

    def test_pagination_and_readback(self):
        api = Mock(side_effect=[{'results': [], 'has_more': True, 'next_cursor': 'next'},
                                {'results': []}, {'id': 'new'}, {'id': 'new'}])
        self.assertEqual(NotionSandbox('test', self.parent, api).save(preview(NOW)), 'created')
        self.assertIn('start_cursor=next', api.call_args_list[1].args[1])
        self.assertEqual(api.call_args_list[-1].args, ('GET', 'pages/new'))

    def test_query_failure_prevents_post(self):
        api = Mock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            NotionSandbox('test', self.parent, api).save(preview(NOW))
        api.assert_called_once()

    def test_post_timeout_is_not_retried(self):
        api = Mock(side_effect=[{'results': []}, TimeoutError])
        with self.assertRaises(TimeoutError):
            NotionSandbox('test', self.parent, api).save(preview(NOW))
        self.assertEqual(api.call_count, 2)

    def test_non_synthetic_input_is_rejected(self):
        api = Mock()
        with self.assertRaises(ValueError):
            NotionSandbox('test', self.parent, api).save({'synthetic': False})
        api.assert_not_called()


class MonthlySafetyTests(TestCase):
    def test_stock_failure_still_attempts_independent_news(self):
        for mode in ['morning', 'evening']:
            clock = Mock()
            clock.now.return_value = NOW
            news = Mock(return_value='synthetic-url')
            stock = Mock(side_effect=ValueError('missing FX'))
            ns = extract('daily_report.py', {'main'}, datetime=clock, timezone=timezone,
                         timedelta=timedelta, resolve_mode=lambda hour: mode,
                         create_morning_page=stock, create_evening_page=stock, create_news_page=news)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                ns['main']()
            news.assert_called_once()

    def test_later_invalid_fund_cannot_leave_earlier_contribution_written(self):
        module = Mock()
        module.get_usdjpy_rate.return_value = 150
        clock = Mock()
        clock.now.return_value = NOW
        api = Mock()
        def fund(name, base):
            return {'id': name, 'properties': {
                '名称': {'title': [{'plain_text': name}]},
                '区分': {'select': {'name': '投信'}},
                '口数': {'number': 10000}, '基準価額': {'number': base},
                '連動指数': {'select': {'name': '日経225'}},
                '指数基準値': {'number': 100}, '月額積立': {'number': 100},
                '最終積立反映月': {'rich_text': []}}}
        ns = extract('monthly_review.py', {'main', 'ptext', 'pnum', 'psel'},
                     m=module, datetime=clock, JST=timezone.utc, requests=api,
                     PROXY={'日経225': 'TEST'}, latest_close=Mock(return_value=100),
                     stocks_value=Mock(return_value=(1000, [])), MASTER_DB='master',
                     query_all=Mock(return_value=[fund('valid', 10000), fund('invalid', None)]))
        with self.assertRaises(ValueError):
            ns['main']()
        api.patch.assert_not_called()
        api.post.assert_not_called()

    def test_fx_failure_occurs_before_any_master_or_history_mutation(self):
        module = Mock()
        module.get_usdjpy_rate.return_value = None
        clock = Mock()
        clock.now.return_value = NOW
        api = Mock()
        ns = extract('monthly_review.py', {'main'}, m=module, datetime=clock, JST=timezone.utc, requests=api, PROXY={})
        with self.assertRaisesRegex(ValueError, '為替が未確認'):
            ns['main']()
        api.patch.assert_not_called()
        api.post.assert_not_called()

    def test_partial_stock_valuation_does_not_write(self):
        module = Mock()
        module.get_usdjpy_rate.return_value = 150
        clock = Mock()
        clock.now.return_value = NOW
        api = Mock()
        ns = extract('monthly_review.py', {'main'}, m=module, datetime=clock, JST=timezone.utc,
                     requests=api, PROXY={}, stocks_value=Mock(return_value=(1000, ['missing'])))
        with self.assertRaises(ValueError):
            ns['main']()
        api.patch.assert_not_called()
        api.post.assert_not_called()
