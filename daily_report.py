#!/usr/bin/env python3
"""
毎日リサーチ日報 - 株式＋銘柄ニュース＋AIアドバイス＋カテゴリニュース → Notion自動作成
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import requests
import yfinance as yf
import feedparser
from groq import Groq
from datetime import datetime, timezone, timedelta
import time
import os
from pathlib import Path

# ローカル実行時は .env を読み込む
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── 設定（環境変数から取得）────────────────────────
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
PARENT_PAGE_ID = os.environ["NOTION_PAGE_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

# インデックス
INDICES = [
    ("^N225",  "日経平均"),
    ("^GSPC",  "S&P 500"),
    ("^DJI",   "NYダウ"),
    ("^IXIC",  "NASDAQ"),
]

# 保有個別株 (ticker, 銘柄名, 保有株数, 平均取得単価)
HOLDINGS = [
    ("8591.T", "オリックス",       5,   1442),
    ("9434.T", "ソフトバンク",    10,    130),
    ("8411.T", "みずほFG",         5,   1360),
    ("2914.T", "JT",               1,   2139),
    ("8002.T", "丸紅",             3,    740),
    ("8001.T", "伊藤忠商事",       5,    651),
    ("9904.T", "ベリテ",          10,    394),
    ("3315.T", "日本コークス",    50,    110),
    ("8173.T", "Joshin",           3,   2270),
    ("4502.T", "武田薬品",        18,   3471),
    ("1835.T", "東鉄工業",        12,   2243),
    ("8697.T", "JPX",            200,    936),
    ("6301.T", "コマツ",          15,   2916),
    ("9432.T", "NTT",            225,    158),
    ("VOO",    "VOO(S&P500ETF)",   1, 56141),  # 355USD×158円
    ("SPYD",   "SPYD(高配当ETF)",  5,  6361),  # 40.26USD×158円
]

# 新規購入検討銘柄
WATCHLIST = [
    ("4246.T", "ダイキョーニシカワ"),
    ("8892.T", "エスコン"),
    ("8923.T", "トーセイ"),
    ("9252.T", "ラストワンマイル"),
    ("8061.T", "西華産業"),
    ("8316.T", "三井住友FG"),
    ("9503.T", "関西電力"),
    ("1489.T", "日経高配当50ETF"),
]

# 監視中銘柄
MONITOR = [
    ("1605.T", "INPEX"),
    ("3003.T", "ヒューリック"),
    ("3543.T", "コメダHD"),
    ("4063.T", "信越化学工業"),
    ("4452.T", "花王 ★34年連続増配"),
    ("8031.T", "三井物産"),
    ("8058.T", "三菱商事"),
    ("8306.T", "三菱UFJFG"),
    ("8766.T", "東京海上HD"),
    ("8593.T", "三菱HCキャピタル ★連続増配"),
    ("9433.T", "KDDI ★23年連続増配"),
    ("9436.T", "沖縄セルラー電話 ★連続増配"),
    ("7466.T", "SPK ★連続増配"),
]

# カテゴリニュースRSS
NEWS_FEEDS = {
    "📰 日本経済": [
        "https://www3.nhk.or.jp/rss/news/cat4.xml",
        "https://www3.nhk.or.jp/rss/news/cat6.xml",
    ],
    "🌐 世界経済": [
        "https://feeds.bbci.co.uk/japanese/rss.xml",
    ],
    "💻 IT・テクノロジー": [
        "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
    ],
    "📱 携帯キャリア・ガジェット": [
        "https://k-tai.watch.impress.co.jp/data/rss/1.0/ktw/feed.rdf",
        "https://pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf",
    ],
    "🌾 新潟・地域経済": [
        "https://news.google.com/rss/search?q=新潟+経済&hl=ja&gl=JP&ceid=JP:ja",
    ],
}

# ── 株価・分析データ取得 ──────────────────────────
def get_stock_data(ticker):
    try:
        t    = yf.Ticker(ticker)
        info = t.info
        hist = t.history(period="2d")
        if len(hist) < 1:
            return None

        price   = hist["Close"].iloc[-1]
        prev    = hist["Close"].iloc[-2] if len(hist) >= 2 else price
        chg_pct = (price - prev) / prev * 100
        low52   = info.get("fiftyTwoWeekLow", price)
        high52  = info.get("fiftyTwoWeekHigh", price)
        div_yield = info.get("dividendYield") or 0
        per     = info.get("trailingPE")

        span     = high52 - low52
        position = (price - low52) / span if span > 0 else 0.5

        if position < 0.3:
            signal = "🟢 割安圏"
        elif position < 0.65:
            signal = "🟡 適正圏"
        else:
            signal = "🔴 高値圏"

        return {
            "price": price, "chg_pct": chg_pct,
            "low52": low52, "high52": high52,
            "position": position, "div_yield": div_yield,
            "per": per, "signal": signal,
        }
    except Exception as e:
        return {"error": str(e)}

# ── 銘柄ニュース取得（Google News日本語RSS） ────────
def get_ticker_news(company_name, max_items=3):
    """Google NewsのRSSで日本語ニュースを取得"""
    try:
        import urllib.parse
        query = urllib.parse.quote(company_name.replace("★連続増配", "").replace("★連続増配", "").strip())
        url   = f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
        feed  = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            link  = entry.get("link", "")
            src   = entry.get("source", {}).get("title", "")
            if title:
                label = f"[{src}] {title}" if src else title
                items.append((label, link))
        return items
    except Exception:
        return []

# ── 買い/様子見/慎重 判定 ────────────────────────
def trade_signal(position, div_yield, gain_pct=None):
    """52W位置・配当・損益から総合判定を返す"""
    # 含み損かつ配当低い → 要検討
    if gain_pct is not None and gain_pct < -15 and div_yield < 3.0:
        return "🔴 要検討（含み損・低配当）"
    # 割安圏 + 高配当
    if position < 0.30 and div_yield >= 4.0:
        return "🟢 強い買い検討（割安＋高配当）"
    if position < 0.35 and div_yield >= 3.0:
        return "🟢 買い検討（割安＋配当良好）"
    if position < 0.40 and div_yield >= 3.0:
        return "🟢 買い検討圏"
    # 高値圏
    if position > 0.80:
        return "🔴 追加購入は慎重（高値圏）"
    if position > 0.65:
        return "🟡 様子見（やや高値）"
    # 適正圏
    if div_yield >= 3.0:
        return "🟡 継続保有（配当良好）"
    return "🟡 様子見"

# ── 日経電子版スクレイピング ─────────────────────
def get_nikkei_news(max_items=8):
    """日経電子版にログインしてトップニュースを取得"""
    email    = os.environ.get("NIKKEI_EMAIL", "")
    password = os.environ.get("NIKKEI_PASSWORD", "")
    if not email or not password:
        return []
    try:
        from bs4 import BeautifulSoup

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })

        # ログインページ取得 → フォーム解析
        login_resp = session.get("https://www.nikkei.com/login/", timeout=15)
        soup = BeautifulSoup(login_resp.text, "html.parser")
        form = soup.find("form")

        payload     = {}
        form_action = "https://id.nikkei.com/lounge/nl/base/LA0010.seam"

        if form:
            raw = form.get("action", "")
            if raw:
                form_action = raw if raw.startswith("http") else "https://id.nikkei.com" + raw
            for inp in form.find_all("input"):
                n = inp.get("name", "")
                v = inp.get("value", "")
                if n:
                    payload[n] = v

        # メール・パスワードフィールドを動的検出（見つからなければデフォルト名）
        email_field = next(
            (i.get("name") for i in soup.find_all("input", type=["email","text"]) if i.get("name")),
            "LA0010Form:userID"
        )
        pass_field = next(
            (i.get("name") for i in soup.find_all("input", type="password") if i.get("name")),
            "LA0010Form:password"
        )
        payload[email_field] = email
        payload[pass_field]  = password

        # ログイン実行
        session.post(form_action, data=payload, timeout=15, allow_redirects=True)

        # トップページからニュース収集
        top = session.get("https://www.nikkei.com/", timeout=15)
        soup2 = BeautifulSoup(top.text, "html.parser")

        items = []
        seen  = set()
        for a in soup2.find_all("a", href=True):
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if ("/article/" in href or "/nkd/issue/" in href) and len(text) > 8:
                url = href if href.startswith("http") else "https://www.nikkei.com" + href
                if text not in seen:
                    seen.add(text)
                    items.append((text, url))
            if len(items) >= max_items:
                break

        return items
    except Exception as e:
        print(f"  日経スクレイピング失敗: {e}")
        return []

# ── 新潟日報スクレイピング ────────────────────────
def get_niigata_nippo_news(max_items=8):
    """新潟日報にログインして地域経済ニュースを取得"""
    email    = os.environ.get("NIIGATA_NIPPO_EMAIL", "")
    password = os.environ.get("NIIGATA_NIPPO_PASSWORD", "")
    if not email or not password:
        return []

    EXCLUDE = [
        "スポーツ","野球","サッカー","バスケ","バレー","陸上","水泳","ラグビー",
        "高校生","中学生","小学生","子ども","こども","児童","学校","部活",
        "甲子園","Jリーグ","プロ野球","アルビ","吹奏楽","合唱","俳句","短歌",
    ]

    try:
        from bs4 import BeautifulSoup

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })

        # ログイン
        login_resp = session.get("https://www.niigata-nippo.co.jp/login/", timeout=15)
        soup = BeautifulSoup(login_resp.text, "html.parser")
        form = soup.find("form")

        payload     = {}
        form_action = "https://www.niigata-nippo.co.jp/login/"

        if form:
            raw = form.get("action", "")
            if raw:
                form_action = raw if raw.startswith("http") else "https://www.niigata-nippo.co.jp" + raw
            for inp in form.find_all("input"):
                n = inp.get("name", "")
                v = inp.get("value", "")
                if n:
                    payload[n] = v

        email_field = next(
            (i.get("name") for i in soup.find_all("input", type=["email","text"]) if i.get("name")),
            "email"
        )
        pass_field = next(
            (i.get("name") for i in soup.find_all("input", type="password") if i.get("name")),
            "password"
        )
        payload[email_field] = email
        payload[pass_field]  = password

        session.post(form_action, data=payload, timeout=15, allow_redirects=True)

        # 経済カテゴリページを優先
        target_urls = [
            "https://www.niigata-nippo.co.jp/economy/",
            "https://www.niigata-nippo.co.jp/news/economics/",
            "https://www.niigata-nippo.co.jp/",
        ]

        items = []
        seen  = set()

        for url in target_urls:
            resp  = session.get(url, timeout=15)
            soup2 = BeautifulSoup(resp.text, "html.parser")
            for a in soup2.find_all("a", href=True):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if len(text) < 8:
                    continue
                if any(kw in text for kw in EXCLUDE):
                    continue
                if "/article/" in href or "/news/" in href:
                    full_url = href if href.startswith("http") else "https://www.niigata-nippo.co.jp" + href
                    if text not in seen:
                        seen.add(text)
                        items.append((text, full_url))
                if len(items) >= max_items:
                    break
            if len(items) >= max_items:
                break

        return items[:max_items]
    except Exception as e:
        print(f"  新潟日報スクレイピング失敗: {e}")
        return []

# ── カテゴリニュース取得（RSS） ───────────────────
def get_rss_news(urls, max_items=5):
    items = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "")
                if title and len(title) > 5:
                    items.append((title, link))
                if len(items) >= max_items:
                    break
            if items:
                break
        except Exception:
            continue
    return items[:max_items]

# ── Groq APIでアドバイス生成 ─────────────────────
def generate_advice(portfolio_summary, news_summary):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""あなたは日本株・米国株に詳しい投資アドバイザーです。
以下のポートフォリオデータと最新ニュースをもとに、今日のアドバイスをください。

【投資方針・好み】
- 基本思想：両学長（リベラルアーツ大学）と山崎元氏の影響を強く受けている
- 投資信託（eMAXIS Slim等）：山崎元氏スタイル。低コストインデックスで長期積立。配当不要・分析不要
- 個別株・ETF：両学長スタイル。高配当・連続増配・株主還元重視でキャッシュフロー構築。目標配当利回り3%以上
- 連続増配・株主還元に積極的な企業が好み（花王34年・KDDI23年連続増配など）
- 高値掴みを避けたい。52週レンジの低い位置で仕込みたい
- 分散投資を意識（セクター・地域ともに）
- FIREや経済的自由を意識した資産形成が目的

【今日のポートフォリオ状況】
{portfolio_summary}

【関連ニュース】
{news_summary}

以下の形式で日本語でアドバイスをください（各項目3〜5行）：

1. 📊 今日の相場観（インデックスの動きから市場全体の状況）
2. 💡 今日の買い増し候補（割安圏・高配当・連続増配の観点でおすすめ）
3. ⚠️ 注意銘柄（高値圏・含み損・配当3%未満で改善余地ある銘柄）
4. 🏆 連続増配・株主還元銘柄の注目ポイント
5. 🔄 ポートフォリオ全体への一言（分散・バランスの観点）
6. 📰 今日のニュースで保有銘柄・検討銘柄に影響しそうな話題"""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"AIアドバイス生成失敗: {e}"

