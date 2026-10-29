import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.error import HTTPError
from unittest import TestCase
from unittest.mock import Mock, patch
from investment_analysis import dashboard, news_identity
from investment_home import demo_rows, preview
from investment_notion import (SOURCE, WATCH, HOME, DECISIONS, LEARNING, NotionClient,
                              adapt_row, save_weekly_logs)
from test_investment import extract

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


class AdapterTests(TestCase):
    def test_readonly_cli_never_prints_portfolio_or_goal(self):
        import investment_home
        client = Mock()
        rows = demo_rows(NOW)
        rows[0]['name'] = 'PRIVATE_PORTFOLIO_MARKER'
        client.read_portfolio.return_value = rows
        client.read_goal.return_value = 1234
        output = io.StringIO()
        with patch('sys.argv', ['investment_home.py', '--read-notion']), \
             patch.dict('os.environ', {'NOTION_API_KEY': 'fake'}, clear=True), \
             patch('investment_notion.NotionClient', return_value=client), redirect_stdout(output):
            investment_home.main()
        self.assertNotIn('PRIVATE_PORTFOLIO_MARKER', output.getvalue())
        self.assertNotIn('1234', output.getvalue())
        client.save_log.assert_not_called()

    def test_readonly_cli_rejects_combined_write_flags(self):
        import investment_home
        with patch('sys.argv', ['investment_home.py', '--read-notion', '--write-sandbox']), self.assertRaises(ValueError):
            investment_home.main()

    def test_private_goal_read_without_local_amount_defaults(self):
        import json
        goal = {'annual_goal_jpy': 1000, 'currency': 'JPY', 'tax_basis': 'before_tax'}
        block = {'code': {'language': 'json', 'rich_text': [{'plain_text': json.dumps(goal)}]}}
        api = Mock(side_effect=[{'parent': {'page_id': HOME}}, {'results': [block]}])
        self.assertEqual(NotionClient('test', api, sleep=Mock()).read_goal(), 1000)
        self.assertTrue(all(call.args[0] == 'GET' for call in api.call_args_list))

    def test_ambiguous_private_settings_rejected(self):
        api = Mock(side_effect=[{'parent': {'page_id': HOME}}, {'results': []}])
        with self.assertRaises(ValueError):
            NotionClient('test', api, sleep=Mock()).read_goal()

    def test_old_schema_preserves_unknown_and_fractional_shares(self):
        row = adapt_row({'id': 'example', 'properties': {
            '銘柄名': {'title': [{'plain_text': '架空A'}]}, '区分': {'select': {'name': '保有'}},
            '株数': {'number': .5}, '配当利回り%': {'number': 5}, '現在値': {'number': 1000}}})
        self.assertEqual(row['shares'], .5)
        self.assertEqual(row['kind'], 'holding')
        for key in ['annual_dps', 'dividend_currency', 'price_asof', 'sector_room']:
            self.assertIsNone(row[key])
        self.assertIsNone(dashboard([row], None, NOW)['annual_dividend_jpy'])

    def test_does_not_invent_timestamp_from_update_text(self):
        row = adapt_row({'properties': {'更新時刻': {'rich_text': [{'plain_text': '2026-10-29 12:00'}]}}})
        self.assertIsNone(row['price_asof'])

    def test_property_type_mismatch_is_unknown(self):
        row = adapt_row({'properties': {'株数': {'type': 'rich_text', 'rich_text': [{'plain_text': '100'}]}}})
        self.assertIsNone(row['shares'])

    def test_read_portfolio_only_queries_source(self):
        api = Mock(side_effect=[{'results': [], 'has_more': True, 'next_cursor': 'next'},
                                {'results': [{'properties': {}}, {'archived': True}]}])
        client = NotionClient('test', api, sleep=Mock())
        self.assertEqual(len(client.read_portfolio()), 1)
        self.assertTrue(all(c.args[0] == 'POST' and c.args[1] == f'data_sources/{SOURCE}/query' for c in api.call_args_list))
        self.assertEqual(api.call_args_list[1].args[2]['start_cursor'], 'next')

    def test_broken_pagination_fails_instead_of_partial_total(self):
        api = Mock(return_value={'results': [], 'has_more': True, 'next_cursor': 'same'})
        with self.assertRaises(RuntimeError):
            NotionClient('test', api, sleep=Mock()).read_portfolio()
        self.assertEqual(api.call_count, 2)


