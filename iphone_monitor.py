"""Read-only official price research. No purchasing, orders or historic-price fallback."""
import argparse
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

JST = timezone(timedelta(hours=9))
MIX = 'https://mobile-mix.jp/'
DS = '25643f3c-6b80-4eb7-856f-cc8e25c136ac'
APPLE = 'https://www.apple.com/jp/shop/buy-iphone/'
TARGETS = {'iPhone Duo': ('iphone-duo', ['256GB', '512GB', '1TB', '2TB']),
           'iPhone 18 Pro Max': ('iphone-18-pro', ['256GB', '512GB', '1TB', '2TB']),
           'iPhone 18 Pro': ('iphone-18-pro', ['256GB', '512GB', '1TB'])}


def soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, 'html.parser')


def plain(html):
    return re.sub(r'\s+', ' ', soup(html).get_text(' ', strip=True)).strip()


def money(value):
    value = float(str(value).replace(',', ''))
    if not math.isfinite(value) or not 10000 <= value <= 2000000 or value != int(value):
        raise ValueError('invalid price')
    return int(value)


def parse_mix(html):
    doc = soup(html)
    if 'Cookieを有効にしてください' in doc.get_text() or '買取強化' not in doc.get_text():
        raise ValueError('cookie error or missing featured section')
    # Only the initially displayed featured table; never traverse other category endpoints.
    table = doc.select_one('table.list')
    if not table:
        raise ValueError('featured table missing')
    result = {}
    for td in table.select('td.product'):
        match = re.fullmatch(r'(iPhone .+?)\s+(\d+(?:GB|TB))', td.get_text(' ', strip=True))
        if not match:
            continue
        row = td.parent
        following = row.find_next_sibling('tr')
        cells = following.find_all('td', recursive=False) if following else []
        if len(cells) < 2 or cells[0].get_text(strip=True) != '未開封':
            continue
        price = row.select_one('td.price')
        price_match = re.fullmatch(r'([\d,]+)円', price.get_text(strip=True)) if price else None
        if not price_match:
            raise ValueError('featured price format changed')
        key = match.groups()
        if key in result:
            raise ValueError('ambiguous featured product')
        result[key] = {'base': money(price_match[1]), 'condition': cells[1].get_text(' ', strip=True)}
    updated = re.search(r'更新日[：:]\s*(\d{4}/\d{1,2}/\d{1,2})', doc.get_text(' ', strip=True))
    if not result or not updated:
        raise ValueError('featured prices or publication date missing')
    return result, updated[1]


def adjustment(note, color, colors):
    """Interpret visible color conditions only; unknown text fails closed."""
    if not note:
        return 0
    only = re.fullmatch(r'(.+?)のみ\s*他色買取不可', note)
    if only:
        return 0 if color == only[1].strip() else None
    remaining = note
    found = {}
    for match in re.finditer(r'([^\s､、,]+?)-([\d,]+)円', note):
        names = match[1].split('/')
        if any(name not in colors for name in names):
            raise ValueError('unknown color condition')
        for name in names:
            found[name] = int(match[2].replace(',', ''))
        remaining = remaining.replace(match[0], '', 1)
    if remaining.strip(' ､、,'):
        raise ValueError('unsupported condition')
    return found.get(color, 0)


def parse_apple(html):
    doc = soup(html)
    script = next((s.get_text() for s in doc.find_all('script')
                   if 'window.PRODUCT_SELECTION_BOOTSTRAP' in s.get_text()), None)
    if not script or '税込' not in doc.get_text() or 'SIMフリー' not in doc.get_text():
        raise ValueError('Apple tax/SIM-free/product data not confirmed')
    data = json.JSONDecoder().raw_decode(script.split('productSelectionData:', 1)[1].lstrip())[0]
    values = data['displayValues']
    result = {}
    for product in data['products']:
        if product.get('carrierModel') != 'UNLOCKED_JP' or product.get('comingSoon') or product.get('getReady'):
            continue
        model_html = values.get('dimensionScreensize', {}).get(product.get('dimensionScreensize'), {}).get('value', '')
        model = plain(model_html.split('\n')[0])
        if not model.startswith('iPhone'):
            heading = plain(str(doc.find('h1')))
            m = re.search(r'(iPhone [\w ]+?)(?:を購入|購入|$)', heading)
            model = m[1].strip() if m else ''
        cap = product['dimensionCapacity'].upper()
        color = plain(values['dimensionColor'][product['dimensionColor']]['value'])
        pricing = values['prices'][product['fullPrice']]
        if pricing.get('priceCurrency') != 'JPY' or pricing.get('refurbProduct'):
            continue
        price = money(pricing['currentPrice']['raw_amount'])
        quote = product.get('displayShippingQuote')
        # Never treat generic "earliest tomorrow" banners as a variant's delivery date.
        delivery = plain(quote) if isinstance(quote, str) and quote.strip() else '未取得（Apple公式でこの仕様の納期未確認）'
        key = (model, cap, color)
        if key in result and result[key]['price'] != price:
            raise ValueError('ambiguous Apple variant price')
        result[key] = {'price': price, 'delivery': delivery}
    if not result:
        raise ValueError('no unlocked Apple variants')
    return result