# ── ニュースダイジェスト生成 ─────────────────────
def generate_news_digest(all_news_text):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""以下は今日の各カテゴリのニュース一覧です。
投資家・個人の資産形成に関心がある人向けに、特に重要なニュースを5点に絞って日本語で要約してください。
経済・マーケット・テクノロジー・地域ニュースをバランスよく取り上げてください。
各項目は1〜2行で簡潔に。

【今日のニュース一覧】
{all_news_text}

形式：
• [カテゴリ] 要約文
• [カテゴリ] 要約文
（5点）"""
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"ダイジェスト生成失敗: {e}"

# ── フォーマットヘルパー ──────────────────────────
def format_price(ticker, price):
    if ".T" in ticker or ticker.startswith("^N") or ticker.startswith("^T"):
        return f"¥{price:,.0f}"
    return f"${price:,.2f}"

def div_label(dy):
    if dy >= 4.0:   return f"配当{dy:.1f}% 🏆"
    elif dy >= 3.0: return f"配当{dy:.1f}% ✅"
    elif dy > 0:    return f"配当{dy:.1f}% ❌"
    return "配当データなし"

def holding_line(ticker, name, shares, cost, d):
    if not d or "error" in d:
        return f"{name}  データ取得失敗"
    price    = d["price"]
    chg_str  = f"+{d['chg_pct']:.2f}%" if d["chg_pct"] >= 0 else f"{d['chg_pct']:.2f}%"
    gain_pct = (price - cost) / cost * 100
    gain_str = f"+{gain_pct:.1f}%" if gain_pct >= 0 else f"{gain_pct:.1f}%"
    per_str  = f"PER:{d['per']:.1f}" if d["per"] else ""
    signal   = trade_signal(d["position"], d["div_yield"], gain_pct)
    return (f"{name}  {format_price(ticker, price)}  前日比{chg_str}  "
            f"取得比{gain_str}  52W:{int(d['position']*100)}%  "
            f"{div_label(d['div_yield'])}  {per_str}  → {signal}")

def watch_line(ticker, name, d):
    if not d or "error" in d:
        return f"{name}  データ取得失敗"
    chg_str = f"+{d['chg_pct']:.2f}%" if d["chg_pct"] >= 0 else f"{d['chg_pct']:.2f}%"
    per_str = f"PER:{d['per']:.1f}" if d["per"] else ""
    signal  = trade_signal(d["position"], d["div_yield"])
    return (f"{name}  {format_price(ticker, d['price'])}  前日比{chg_str}  "
            f"52W:{int(d['position']*100)}%  "
            f"{div_label(d['div_yield'])}  {per_str}  → {signal}")

# ── Notionブロックヘルパー ────────────────────────
def h2(text):
    return {"object":"block","type":"heading_2",
            "heading_2":{"rich_text":[{"type":"text","text":{"content":text[:2000]}}]}}

def h3(text):
    return {"object":"block","type":"heading_3",
            "heading_3":{"rich_text":[{"type":"text","text":{"content":text[:2000]}}]}}

def bul(text, url=None):
    rt = {"type":"text","text":{"content":text[:2000]}}
    if url: rt["text"]["link"] = {"url": url}
    return {"object":"block","type":"bulleted_list_item",
            "bulleted_list_item":{"rich_text":[rt]}}

def callout(text, emoji="💡"):
    return {"object":"block","type":"callout",
            "callout":{"rich_text":[{"type":"text","text":{"content":text[:2000]}}],
                       "icon":{"type":"emoji","emoji":emoji}}}

def quote(text):
    return {"object":"block","type":"quote",
            "quote":{"rich_text":[{"type":"text","text":{"content":text[:2000]}}]}}

def para(text):
    return {"object":"block","type":"paragraph",
            "paragraph":{"rich_text":[{"type":"text","text":{"content":text[:2000]}}]}}

def divider():
    return {"object":"block","type":"divider","divider":{}}

def create_page(title, blocks, icon="📋"):
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type":  "application/json",
        "Notion-Version": "2022-06-28",
    }
    data = {
        "parent": {"page_id": PARENT_PAGE_ID},
        "icon":   {"type":"emoji","emoji":icon},
        "properties": {"title":{"title":[{"text":{"content":title}}]}},
        "children": blocks[:100],
    }
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=data)
    resp.raise_for_status()
    pid = resp.json()["id"]
    if len(blocks) > 100:
        for i in range(100, len(blocks), 100):
            requests.patch(f"https://api.notion.com/v1/blocks/{pid}/children",
                           headers=headers, json={"children": blocks[i:i+100]})
    return resp.json().get("url", "")

# ── 株式ページ ────────────────────────────────────
def create_stock_page(date_str):
    print("\n==== 株式レポート作成中 ====")
    blocks = []
    portfolio_lines = []
    news_lines      = []
    total_div       = 0
    holdings_data   = []

    # インデックス
    blocks.append(h2("📈 主要インデックス"))
    for ticker, name in INDICES:
        d = get_stock_data(ticker)
        if d and "error" not in d:
            chg = d["chg_pct"]
            line = f"{name}  {format_price(ticker, d['price'])}  {'+' if chg>=0 else ''}{chg:.2f}%  {d['signal']}"
            blocks.append(bul(line))
            portfolio_lines.append(f"[インデックス] {line}")
            print(f"  {line}")
    blocks.append(divider())

    # 保有株
    blocks.append(h2("🇯🇵 保有株"))
    blocks.append(callout("52W位置: 0%=年初来最安値 / 100%=最高値　🟢割安(〜30%) 🟡適正 🔴高値(65%〜)", "📌"))
    for ticker, name, shares, cost in HOLDINGS:
        d    = get_stock_data(ticker)
        line = holding_line(ticker, name, shares, cost, d)
        blocks.append(bul(line))
        portfolio_lines.append(f"[保有] {line}")
        print(f"  {line}")
        if d and "error" not in d:
            gain_pct = (d["price"] - cost) / cost * 100
            holdings_data.append((name, d["div_yield"], shares, d["price"], gain_pct))
            if d["div_yield"] > 0:
                total_div += d["price"] * shares * d["div_yield"] / 100

        # 銘柄ニュース
        ticker_news = get_ticker_news(name)
        if ticker_news:
            for title, link in ticker_news:
                blocks.append(bul(f"  📄 {title}", link or None))
                news_lines.append(f"{name}: {title}")
        time.sleep(0.3)
    blocks.append(divider())

    # 配当サマリー
    blocks.append(h2("💰 配当キャッシュフロー（個別株・ETF）"))
    blocks.append(callout(f"年間配当収入（概算）: ¥{total_div:,.0f}　※投資信託除く", "💴"))
    over3  = [(n,dy,s,p,g) for n,dy,s,p,g in holdings_data if dy >= 3.0]
    under3 = [(n,dy,s,p,g) for n,dy,s,p,g in holdings_data if 0 < dy < 3.0]
    if over3:
        blocks.append(para("✅ 目標達成（3%以上）"))
        for n,dy,s,p,g in sorted(over3, key=lambda x:-x[1]):
            mark = "🏆" if dy >= 4.0 else "✅"
            blocks.append(bul(f"{mark} {n}  配当{dy:.1f}%  年間{p*s*dy/100:,.0f}円"))
    if under3:
        blocks.append(para("❌ 目標未達（3%未満）"))
        for n,dy,s,p,g in sorted(under3, key=lambda x:-x[1]):
            loss_note = " ⚠️含み損" if g < 0 else ""
            blocks.append(bul(f"❌ {n}  配当{dy:.1f}%  年間{p*s*dy/100:,.0f}円{loss_note}"))
    blocks.append(divider())

    # 新規検討銘柄
    blocks.append(h2("🎯 新規購入検討銘柄"))
    blocks.append(callout("🟢割安圏 + 配当✅✅は積極検討候補", "⚠️"))
    for ticker, name in WATCHLIST:
        d    = get_stock_data(ticker)
        line = watch_line(ticker, name, d)
        blocks.append(bul(line))
        portfolio_lines.append(f"[検討] {line}")
        print(f"  [検討] {line}")
        ticker_news = get_ticker_news(name, max_items=2)
        for title, link in ticker_news:
            blocks.append(bul(f"  📄 {title}", link or None))
            news_lines.append(f"{name}: {title}")
        time.sleep(0.3)
    blocks.append(divider())

    # 監視銘柄
    blocks.append(h2("👀 監視銘柄"))
    for ticker, name in MONITOR:
        d    = get_stock_data(ticker)
        line = watch_line(ticker, name, d)
        blocks.append(bul(line))
        portfolio_lines.append(f"[監視] {line}")
        print(f"  [監視] {line}")
        ticker_news = get_ticker_news(name, max_items=2)
        for title, link in ticker_news:
            blocks.append(bul(f"  📄 {title}", link or None))
            news_lines.append(f"{name}: {title}")
        time.sleep(0.3)
    blocks.append(divider())

    # AIアドバイス（一番上に挿入するため後から先頭に追加）
    print("\n  Claude AIアドバイス生成中...")
    portfolio_summary = "\n".join(portfolio_lines[:40])
    news_summary      = "\n".join(news_lines[:20]) if news_lines else "ニュース取得なし"
    advice_text       = generate_advice(portfolio_summary, news_summary)
    print(f"  アドバイス生成完了")

    advice_blocks = [
        h2("🤖 今日のAIアドバイス"),
        callout(advice_text, "🤖"),
        divider(),
    ]
    blocks = advice_blocks + blocks

    url = create_page(f"📈 {date_str} 株式レポート", blocks, "📈")
    print(f"\n株式ページ完了: {url}")
    return url

# ── ニュースページ ────────────────────────────────
def create_news_page(date_str):
    print("\n==== ニュースダイジェスト作成中 ====")
    blocks       = []
    all_news_for_digest = []

    # 日経新聞スクレイピング
    print("  📰 日経新聞取得中...")
    nikkei_items = get_nikkei_news(max_items=10)
    if nikkei_items:
        for title, _ in nikkei_items:
            all_news_for_digest.append(f"日経新聞: {title}")

    # 新潟日報スクレイピング
    print("  📰 新潟日報取得中...")
    niigata_nippo_items = get_niigata_nippo_news(max_items=8)
    if niigata_nippo_items:
        for title, _ in niigata_nippo_items:
            all_news_for_digest.append(f"新潟日報: {title}")

    # 全ニュース収集（RSS）
    category_news = {}
    for category, urls in NEWS_FEEDS.items():
        print(f"  {category}...")
        items = get_rss_news(urls)
        category_news[category] = items
        for title, _ in items:
            all_news_for_digest.append(f"{category}: {title}")

    # AIダイジェストを冒頭に
    if all_news_for_digest:
        print("  AIダイジェスト生成中...")
        digest = generate_news_digest("\n".join(all_news_for_digest))
        blocks.append(h2("🗞️ 今日のニュース ダイジェスト（AI要約）"))
        blocks.append(callout(digest, "🗞️"))
        blocks.append(divider())

    # 日経新聞ピックアップ（ログイン成功時のみ表示）
    if nikkei_items:
        blocks.append(h2("📰 日経新聞ピックアップ"))
        for title, link in nikkei_items:
            blocks.append(bul(title, link or None))
        blocks.append(divider())
    elif os.environ.get("NIKKEI_EMAIL"):
        blocks.append(h2("📰 日経新聞ピックアップ"))
        blocks.append(para("ログインに失敗しました。メールアドレス・パスワードを確認してください。"))
        blocks.append(divider())

    # カテゴリ別詳細
    blocks.append(h2("📋 カテゴリ別詳細"))
    for category, items in category_news.items():
        blocks.append(h3(category))
        # 新潟カテゴリは新潟日報を先頭に追加
        if category == "🌾 新潟・地域経済" and niigata_nippo_items:
            blocks.append(para("📰 新潟日報"))
            for title, link in niigata_nippo_items:
                blocks.append(bul(title, link or None))
            if items:
                blocks.append(para("📡 その他（Google News）"))
        elif category == "🌾 新潟・地域経済" and os.environ.get("NIIGATA_NIPPO_EMAIL"):
            blocks.append(para("⚠️ 新潟日報ログイン失敗。メールアドレス・パスワードを確認してください。"))
        if items:
            for title, link in items:
                blocks.append(bul(title, link or None))
        elif category != "🌾 新潟・地域経済" or not niigata_nippo_items:
            blocks.append(para("ニュース取得できませんでした"))
    blocks.append(divider())

    url = create_page(f"📰 {date_str} ニュースダイジェスト", blocks, "📰")
    print(f"ニュースページ完了: {url}")
    return url

# ── メイン ────────────────────────────────────────
def main():
    JST = timezone(timedelta(hours=9))
    date_str = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"\n{date_str} 日報作成開始\n")
    stock_url = create_stock_page(date_str)
    news_url  = create_news_page(date_str)
    print(f"\n=== 完了 ===")
    print(f"株式 : {stock_url}")
    print(f"ニュース : {news_url}")

if __name__ == "__main__":
    main()
