"""Notion adapters. Production source is read-only; writes target the sandbox only."""
import json
import time
from urllib.error import HTTPError
import re
from urllib.request import Request, urlopen
from uuid import UUID

SOURCE = 'ef95daef-20d6-4021-ac66-f4d8e18a753c'
WATCH = '52c1b668-9404-4fae-9cef-6c0183f2871e'
DECISIONS = 'e6fe464d-de6d-4527-a070-0ad8c56f7524'
LEARNING = 'ebc49cae-32c0-412c-9f69-a140137b5c40'
HOME = '408fc83b-0e47-81da-b814-e8e4f351dbc9'
SETTINGS = '408fc83b-0e47-81ed-85fb-dfbb855550f1'


def identifier(value):
    return str(UUID(value.removeprefix('collection://')))


def value(properties, name, kind):
    prop = properties.get(name, {})
    if prop.get('type', kind) != kind:
        return None
    val = prop.get(kind)
    if kind in {'title', 'rich_text'}:
        return ''.join(item.get('plain_text', item.get('text', {}).get('content', '')) for item in (val or [])) or None
    if kind == 'select':
        return (val or {}).get('name')
    if kind == 'date':
        return (val or {}).get('start')
    return val


def adapt_row(page):
    """No inferred currency, default shares, timestamps, asset class or financials."""
    p = page.get('properties', {})
    row = {'page_id': page.get('id'), 'name': value(p, '銘柄名', 'title') or '名称未設定',
           'ticker': value(p, 'コード', 'rich_text'),
           'kind': {'保有': 'holding', '候補': 'watch', '監視': 'monitor'}.get(value(p, '区分', 'select'), 'unknown')}
    for field, name in {'price': '現在値', 'shares': '株数', 'cost': '取得単価', 'annual_dps': '一株年間配当',
                        'payout_ratio_pct': '配当性向%', 'net_income': '純利益', 'operating_cf': '営業CF',
                        'free_cf': 'FCF', 'target_yield_pct': '希望利回り%', 'max_price': '購入上限価格'}.items():
        row[field] = value(p, name, 'number')
    for field, name in {'currency': '価格通貨', 'dividend_currency': '配当通貨', 'cost_currency': '取得単価通貨',
                        'asset_type': '資産種別'}.items():
        row[field] = value(p, name, 'select')
    row['asset_type'] = {'普通株': 'common_stock', 'ETF': 'ETF', 'REIT': 'REIT'}.get(row['asset_type'])
    row['sector'] = value(p, '業種', 'rich_text')
    row['sector_room'] = value(p, '業種配分確認', 'checkbox')
    row['source_url'] = value(p, '出典URL', 'url')
    row['dividend_cut_5y'] = {'5年減配なし': False, '減配あり': True}.get(value(p, '減配履歴', 'select'))
    row['dividend_basis'] = {'予想年間': 'forward_annual', '実績年間': 'trailing_annual'}.get(value(p, '配当区分', 'select'))
    for field, name in {'price_asof': '価格基準日', 'financial_asof': '財務基準日', 'dividend_asof': '配当基準日'}.items():
        row[field] = value(p, name, 'date')
    return row


