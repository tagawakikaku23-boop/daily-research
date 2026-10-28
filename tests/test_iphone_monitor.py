import copy
import importlib.util
import unittest
from unittest.mock import patch
from pathlib import Path
from iphone_monitor import (Notion, adjustment, money, observation_rows, parse_mix,
                            parse_apple, properties, snapshot, code_block)

HAS_BS4 = importlib.util.find_spec('bs4') is not None
FIXTURES = Path(__file__).parent / 'fixtures' / 'iphone'


class PriceRules(unittest.TestCase):
    def test_no_historic_fallback_or_net_profit(self):
        rows = observation_rows({}, {}, '2026-10-29T08:00:00+09:00', None, ['failure'])
        self.assertTrue(rows)
        self.assertTrue(all(r['buy_price'] is None and r['apple_price'] is None and r['gross'] is None
                            and r['net'] is None and r['quality'] == '取得失敗' for r in rows))

    def test_color_deductions_and_restrictions(self):
        colors = ['バーガンディ', 'グレイシャー', 'ブラック', 'シルバー']
        note = 'グレイシャー/ブラック-6,000円 シルバー-11,000円'
        self.assertEqual(adjustment(note, 'ブラック', colors), 6000)
        self.assertEqual(adjustment(note, 'バーガンディ', colors), 0)
        self.assertIsNone(adjustment('バーガンディのみ 他色買取不可', 'ブラック', colors))
        with self.assertRaises(ValueError):
            adjustment('条件変更-要電話', 'ブラック', colors)

    def test_invalid_prices_rejected(self):
        for amount in ['nan', 'inf', '-1', '0', '2.5', '50000000']:
            with self.assertRaises(ValueError):
                money(amount)


@unittest.skipUnless(HAS_BS4, 'dedicated iPhone workflow installs parser dependencies')
class OfficialParsers(unittest.TestCase):
    def test_captured_featured_section_and_color_adjustment(self):
        featured, date = parse_mix((FIXTURES / 'mobilemix.html').read_text(encoding='utf-8'))
        apple = parse_apple((FIXTURES / 'apple.html').read_text(encoding='utf-8'))
        rows = observation_rows(featured, apple, '2026-10-29T08:00:00+09:00', date, [])
        row = next(r for r in rows if r['model'] == 'iPhone Duo' and r['capacity'] == '512GB' and r['color'] == 'スターホワイト')
        self.assertEqual(row['base_price'], 415000)
        self.assertEqual(row['buy_price'], 405000)
        self.assertEqual(row['apple_price'], 399800)
        self.assertEqual(row['gross'], 5200)
        self.assertIsNone(row['net'])
        self.assertIn('未取得', row['delivery'])
        self.assertFalse(any('iPad' in r['model'] for r in rows))

    def test_cookie_page_cannot_be_a_price(self):
        with self.assertRaises(ValueError):
            parse_mix('<h1>Cookieを有効にしてください</h1>')

    def test_unrecognized_condition_does_not_produce_buy_price(self):
        a = {('iPhone Duo', '512GB', 'ホワイト'): {'price': 399800}}
        rows = observation_rows({('iPhone Duo', '512GB'): {'base': 415000, 'condition': '保証開始減額要確認'}}, a, '2026-10-29', '2026/10/28', [])
        row = next(r for r in rows if r['color'] == 'ホワイト')
        self.assertIsNone(row['buy_price'])
        self.assertIsNone(row['gross'])