def observation_rows(featured, apples, stamp, publication, errors):
    rows = []
    keys = set(featured) | {(m, cap) for m, (_, caps) in TARGETS.items() for cap in caps}
    for model, cap in sorted(keys):
        offer = featured.get((model, cap))
        variants = [(color, value) for (m, c, color), value in apples.items() if (m, c) == (model, cap)]
        if not variants:
            variants = [('未取得', {})]
        colors = [color for color, _ in variants]
        for color, apple in variants:
            base = offer['base'] if offer else None
            note = offer['condition'] if offer else '買取強化の対象欄未取得／掲載なし'
            deduction, buy = None, None
            try:
                if offer and color != '未取得':
                    deduction = adjustment(note, color, colors)
                    buy = base - deduction if deduction is not None else None
            except ValueError:
                note += '／色別条件を解釈できず未取得'
            apple_price = apple.get('price')
            rows.append({'model': model, 'capacity': cap, 'color': color,
                         'observed_at': stamp, 'publication_date': publication,
                         'base_price': base, 'buy_price': buy, 'deduction': deduction,
                         'apple_price': apple_price, 'gross': buy - apple_price if buy is not None and apple_price is not None else None,
                         'net': None, 'shipping': None, 'condition': '新品未開封。' + note,
                         'delivery': apple.get('delivery', '未取得（Apple公式納期未確認）'),
                         'quality': '公式確認済' if buy is not None and apple_price is not None else '取得失敗',
                         'source': MIX, 'apple_url': APPLE + TARGETS.get(model, ('', []))[0],
                         'errors': errors})
    return rows


def check_robots(context, url):
    root = urlparse(url)
    robots_url = f'{root.scheme}://{root.netloc}/robots.txt'
    response = context.request.get(robots_url, timeout=30000)
    if response.status == 404:
        return
    if response.status != 200:
        raise ValueError('robots unavailable; stopped')
    parser = RobotFileParser()
    parser.parse(response.text().splitlines())
    if not parser.can_fetch('*', url):
        raise ValueError('robots disallows access')


def navigate(page, url):
    response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(1500)
    if url.startswith(MIX) and urlparse(page.url).path == '/cookie-error':
        # Ordinary first-party session: accept server cookies, revisit HOME once.
        response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(1500)
    if not response or response.status != 200 or urlparse(page.url).netloc != urlparse(url).netloc:
        raise ValueError('HTTP or unexpected redirect')
    if any(marker in page.locator('body').inner_text().lower() for marker in
           ('captcha', 'access denied', 'verify you are human', 'Cookieを有効にしてください'.lower())):
        raise ValueError('access restriction; stopped')
    return page.content()