class NotionClient:
    def __init__(self, token, transport=None, sleep=time.sleep, clock=time.monotonic):
        self.token, self.sleep, self.clock = token, sleep, clock
        self.transport = transport or self._http
        self.last_request = None

    def _http(self, method, path, body):
        request = Request('https://api.notion.com/v1/' + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Notion-Version': '2025-09-03', 'Content-Type': 'application/json'}, method=method)
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def request(self, method, path, body=None, read_only=False):
        # Keep credentials away from redirects/paths outside our explicit API surface.
        if '://' in path or '..' in path:
            raise ValueError('Invalid API path')
        query_match = re.fullmatch(r'data_sources/([0-9a-f-]+)/query', path)
        is_query = method == 'POST' and query_match is not None and identifier(query_match[1]) in {SOURCE, WATCH, DECISIONS, LEARNING}
        is_get = method == 'GET' and (re.fullmatch(r'(data_sources|databases|pages)/[0-9a-f-]+', path) is not None
                                     or re.fullmatch(r'blocks/[0-9a-f-]+/children(?:\?[a-zA-Z0-9_%=&-]+)?', path) is not None)
        is_create = method == 'POST' and path == 'pages'
        if is_create:
            parent = (body or {}).get('parent', {})
            allowed = parent.get('data_source_id') in {DECISIONS, LEARNING} or parent.get('page_id') == HOME
            if not allowed:
                raise ValueError('Production writes are prohibited')
        if not (is_query or is_get or is_create):
            raise ValueError('API operation outside allowed scope')
        read_only = is_get or is_query
        for attempt in range(3):
            if self.last_request is not None:
                self.sleep(max(0, .4 - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            try:
                return self.transport(method, path, body)
            except HTTPError as error:
                # Query POST is read-only. Mutation 5xx/timeout is never retried blindly.
                safe_retry = read_only and error.code in {429, 500, 502, 503, 504, 529}
                if not safe_retry or attempt == 2:
                    raise RuntimeError('Notion request failed; private response suppressed') from None
                if error.code == 429:
                    payload = error.read().decode('utf-8', errors='replace') if error.fp else ''
                    if 'public_api_request_blocked' in payload:
                        raise RuntimeError('Notion API access blocked') from None
                retry = error.headers.get('Retry-After') if error.headers else None
                if retry is not None:
                    try:
                        delay = int(retry)
                    except (ValueError, TypeError):
                        raise RuntimeError('Invalid retry delay') from None
                    if delay < 0 or delay > 60:
                        raise RuntimeError('Retry deferred; wait for the server retry window') from None
                else:
                    delay = 2 ** attempt
                self.sleep(delay)

    def query(self, data_source, filter=None):
        ds = identifier(data_source)
        if ds not in {SOURCE, WATCH, DECISIONS, LEARNING}:
            raise ValueError('Data source outside allowed scope')
        rows, cursor, seen = [], None, set()
        while True:
            body = {'page_size': 100}
            if filter:
                body['filter'] = filter
            if cursor:
                body['start_cursor'] = cursor
            data = self.request('POST', f'data_sources/{ds}/query', body, read_only=True)
            rows.extend(data['results'])
            if not data.get('has_more'):
                return rows
            cursor = data.get('next_cursor')
            if not cursor or cursor in seen:
                raise RuntimeError('Invalid pagination; partial data rejected')
            seen.add(cursor)

    def read_portfolio(self):
        return [adapt_row(page) for page in self.query(SOURCE) if not page.get('archived') and not page.get('in_trash')]

    def read_goal(self, page_id=SETTINGS):
        from investment_analysis import number
        page_id = identifier(page_id)
        page = self.request('GET', 'pages/' + page_id, read_only=True)
        if page.get('parent', {}).get('page_id', '').replace('-', '') != HOME.replace('-', ''):
            raise ValueError('Settings page outside private sandbox')
        data = self.request('GET', f'blocks/{page_id}/children?page_size=100', read_only=True)
        if data.get('has_more'):
            raise ValueError('Settings page is ambiguous')
        settings = []
        for block in data['results']:
            code = block.get('code', {})
            if code.get('language') == 'json':
                payload = ''.join(x.get('plain_text', x.get('text', {}).get('content', '')) for x in code.get('rich_text', []))
                settings.append(json.loads(payload))
        if len(settings) != 1:
            raise ValueError('Exactly one settings object required')
        goal = settings[0]
        amount = number(goal.get('annual_goal_jpy'))
        if amount is None or amount <= 0 or goal.get('currency') != 'JPY' or goal.get('tax_basis') != 'before_tax':
            raise ValueError('Invalid dividend goal configuration')
        return amount

    def save_log(self, ds, key, properties):
        ds = identifier(ds)
        if ds not in {DECISIONS, LEARNING}:
            raise ValueError('Log writes are limited to sandbox logs')
        # A log destination must remain under the designated private sandbox.
        meta = self.request('GET', 'data_sources/' + ds, read_only=True)
        database = meta.get('parent', {}).get('database_id')
        if not database:
            raise ValueError('Cannot verify log parent')
        container = self.request('GET', 'databases/' + identifier(database), read_only=True)
        if container.get('parent', {}).get('page_id', '').replace('-', '') != HOME.replace('-', ''):
            raise ValueError('Log database moved outside sandbox')
        matches = self.query(ds, {'property': '週次キー', 'rich_text': {'equals': key}})
        if len(matches) > 1:
            raise RuntimeError('Duplicate weekly log keys require review')
        if matches:
            return 'already_exists'
        properties = dict(properties, **{'週次キー': text_property(key)})
        created = self.request('POST', 'pages', {
            'parent': {'type': 'data_source_id', 'data_source_id': ds}, 'properties': properties})
        saved = self.request('GET', 'pages/' + identifier(created['id']), read_only=True)
        if value(saved.get('properties', {}), '週次キー', 'rich_text') != key:
            raise RuntimeError('Log readback mismatch')
        return 'created'


def text_property(text, title=False):
    key = 'title' if title else 'rich_text'
    return {key: [{'type': 'text', 'text': {'content': str(text)[:1800]}}]}


def save_weekly_logs(client, rows, result):
    """Synthetic rows only at this stage; completion can resume after partial success."""
    if result.get('synthetic') is not True:
        raise ValueError('Live portfolio logs are not enabled')
    from investment_analysis import candidate
    from datetime import datetime
    now = datetime.fromisoformat(result['generated_at'])
    links = {}
    for page in client.query(WATCH):
        ticker = value(page.get('properties', {}), 'コード', 'rich_text')
        if ticker:
            if ticker in links:
                raise ValueError('Duplicate sandbox tickers require review')
            links[ticker] = identifier(page['id'])
    statuses = []
    for row in rows:
        verdict = candidate(row, now)
        reason = ' / '.join(verdict['reasons'])
        # Stable ticker in fixtures; never use company names for identity.
        ticker = row.get('ticker')
        if not ticker:
            raise ValueError('Stable ticker required for weekly logs')
        relation = {'銘柄': {'relation': [{'id': links[ticker]}]}} if ticker in links else {}
        key = result['key'] + ':decision:' + ticker
        statuses.append(client.save_log(DECISIONS, key, {
            **relation,
            '記録': text_property(row['name'] + '：週次確認', title=True),
            '判断': {'select': {'name': '検討' if verdict['eligible'] else '判断保留'}},
            '記録種別': {'select': {'name': '自動提案'}},
            '根拠': text_property(reason), '次回確認条件': text_property('一次資料と購入条件を確認する'),
            '日付': {'date': {'start': now.date().isoformat()}}}))
        statuses.append(client.save_log(LEARNING, result['key'] + ':learning:' + ticker, {
            **relation,
            'テーマ': text_property(row['name'] + '：配当を支える利益とCF', title=True),
            '確認状態': {'select': {'name': '未確認教材'}},
            '観察': text_property(reason),
            '仮説': text_property('利益とキャッシュフローの両方から配当の持続性を検討する'),
            '次の確認': text_property('決算資料・配当方針・減配履歴を確認し、不明点を残す'),
            '日付': {'date': {'start': now.date().isoformat()}}}))
    return statuses