class SaveSafeguards(unittest.TestCase):
    def setUp(self):
        for name, result in [('previous', None), ('finish', {})]:
            task = patch.object(Notion, name, return_value=result)
            task.start()
            self.addCleanup(task.stop)

    def row(self):
        return observation_rows({}, {}, '2026-10-29T08:00:00+09:00', None, ['failure'])[0]

    @patch('iphone_monitor.time.sleep')
    def test_new_save_reads_back_all_fields(self, _):
        n = Notion('test', session=object())
        row = self.row()
        title = f"MM公式|slot|{row['model']} {row['capacity']}|{row['color']}"
        saved = {'id': 'new', 'properties': properties(row, title)}
        with patch.object(n, 'call', side_effect=[{'results': []}, {'id': 'new'}, saved]) as call:
            self.assertEqual(n.save(row, 'slot'), ('new', True))
            self.assertEqual(call.call_count, 3)
            self.assertEqual(call.call_args_list[-1].args, ('GET', 'pages/new'))

    def test_rerun_existing_snapshot_is_not_overwritten(self):
        n = Notion('test', session=object()); row = self.row()
        title = f"MM公式|slot|{row['model']} {row['capacity']}|{row['color']}"
        saved = {'id': 'old', 'properties': properties(row, title)}
        with patch.object(n, 'call', side_effect=[{'results': [{'id': 'old'}]}, saved]) as call:
            self.assertEqual(n.save(row, 'slot'), ('old', False))
            self.assertEqual(call.call_count, 2)
            self.assertFalse(any(c.args[1] == 'pages' for c in call.call_args_list))

    def test_lookup_failure_stops_before_create(self):
        n = Notion('test', session=object())
        with patch.object(n, 'call', side_effect=TimeoutError) as call:
            with self.assertRaises(TimeoutError): n.save(self.row(), 'slot')
            self.assertEqual(call.call_count, 1)

    def test_ambiguous_create_is_never_retried(self):
        n = Notion('test', session=object())
        with patch.object(n, 'call', side_effect=[{'results': []}, TimeoutError]) as call:
            with self.assertRaises(TimeoutError): n.save(self.row(), 'slot')
            self.assertEqual(call.call_count, 2)

    def test_readback_mismatch_is_failure(self):
        n = Notion('test', session=object()); row = self.row()
        title = f"MM公式|slot|{row['model']} {row['capacity']}|{row['color']}"
        saved = {'id': 'new', 'properties': properties(row, title)}
        saved['properties']['粗差額'] = {'number': 999}
        with patch.object(n, 'call', side_effect=[{'results': []}, {'id': 'new'}, saved]):
            with self.assertRaisesRegex(ValueError, 'read-back mismatch'): n.save(row, 'slot')

    def test_multiple_existing_records_stop(self):
        n = Notion('test', session=object())
        with patch.object(n, 'call', return_value={'results': [{'id': '1'}, {'id': '2'}]}):
            with self.assertRaises(ValueError): n.save(self.row(), 'slot')


class ReceiptTests(unittest.TestCase):
    def test_previous_lookup_separates_official_variant_and_earlier_time(self):
        n = Notion('test', session=object())
        row = observation_rows({}, {}, '2026-10-29T08:00:00+09:00', None, [])[0]
        with patch.object(n, 'call', return_value={'results': []}) as call:
            self.assertIsNone(n.previous(row))
            filters = call.call_args.args[2]['filter']['and']
            self.assertIn({'property': '価格出典', 'url': {'equals': 'https://mobile-mix.jp/'}}, filters)
            self.assertIn({'property': 'カラー', 'rich_text': {'equals': row['color']}}, filters)
            self.assertIn({'property': '確認日時', 'date': {'before': row['observed_at']}}, filters)

    def test_change_uses_same_variant_previous_and_handles_missing(self):
        row = observation_rows({}, {}, '2026-10-29T08:00:00+09:00', None, [])[0]
        row['buy_price'] = 100000
        evidence = snapshot(row, 'slot', {'buy_price': 95000, 'observed_at': '2026-10-28T08:00:00+09:00'})
        self.assertEqual(evidence['price_change']['buy_price_delta'], 5000)
        self.assertIsNone(snapshot(row, 'slot')['price_change']['buy_price_delta'])
        row['buy_price'] = None
        self.assertIsNone(snapshot(row, 'slot', {'buy_price': 95000})['price_change']['buy_price_delta'])

    def test_receipt_verified_only_after_property_readback(self):
        import json
        row = observation_rows({}, {}, '2026-10-29T08:00:00+09:00', None, [])[0]
        evidence = snapshot(row, 'slot')
        initial = dict(code_block(evidence), id='block')
        page = {'properties': properties(row, evidence['record_key'])}
        n = Notion('test', session=object())
        def response(method, path, body=None):
            if path.startswith('blocks/page/children'): return {'results': [initial]}
            if path == 'pages/page': return page
            if method == 'PATCH':
                initial['code'] = body['code']; return initial
            return initial
        with patch.object(n, 'call', side_effect=response) as call:
            saved = n.finish('page')
            self.assertEqual(saved['save_result'], 'verified')
            self.assertTrue(any(c.args[0] == 'PATCH' for c in call.call_args_list))
            self.assertEqual(call.call_args_list[-1].args, ('GET', 'blocks/block'))

    def test_missing_snapshot_does_not_recreate(self):
        n = Notion('test', session=object())
        with patch.object(n, 'call', return_value={'results': []}) as call:
            with self.assertRaises(ValueError): n.finish('page')
            self.assertEqual(call.call_count, 1)


if __name__ == '__main__':
    unittest.main()