class TransportTests(TestCase):
    def error(self, status, retry='2', body=b'{}'):
        return HTTPError('https://api.notion.com/v1/test', status, 'test', {'Retry-After': retry}, io.BytesIO(body))

    def test_rate_limit_honors_retry_after(self):
        api = Mock(side_effect=[self.error(429, '3'), {'results': []}])
        sleep = Mock()
        self.assertEqual(NotionClient('test', api, sleep=sleep).read_portfolio(), [])
        self.assertIn(((3,), {}), sleep.call_args_list)

    def test_long_server_window_does_not_retry_early(self):
        api = Mock(side_effect=self.error(429, '120'))
        with self.assertRaisesRegex(RuntimeError, 'deferred'):
            NotionClient('test', api, sleep=Mock()).read_portfolio()
        api.assert_called_once()

    def test_blocked_connection_is_not_retried(self):
        api = Mock(side_effect=self.error(429, '1', b'{"rate_limit_reason":"public_api_request_blocked"}'))
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            NotionClient('test', api, sleep=Mock()).read_portfolio()
        api.assert_called_once()

    def test_server_failure_on_query_can_retry(self):
        api = Mock(side_effect=[self.error(503), {'results': []}])
        self.assertEqual(NotionClient('test', api, sleep=Mock()).read_portfolio(), [])

    def test_page_creation_is_not_retried(self):
        for error in [self.error(503), TimeoutError('secret private data')]:
            api = Mock(side_effect=error)
            with self.subTest(error=type(error)), self.assertRaises((RuntimeError, TimeoutError)):
                NotionClient('test', api, sleep=Mock()).request('POST', 'pages', {'parent': {'page_id': HOME}})
            api.assert_called_once()


class LogTests(TestCase):
    def test_direct_mutation_of_production_is_rejected(self):
        api = Mock()
        client = NotionClient('test', api, sleep=Mock())
        for method, path, body in [('PATCH', 'pages/' + SOURCE, {}),
                                    ('POST', 'pages', {'parent': {'data_source_id': SOURCE}}),
                                    ('DELETE', 'data_sources/' + SOURCE, None)]:
            with self.subTest(method=method), self.assertRaises(ValueError):
                client.request(method, path, body, read_only=True)
        api.assert_not_called()

    def client(self, tail):
        meta = {'parent': {'database_id': WATCH}}
        container = {'parent': {'page_id': HOME}}
        api = Mock(side_effect=[meta, container, *tail])
        return NotionClient('test', api, sleep=Mock()), api

    def test_production_log_destination_rejected(self):
        api = Mock()
        with self.assertRaises(ValueError):
            NotionClient('test', api, sleep=Mock()).save_log(SOURCE, 'key', {})
        api.assert_not_called()

    def test_moved_database_rejected(self):
        api = Mock(side_effect=[{'parent': {'database_id': WATCH}}, {'parent': {'page_id': SOURCE}}])
        with self.assertRaises(ValueError):
            NotionClient('test', api, sleep=Mock()).save_log(DECISIONS, 'key', {})
        self.assertEqual(api.call_count, 2)

    def test_existing_week_is_skipped(self):
        client, api = self.client([{'results': [{'id': 'existing'}]}])
        self.assertEqual(client.save_log(DECISIONS, 'key', {}), 'already_exists')
        self.assertFalse(any(c.args[0] == 'POST' and c.args[1] == 'pages' for c in api.call_args_list))

    def test_log_created_and_key_read_back(self):
        client, api = self.client([{'results': []}, {'id': SOURCE}, {'properties': {'週次キー': {'rich_text': [{'plain_text': 'key'}]}}}])
        self.assertEqual(client.save_log(DECISIONS, 'key', {}), 'created')
        self.assertEqual(api.call_args_list[-2].args[2]['parent']['data_source_id'], DECISIONS)

    def test_readback_mismatch_stops(self):
        client, api = self.client([{'results': []}, {'id': SOURCE}, {'properties': {}}])
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            client.save_log(LEARNING, 'key', {})

    def test_duplicate_existing_keys_are_not_silently_accepted(self):
        client, api = self.client([{'results': [{}, {}]}])
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            client.save_log(DECISIONS, 'key', {})

    def test_weekly_partial_success_resumes_using_same_keys(self):
        client = Mock()
        client.query.return_value = []
        client.save_log.side_effect = ['created', TimeoutError]
        rows = demo_rows(NOW)[:1]
        result = preview(NOW)
        with self.assertRaises(TimeoutError):
            save_weekly_logs(client, rows, result)
        first_key = client.save_log.call_args_list[0].args[1]
        client.save_log.side_effect = ['already_exists', 'created']
        self.assertEqual(save_weekly_logs(client, rows, result), ['already_exists', 'created'])
        self.assertEqual(client.save_log.call_args_list[2].args[1], first_key)

    def test_weekly_logs_link_to_sandbox_ticker_rows(self):
        client = Mock()
        client.query.return_value = [{'id': WATCH, 'properties': {'コード': {'rich_text': [{'plain_text': 'DEMO-A'}]}}}]
        save_weekly_logs(client, demo_rows(NOW)[:1], preview(NOW))
        self.assertEqual(client.save_log.call_args_list[0].args[2]['銘柄'], {'relation': [{'id': WATCH}]})

    def test_live_logs_are_rejected(self):
        with self.assertRaises(ValueError):
            save_weekly_logs(Mock(), [], {'synthetic': False})