def collect(channel=None):
    from playwright.sync_api import sync_playwright
    featured, apples, errors, publication = {}, {}, [], None
    stamp = datetime.now(JST).isoformat(timespec='seconds')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'channel': channel} if channel else {}))
        context = browser.new_context(locale='ja-JP', timezone_id='Asia/Tokyo')
        page = context.new_page()
        try:
            check_robots(context, MIX)
            html = navigate(page, MIX)
            featured, publication = parse_mix(html)
            # Capture terms and hash, not an assertion that all future conditions are fixed.
            terms = plain(navigate(page, MIX + 'terms-of-service'))
            if '禁止事項' not in terms:
                raise ValueError('terms not confirmed')
            if re.search(r'(スクレイピング|クローリング|自動取得).{0,40}(禁止|許可)', terms):
                raise ValueError('automation condition requires review')
            terms_hash = hashlib.sha256(terms.encode()).hexdigest()
        except Exception as exc:
            featured = {}
            terms_hash = None
            errors.append('MobileMix:' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
        for slug in sorted({TARGETS[m][0] for m, _ in featured if m in TARGETS}):
            try:
                url = APPLE + slug
                check_robots(context, url)
                time.sleep(2)
                apples.update(parse_apple(navigate(page, url)))
            except Exception as exc:
                errors.append('Apple/' + slug + ':' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
        browser.close()
    return {'observed_at': stamp, 'terms_sha256': terms_hash,
            'rows': observation_rows(featured, apples, stamp, publication, errors)}


def rich(text):
    return [{'type': 'text', 'text': {'content': str(text)[i:i+1800]}} for i in range(0, len(str(text)), 1800)]


def properties(row, title):
    note = (f"公式トップ『買取強化』基準額={row['base_price']}円。掲載更新日={row['publication_date']}。"
            f"{row['condition']}。取得エラー={','.join(row['errors']) or 'なし'}。"
            '送料・減額・諸費用未確定につき純利益空欄。掲載額は査定確約ではない。購入・買取申込なし。')
    props = {'記録名': {'title': rich(title)}, '確認日時': {'date': {'start': row['observed_at']}},
             'モデル': {'select': {'name': row['model'] if row['model'] in TARGETS else 'その他'}},
             '容量': {'select': {'name': row['capacity']}}, 'カラー': {'rich_text': rich(row['color'])},
             '買取先': {'select': {'name': 'モバイルミックス'}}, 'データ品質': {'select': {'name': row['quality']}},
             '判定': {'select': {'name': '公式要確認'}}, '価格出典': {'url': row['source']},
             'Apple URL': {'url': row['apple_url'] if row['model'] in TARGETS else None},
             '備考': {'rich_text': rich(note)}, '納期': {'rich_text': rich(row['delivery'])}}
    for name, key in [('Apple定価', 'apple_price'), ('買取参考額', 'buy_price'), ('色別減額', 'deduction'),
                      ('粗差額', 'gross'), ('推定純利益', 'net'), ('送料など', 'shipping')]:
        props[name] = {'number': row[key]}
    return props


def value(prop):
    kind = prop.get('type') or next(iter(prop))
    v = prop.get(kind)
    if kind in ('rich_text', 'title'):
        return ''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in v)
    if kind == 'select':
        return v.get('name') if v else None
    if kind == 'date':
        # Notion normalizes timezone and fractional seconds.
        return datetime.fromisoformat(v['start'].replace('Z', '+00:00')) if v else None
    return v


class Notion:
    def __init__(self, token, session=None):
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.headers = {'Authorization': 'Bearer ' + token, 'Notion-Version': '2025-09-03', 'Content-Type': 'application/json'}

    def call(self, method, path, body=None):
        # No automatic POST retries, including ambiguous timeouts and HTTP 5xx.
        response = self.session.request(method, 'https://api.notion.com/v1/' + path,
                                        headers=self.headers, json=body, timeout=30)
        response.raise_for_status()
        time.sleep(0.35)
        return response.json()

    def preflight(self):
        schema = self.call('GET', 'data_sources/' + DS)['properties']
        for key, expected in [('記録名', 'title'), ('確認日時', 'date'), ('カラー', 'rich_text'),
                              ('Apple定価', 'number'), ('買取参考額', 'number'), ('粗差額', 'number'),
                              ('推定純利益', 'number'), ('送料など', 'number'), ('備考', 'rich_text')]:
            if schema.get(key, {}).get('type') != expected:
                raise ValueError('Notion schema mismatch: ' + key)

    def save(self, row, slot):
        title = f"MM公式|{slot}|{row['model']} {row['capacity']}|{row['color']}"
        expected = properties(row, title)
        query = {'filter': {'property': '記録名', 'title': {'equals': title}}, 'page_size': 2}
        found = self.call('POST', 'data_sources/' + DS + '/query', query)
        if found.get('has_more') or len(found['results']) > 1:
            raise ValueError('duplicate records require review')
        if found['results']:
            saved = self.call('GET', 'pages/' + found['results'][0]['id'])
            if value(saved['properties']['記録名']) != title:
                raise ValueError('existing record mismatch')
            return saved['id'], False
        created = self.call('POST', 'pages', {'parent': {'data_source_id': DS}, 'properties': expected})
        saved = self.call('GET', 'pages/' + created['id'])
        for key, prop in expected.items():
            if value(saved['properties'][key]) != value(prop):
                raise ValueError('Notion read-back mismatch: ' + key)
        return saved['id'], True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='iphone-observation.json')
    parser.add_argument('--channel')
    parser.add_argument('--save', action='store_true')
    parser.add_argument('--verify-rerun', action='store_true')
    parser.add_argument('--slot', required=True)
    args = parser.parse_args()
    observation = collect(args.channel)
    Path(args.output).write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.save:
        notion = Notion(os.environ['NOTION_API_KEY'])
        notion.preflight()
        for row in observation['rows']:
            _, created = notion.save(row, args.slot)
            print('Verified new record' if created else 'Verified existing record; unchanged')
            if args.verify_rerun:
                _, duplicate_created = notion.save(row, args.slot)
                if duplicate_created:
                    raise ValueError('duplicate prevention failed')
        print('Notion read-back and duplicate checks passed')
    good = sum(r['quality'] == '公式確認済' for r in observation['rows'])
    print(f'Official variants verified: {good}/{len(observation["rows"])}')
    if not good:
        raise SystemExit('No official paired prices acquired; failure recorded when saving enabled')


if __name__ == '__main__':
    main()