class NewsFreshnessTests(TestCase):
    def entry(self, **updates):
        return dict({'title': '架空工業が増配', 'published': format_datetime(NOW),
                     'link': 'https://example.com/article?utm_source=feed&id=1#top'}, **updates)

    def test_tracking_variations_are_same_article(self):
        a = news_identity(self.entry(), ['架空工業'], NOW)
        b = news_identity(self.entry(link='https://example.com/article?id=1&utm_source=other'), ['架空工業'], NOW)
        self.assertEqual(a, b)

    def test_undated_old_future_and_unrelated_are_rejected(self):
        entries = [self.entry(published=''), self.entry(published=format_datetime(NOW - timedelta(days=15))),
                   self.entry(published=format_datetime(NOW + timedelta(hours=1))), self.entry(title='別会社が減配'),
                   self.entry(link='javascript:alert(1)')]
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertIsNone(news_identity(entry, ['架空工業'], NOW))

    def test_publisher_name_is_not_company_evidence(self):
        self.assertIsNone(news_identity(self.entry(title='別企業が減配 - 架空工業', source={'title': '架空工業'}), ['架空工業'], NOW))


class ReportContinuationTests(TestCase):
    def test_missing_dividend_keeps_price_metrics_but_withholds_dividend_total(self):
        ns = extract('daily_report.py', {'portfolio_metrics'}, is_usd_ticker=lambda t: False)
        row = ('DEMO', '架空A', 10, 900, {'price': 1000, 'div_yield': 0, 'dividend_known': False})
        self.assertEqual(ns['portfolio_metrics']({'holdings': [row]}, 150), (10000, 1000, None, None))

    def test_missing_price_withholds_all_totals(self):
        ns = extract('daily_report.py', {'portfolio_metrics'}, is_usd_ticker=lambda t: False)
        self.assertEqual(ns['portfolio_metrics']({'holdings': [('D', '架空', 10, 900, None)]}, 150), (None, None, None, None))

    def test_summary_displays_pending_instead_of_zero(self):
        ns = extract('daily_report.py', {'build_summary_cards'}, portfolio_metrics=lambda d, f: (None, None, None, None),
                     columns=lambda x: x, card=lambda title, lines, *args: (title, lines))
        cards = ns['build_summary_cards']({}, 150)
        self.assertTrue(all('算定保留' in c[0][1] for c in cards))
