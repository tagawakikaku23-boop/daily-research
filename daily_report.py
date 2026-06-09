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
        # 週間騰落率・高値からの下落率も計算するため少し長めに取得
        hist = t.history(period="1mo")
        if len(hist) < 1:
            return None

        closes  = hist["Close"]
        price   = closes.iloc[-1]
        prev    = closes.iloc[-2] if len(closes) >= 2 else price
        chg_pct = (price - prev) / prev * 100

        # 週間騰落率（直近5営業日前との比較）
        week_ago    = closes.iloc[-6] if len(closes) >= 6 else closes.iloc[0]
        week_chg    = (price - week_ago) / week_ago * 100 if week_ago else 0
        # 直近高値からの下落率（過去1ヶ月のピーク対比）
        recent_high = closes.max()
        drop_high   = (price - recent_high) / recent_high * 100 if recent_high else 0

        # 取得時刻（最新足の日付）
        try:
            asof = closes.index[-1].strftime("%m/%d")
        except Exception:
            asof = ""

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
            "week_chg": week_chg, "drop_high": drop_high, "asof": asof,
        }
    except Exception as e:
        return {"error": str(e)}

# ── マクロ環境スナップショット（yfinanceで機械取得） ──
MACRO_TICKERS = [
    ("^DJI",  "NYダウ"),
    ("^GSPC", "S&P500"),
    ("^IXIC", "NASDAQ"),
    ("^SOX",  "SOX(半導体)"),
]

def get_macro_snapshot():
    """米国市場・SOX・ドル円・日経3日推移を機械取得（出典=Yahoo Finance）"""
    snap = {"us": [], "usdjpy": None, "nikkei_3d": [], "asof": ""}
    JST = timezone(timedelta(hours=9))
    snap["asof"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    # 米国主要指数＋SOX
    for tk, name in MACRO_TICKERS:
        d = get_stock_data(tk)
        if d and "error" not in d:
            snap["us"].append((name, d["price"], d["chg_pct"], d.get("asof", "")))
    # ドル円
    try:
        fx = yf.Ticker("JPY=X").history(period="5d")["Close"]
        if len(fx) >= 1:
            cur = fx.iloc[-1]
            prv = fx.iloc[-2] if len(fx) >= 2 else cur
            snap["usdjpy"] = (cur, (cur - prv) / prv * 100)
    except Exception:
        pass
    # 日経 直近3営業日の終値推移＋前日比（円・%を整合させて1か所で算出）
    try:
        nk = yf.Ticker("^N225").history(period="10d")["Close"]
        for i in range(max(0, len(nk) - 3), len(nk)):
            snap["nikkei_3d"].append((nk.index[i].strftime("%m/%d"), nk.iloc[i]))
        if len(nk) >= 2:
            cur, prv = nk.iloc[-1], nk.iloc[-2]
            snap["nikkei"] = {
                "close": cur, "chg_yen": cur - prv,
                "chg_pct": (cur - prv) / prv * 100,
                "asof": nk.index[-1].strftime("%m/%d"),
            }
    except Exception:
        pass
    return snap

def get_usdjpy_rate():
    """米国ETF円換算用のドル円レート"""
    try:
        fx = yf.Ticker("JPY=X").history(period="5d")["Close"]
        return float(fx.iloc[-1]) if len(fx) >= 1 else None
    except Exception:
        return None

# ── マクロ「理由」ニュース（Google News RSS・出典付き） ──
def get_macro_news(mode="morning", max_items=8):
    """相場が動いた理由を探すためのニュース見出しを出典付きで収集"""
    if mode == "morning":
        queries = [
            "日経平均 今日 見通し", "米国株 ダウ ナスダック 終値",
            "ドル円 為替 今日", "今週 経済指標 スケジュール 日米",
        ]
    else:
        queries = [
            "日経平均 今日 終値 理由", "東証 今日 値上がり 値下がり セクター",
            "ドル円 今日 終値", "明日 注目 経済指標",
        ]
    import urllib.parse
    items, seen = [], set()
    for q in queries:
        try:
            url  = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=ja&gl=JP&ceid=JP:ja"
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "")
                src   = entry.get("source", {}).get("title", "")
                if title and not is_noise_news(title) and title not in seen:
                    seen.add(title)
                    items.append((f"[{src}] {title}" if src else title, link))
        except Exception:
            continue
    return items[:max_items]

def macro_snapshot_text(snap):
    """マクロスナップショットを人間/AIが読めるテキストに整形"""
    lines = [f"取得時刻: {snap.get('asof','')}"]
    for name, price, chg, _asof in snap.get("us", []):
        lines.append(f"{name}: {price:,.2f} ({'+' if chg>=0 else ''}{chg:.2f}%)")
    if snap.get("usdjpy"):
        cur, chg = snap["usdjpy"]
        lines.append(f"ドル円: {cur:.2f} ({'+' if chg>=0 else ''}{chg:.2f}%)")
    if snap.get("nikkei"):
        nk = snap["nikkei"]
        sign = "+" if nk["chg_yen"] >= 0 else ""
        lines.append(f"日経平均 終値({nk['asof']}): {nk['close']:,.0f}円 "
                     f"前日比{sign}{nk['chg_yen']:,.0f}円 ({sign}{nk['chg_pct']:.2f}%)")
    if snap.get("nikkei_3d"):
        seq = " → ".join(f"{day} {v:,.0f}" for day, v in snap["nikkei_3d"])
        lines.append(f"日経平均 直近推移: {seq}")
    return "\n".join(lines)

# ── 差分（前回レポートとの比較）スナップショット ─────────
SNAP_FILE = Path(__file__).parent / "last_snapshot.json"

def load_prev_snapshot():
    try:
        import json
        return json.loads(SNAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_snapshot(snap):
    try:
        import json
        SNAP_FILE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def diff_lines(prev, cur):
    """前回と今回の銘柄シグナルを比較して変化点を返す"""
    out = []
    for name, sig in cur.items():
        old = prev.get(name)
        if old and old != sig:
            out.append(f"{name}: {old} → {sig}")
    return out

# ── ニュース選別ルール（指示書セクション4／第2弾②） ───────
# 株価に影響しないノイズ（スポーツ・イベント・CSR等）は除外
NEWS_EXCLUDE = [
    # スポーツ全般
    "スポーツ", "野球", "プロ野球", "サッカー", "Jリーグ", "ゴルフ", "テニス",
    "バスケ", "バレー", "陸上", "水泳", "ラグビー", "五輪", "オリンピック",
    # 野球・試合まわりの語（球団名一致対策で強めに）
    "監督", "選手", "試合", "打席", "投手", "登板", "好投", "完投", "完封",
    "打線", "打者", "本塁打", "ホームラン", "安打", "失点", "無失点", "防御率",
    "勝利投手", "敗戦", "サヨナラ", "ドラフト", "スタジアム", "ファインプレー",
    "本拠地", "球団", "ユニホーム", "ユニフォーム", "開幕", "シーズン", "ナイン",
    "夏の陣", "交流戦", "クライマックス", "日本シリーズ", "リーグ優勝",
    # ゴルフ・順位
    "ツアー", "プレーオフ", "予選通過", "首位", "優勝", "連敗", "連勝", "敗退",
    # イベント・CSR
    "スポンサー", "コラボ", "イベント", "寄付", "CSR", "チャリティ", "ファンクラブ",
    "コンサート", "ライブ", "握手会", "グッズ", "ファンミーティング", "ファン感謝",
    "甲子園", "アルビ", "観戦", "始球式", "応援",
]

# 球団・スポーツチームを持つ企業＝社名一致でスポーツ記事が大量に混入する。
# これらはホワイトリスト（業績・配当・M&A等）に該当する記事のみ採用する。
SPORTS_HEAVY = ["オリックス", "ソフトバンク", "楽天", "DeNA", "日本ハム",
                "阪神", "中日", "巨人", "ヤクルト", "西武", "ロッテ"]
# 配当・業績を脅かす重大リスク（評価に必ず反映）
NEWS_RISK = {
    "下方修正": "業績下方修正", "赤字": "赤字", "減益": "減益",
    "減配": "減配", "無配": "無配", "引当金": "引当金計上",
    "訴訟": "訴訟", "提訴": "訴訟", "不祥事": "不祥事",
    "行政処分": "行政処分", "業務改善命令": "行政処分", "課徴金": "課徴金",
    "談合": "談合疑い", "カルテル": "カルテル", "調査委": "調査委員会設置",
    "第三者委員会": "第三者委員会", "格下げ": "格下げ", "目標株価引き下げ": "目標株価引下げ",
    "リコール": "リコール", "粉飾": "粉飾", "破綻": "経営不安",
}
# 株価にプラスの好材料
NEWS_POSITIVE = {
    "上方修正": "業績上方修正", "増配": "増配", "自社株買い": "自社株買い",
    "最高益": "最高益", "増益": "増益", "格上げ": "格上げ",
    "目標株価引き上げ": "目標株価引上げ", "TOB": "TOB", "提携": "資本提携",
}

def is_noise_news(title):
    return any(kw in title for kw in NEWS_EXCLUDE)

def classify_news(title):
    """ニュース見出しからリスク/好材料フラグを抽出"""
    risks = [label for kw, label in NEWS_RISK.items() if kw in title]
    goods = [label for kw, label in NEWS_POSITIVE.items() if kw in title]
    return risks, goods

# ── 銘柄ニュース取得（Google News日本語RSS／ノイズ除外） ──
def get_ticker_news(company_name, max_items=3):
    """Google NewsのRSSで日本語ニュースを取得。ノイズ記事は除外し、
    リスク/好材料フラグも併せて返す。
    戻り値: (items, risk_flags, good_flags)
      items = [(label, link), ...]
    """
    try:
        import urllib.parse
        clean = company_name.replace("★連続増配", "").replace("★34年連続増配", "")
        clean = clean.replace("★23年連続増配", "").replace("★連続増配", "").strip()
        # ニュース精度向上のため「決算 OR 配当 OR 業績」等で軽く絞る
        # 球団・スポーツチームを持つ企業は、決算系の語で検索を絞ってノイズを減らす
        sports_heavy = any(s in clean for s in SPORTS_HEAVY)
        if sports_heavy:
            query = urllib.parse.quote(f"{clean} (決算 OR 配当 OR 業績 OR 株)")
        else:
            query = urllib.parse.quote(clean)
        url   = f"https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
        feed  = feedparser.parse(url)
        items, all_risks, all_goods = [], [], []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link  = entry.get("link", "")
            src   = entry.get("source", {}).get("title", "")
            if not title or is_noise_news(title):
                continue
            risks, goods = classify_news(title)
            # 球団保有企業は、業績・配当・M&A等のホワイトリスト該当記事のみ採用
            if sports_heavy and not (risks or goods):
                continue
            all_risks += risks
            all_goods += goods
            label = f"[{src}] {title}" if src else title
            items.append((label, link))
            if len(items) >= max_items:
                break
        return items, sorted(set(all_risks)), sorted(set(all_goods))
    except Exception:
        return [], [], []

# ── 買い/様子見/慎重 判定（ニュース連動・指示書セクション5） ──
def trade_signal(position, div_yield, gain_pct=None, risk_flags=None, good_flags=None):
    """52W位置・配当・損益＋当日ニュースから総合判定を返す。
    重大リスク（赤字・下方修正・不祥事・訴訟等）があれば機械的な
    『買い』『継続保有』を出さず、必ず判断保留に上書きする。"""
    risk_flags = risk_flags or []
    good_flags = good_flags or []

    # ── リスクによる上書き（最優先）──
    if risk_flags:
        tag = "・".join(risk_flags[:3])
        # 配当の根幹を脅かすもの → 除外/保留
        severe = {"業績下方修正", "赤字", "減配", "無配", "引当金計上",
                  "不祥事", "行政処分", "談合疑い", "調査委員会設置",
                  "第三者委員会", "粉飾", "経営不安"}
        if any(r in severe for r in risk_flags):
            return f"⛔ 判断保留（{tag}のニュースあり・結果確認まで様子見）"
        return f"🔴 注意（{tag}のニュースあり）"

    # 含み損かつ配当低い → 要検討
    if gain_pct is not None and gain_pct < -15 and div_yield < 3.0:
        base = "🔴 要検討（含み損・低配当）"
    # 割安圏 + 高配当
    elif position < 0.30 and div_yield >= 4.0:
        base = "🟢 強い買い検討（割安＋高配当）"
    elif position < 0.35 and div_yield >= 3.0:
        base = "🟢 買い検討（割安＋配当良好）"
    elif position < 0.40 and div_yield >= 3.0:
        base = "🟢 買い検討圏"
    # 高値圏
    elif position > 0.80:
        base = "🔴 追加購入は慎重（高値圏）"
    elif position > 0.65:
        base = "🟡 様子見（やや高値）"
    # 適正圏
    elif div_yield >= 3.0:
        base = "🟡 継続保有（配当良好）"
    else:
        base = "🟡 様子見"

    # 好材料の付記（評価は据え置き、コメントだけ補強）
    if good_flags:
        base += f"　＋{('・'.join(good_flags[:2]))}"
    return base

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

        # 複数カテゴリページを巡回
        target_urls = [
            "https://www.nikkei.com/",
            "https://www.nikkei.com/economy/",
            "https://www.nikkei.com/markets/",
            "https://www.nikkei.com/business/",
        ]

        items     = []
        seen_urls = set()

        for url in target_urls:
            resp  = session.get(url, timeout=15)
            soup2 = BeautifulSoup(resp.text, "html.parser")
            for a in soup2.find_all("a", href=True):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if ("/article/" in href or "/nkd/issue/" in href) and len(text) > 8:
                    full_url = href if href.startswith("http") else "https://www.nikkei.com" + href
                    # URLで重複排除（テキストの表記ゆれを無視）
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        items.append((text, full_url))
            if len(items) >= max_items:
                break

        return items[:max_items]
    except Exception as e:
        print(f"  日経スクレイピング失敗: {e}")
        return []

# ── 新潟日報スクレイピング ────────────────────────
def get_niigata_nippo_news(max_items=8):
    """新潟日報にPlaywrightでログインして地域経済ニュースを取得"""
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
        from playwright.sync_api import sync_playwright
        from bs4 import BeautifulSoup

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page()
            page.set_extra_http_headers({"Accept-Language": "ja-JP,ja;q=0.9"})

            print("  新潟日報: ページ読み込み中...")
            page.goto("https://www.niigata-nippo.co.jp/", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # ログインボタンを探してクリック（モーダルを開く）
            for selector in ["text=ログイン", "a[href*='login']", ".login", "#login-btn", "button:has-text('ログイン')"]:
                try:
                    page.click(selector, timeout=3000)
                    print(f"  ログインボタン発見: {selector}")
                    break
                except Exception:
                    continue

            page.wait_for_timeout(2000)

            # メールアドレス入力
            for sel in ["input[type='email']", "input[name='email']", "input[name='userId']", "input[placeholder*='メール']"]:
                try:
                    page.fill(sel, email, timeout=3000)
                    print(f"  メール欄発見: {sel}")
                    break
                except Exception:
                    continue

            # パスワード入力
            for sel in ["input[type='password']", "input[name='password']"]:
                try:
                    page.fill(sel, password, timeout=3000)
                    print(f"  パスワード欄発見: {sel}")
                    break
                except Exception:
                    continue

            # 送信
            for sel in ["button[type='submit']", "input[type='submit']", "button:has-text('ログイン')", "button:has-text('サインイン')"]:
                try:
                    page.click(sel, timeout=3000)
                    print(f"  送信ボタン発見: {sel}")
                    break
                except Exception:
                    continue

            page.wait_for_timeout(3000)
            print(f"  送信後URL: {page.url}")

            # auth.niigata-nippo.co.jp にリダイレクトされた場合（2段階認証フロー）
            if "auth.niigata-nippo.co.jp" in page.url:
                print("  認証サブドメインを検出、2段階目の入力を試みます...")
                for sel in ["input[type='email']", "input[name='email']", "input[name='username']", "input[name='userId']"]:
                    try:
                        if page.locator(sel).count() > 0:
                            page.fill(sel, email, timeout=3000)
                            print(f"  認証ページ メール欄: {sel}")
                            break
                    except Exception:
                        continue

                for sel in ["input[type='password']", "input[name='password']"]:
                    try:
                        if page.locator(sel).count() > 0:
                            page.fill(sel, password, timeout=3000)
                            print(f"  認証ページ パスワード欄: {sel}")
                            break
                    except Exception:
                        continue

                for sel in ["button[type='submit']", "input[type='submit']", "button:has-text('ログイン')", "button:has-text('次へ')"]:
                    try:
                        page.click(sel, timeout=3000)
                        print(f"  認証ページ 送信: {sel}")
                        break
                    except Exception:
                        continue

                page.wait_for_timeout(4000)
                print(f"  認証完了後URL: {page.url}")

            print(f"  ログイン後URL: {page.url}")

            # 記事収集
            items     = []
            seen_urls = set()
            target_urls = [
                "https://www.niigata-nippo.co.jp/economy/",
                "https://www.niigata-nippo.co.jp/",
            ]

            import re
            for url in target_urls:
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                soup = BeautifulSoup(page.content(), "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    text = a.get_text(strip=True)
                    if len(text) < 8 or any(kw in text for kw in EXCLUDE):
                        continue
                    # カテゴリ・タグページを除外、記事URLのみ（数字IDを含むパス）
                    if "/category/" in href or "/tag/" in href or "/author/" in href:
                        continue
                    is_article = bool(re.search(r'/\d{5,}', href)) or "/articles/" in href
                    if not is_article:
                        continue
                    full_url = href if href.startswith("http") else "https://www.niigata-nippo.co.jp" + href
                    if "niigata-nippo.co.jp" in full_url and full_url not in seen_urls:
                        seen_urls.add(full_url)
                        items.append((text, full_url))
                    if len(items) >= max_items:
                        break
                if len(items) >= max_items:
                    break

            # 記事が取れなかった場合はトップページのhref全体をデバッグ出力
            if not items:
                page.goto("https://www.niigata-nippo.co.jp/", timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                soup = BeautifulSoup(page.content(), "html.parser")
                sample = [a.get("href","") for a in soup.find_all("a", href=True) if len(a.get("href","")) > 10][:20]
                print(f"  新潟日報サンプルURL: {sample}")

            browser.close()
            print(f"  新潟日報: {len(items)}件取得")
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

# ── マクロ環境のAI解説（朝/夜で役割を変える） ──────────
def generate_macro_analysis(macro_text, macro_news, mode="morning", today_str=""):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        news_text = "\n".join(f"・{t}" for t, _ in macro_news) or "（ニュース取得なし）"
        if mode == "morning":
            role = ("今日これから動く相場の戦略視点で、前日の米国市場・ドル円・"
                    "日経の直近推移を踏まえ『なぜ今この相場なのか』を解説。"
                    "必ず直近3日でいくら動いたかとその理由に言及。"
                    "『分散が大事』等の一般論で終わらせない。")
        else:
            role = ("今日の結果の振り返り視点で、今日の日経・ドル円がどう動いたか、"
                    "その主因（金利・為替・決算・地政学・セクター物色）を特定して解説。"
                    "どのセクターが買われ/売られたかにも触れる。")
        prompt = f"""あなたは日本株の市況に詳しいアナリストです。本日は {today_str} です。{role}

【マクロ数値（Yahoo Finance, 出典確実）】
{macro_text}

【関連ニュース見出し（Google News）】
{news_text}

厳守ルール：
- 株価・指数・為替の具体的な数字は、上の【マクロ数値】に書かれた値のみを使う。
  自分で別の数字（前日比○円安など）を創作しない。日経の前日比は提供値をそのまま使う。
- 経済イベントは必ず「○月○日」と日付を添える。本日({today_str})より後の予定だけを
  「今後の注目」として扱い、過去の発表（例：先週の雇用統計）は「発表済み」と明記する。
  「明日」「来週」などの相対表現は使わず実日付に直す。

事実とニュースに基づき憶測を避け、4〜6行で簡潔に。同じ言い回しの繰り返しを避けること。"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        return r.choices[0].message.content
    except Exception as e:
        return f"マクロ解説生成失敗: {e}"

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

# ── 日経ニュース専用AIコメント ────────────────────
def generate_nikkei_analysis(nikkei_headlines):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        headlines_text = "\n".join(f"・{t}" for t, _ in nikkei_headlines)
        prompt = f"""あなたは経済・政治・国際情勢に詳しいジャーナリスト兼アナリストです。
以下は今日の日経新聞のヘッドラインです。投資目線だけでなく、時事・政治・経済・国際情勢を幅広くカバーした読み解きをしてください。

【今日の日経新聞ヘッドライン】
{headlines_text}

以下の項目ごとに日本語で詳しく解説してください（各項目3〜5行、読み応えある内容で）：

1. 🗞️ 今日の最重要ニュース（政治・経済・社会問わず、今日最も注目すべき話題）
2. 🇯🇵 日本経済の動き（景気・企業・雇用・産業政策など国内経済トピック）
3. 🌏 世界経済・国際情勢（米中関係・地政学リスク・各国経済の動向）
4. 🏛️ 政治・政策の読み方（日本・海外の政治動向、政策変化が経済に与える影響）
5. 📈 マーケット・資産形成への示唆（金利・為替・株式市場への影響、個人として意識すべき点）
6. 💬 今日の一言まとめ（全体を通じた今日のキーメッセージを2〜3行で）"""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"日経AIコメント生成失敗: {e}"

# ── ニュースダイジェスト生成 ─────────────────────
def generate_news_digest(all_news_text):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""以下は今日の各カテゴリのニュース一覧です。
個人事業主・投資家・時事に関心がある人向けに、今日の重要ニュースを幅広く取り上げて日本語で解説してください。
経済・政治・国際・テクノロジー・地域ニュースをバランスよくカバーしてください。

【今日のニュース一覧】
{all_news_text}

以下の形式で25〜30点を取り上げてください。各項目は2〜3行で背景・意味・影響まで踏み込んで解説すること。

• [カテゴリ] 見出し
  → 解説文（2〜3行）

• [カテゴリ] 見出し
  → 解説文（2〜3行）

（25〜30点）"""
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
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

def is_usd_ticker(ticker):
    return not (".T" in ticker or ticker.startswith("^N") or ticker.startswith("^T"))

def _wk_dh_str(d):
    """週間騰落率・高値からの下落率の表示文字列"""
    parts = []
    if d.get("week_chg") is not None:
        w = d["week_chg"]; parts.append(f"週{'+' if w>=0 else ''}{w:.1f}%")
    if d.get("drop_high") is not None and d["drop_high"] < -0.05:
        parts.append(f"高値比{d['drop_high']:.1f}%")
    return "  ".join(parts)

def holding_line(ticker, name, shares, cost, d, risk_flags=None, good_flags=None, usdjpy=1):
    if not d or "error" in d:
        return f"{name}  データ取得失敗"
    price = d["price"]
    # 取得単価(cost)は円換算済みで登録されているため、米国ETFは現在値も円換算して揃える
    if is_usd_ticker(ticker):
        price_for_gain = price * (usdjpy or 1)
        price_disp     = f"¥{price_for_gain:,.0f}(${price:,.2f})"
    else:
        price_for_gain = price
        price_disp     = format_price(ticker, price)
    chg_str  = f"+{d['chg_pct']:.2f}%" if d["chg_pct"] >= 0 else f"{d['chg_pct']:.2f}%"
    gain_pct = (price_for_gain - cost) / cost * 100
    gain_str = f"+{gain_pct:.1f}%" if gain_pct >= 0 else f"{gain_pct:.1f}%"
    try:
        per_str = f"PER:{float(d['per']):.1f}" if d["per"] else ""
    except (ValueError, TypeError):
        per_str = ""
    asof   = f"({d.get('asof','')}時点)" if d.get("asof") else ""
    wkdh   = _wk_dh_str(d)
    signal = trade_signal(d["position"], d["div_yield"], gain_pct, risk_flags, good_flags)
    return (f"{name}  {price_disp}{asof}  前日比{chg_str}  "
            f"取得比{gain_str}  52W:{int(d['position']*100)}%  {wkdh}  "
            f"{div_label(d['div_yield'])}  {per_str}  → {signal}")

def watch_line(ticker, name, d, risk_flags=None, good_flags=None):
    if not d or "error" in d:
        return f"{name}  データ取得失敗"
    chg_str = f"+{d['chg_pct']:.2f}%" if d["chg_pct"] >= 0 else f"{d['chg_pct']:.2f}%"
    try:
        per_str = f"PER:{float(d['per']):.1f}" if d["per"] else ""
    except (ValueError, TypeError):
        per_str = ""
    asof   = f"({d.get('asof','')}時点)" if d.get("asof") else ""
    wkdh   = _wk_dh_str(d)
    signal = trade_signal(d["position"], d["div_yield"], None, risk_flags, good_flags)
    return (f"{name}  {format_price(ticker, d['price'])}{asof}  前日比{chg_str}  "
            f"52W:{int(d['position']*100)}%  {wkdh}  "
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

def long_text_blocks(text, emoji=None):
    """2000文字を超えるテキストを複数のブロックに分割して返す"""
    blocks = []
    # 行ごとに分割して2000文字以内にまとめる
    lines   = text.splitlines(keepends=True)
    chunk   = ""
    first   = True
    for line in lines:
        if len(chunk) + len(line) > 1900:
            if chunk.strip():
                if first and emoji:
                    blocks.append(callout(chunk.strip(), emoji))
                    first = False
                else:
                    blocks.append(para(chunk.strip()))
            chunk = line
        else:
            chunk += line
    if chunk.strip():
        if first and emoji:
            blocks.append(callout(chunk.strip(), emoji))
        else:
            blocks.append(para(chunk.strip()))
    return blocks

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

# ── 夜の「明日の注目＋ひとことメモ」AI生成 ─────────────
def generate_evening_memo(macro_text, movers_text, today_str=""):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""あなたは長期インカム（高配当・連続増配）投資家に寄り添うアドバイザーです。本日は {today_str} です。

【今日のマクロ】
{macro_text}

【今日大きく動いた保有/候補銘柄】
{movers_text or "特になし"}

以下を日本語で簡潔に：
1. 🔭 明日以降の注目ポイント（米国市場・指標・決算など見るべき1〜2点）
   ※必ず「○月○日」と日付を添え、本日({today_str})より後の予定のみ挙げる。
     過去に発表済みのイベント（例：先週の雇用統計）は書かない。「明日」等の相対表現は実日付に直す。
2. 💬 ひとことメモ（長期インカム投資家としての心構えを2〜3行。淡々と・分割で・狼狽売りしない等。毎回同じ言い回しは避ける）"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
        )
        return r.choices[0].message.content
    except Exception as e:
        return f"夜メモ生成失敗: {e}"

# ── ポートフォリオ一括収集（朝夜で共有） ───────────────
def collect_portfolio(usdjpy):
    """全銘柄の株価・ニュース・リスクフラグをまとめて取得。
    戻り値 dict: holdings/watch/monitor の各リスト＋signals辞書＋total_div"""
    data = {"holdings": [], "watch": [], "monitor": [],
            "signals": {}, "total_div": 0.0, "news_lines": [], "portfolio_lines": []}

    for ticker, name, shares, cost in HOLDINGS:
        d = get_stock_data(ticker)
        news, risks, goods = get_ticker_news(name, max_items=3)
        line = holding_line(ticker, name, shares, cost, d, risks, goods, usdjpy)
        data["holdings"].append((ticker, name, shares, cost, d, news, risks, goods, line))
        data["portfolio_lines"].append(f"[保有] {line}")
        data["signals"][name] = trade_signal(
            d["position"], d["div_yield"], (d["price"]-cost)/cost*100, risks, goods
        ) if d and "error" not in d else "データなし"
        for t, _ in news:
            data["news_lines"].append(f"{name}: {t}")
        # 配当キャッシュフロー（米国ETFはドル円で円換算）
        if d and "error" not in d and d["div_yield"] > 0:
            px = d["price"] * (usdjpy or 1) if is_usd_ticker(ticker) else d["price"]
            data["total_div"] += px * shares * d["div_yield"] / 100
        time.sleep(0.3)

    for ticker, name in WATCHLIST:
        d = get_stock_data(ticker)
        news, risks, goods = get_ticker_news(name, max_items=2)
        line = watch_line(ticker, name, d, risks, goods)
        data["watch"].append((ticker, name, d, news, risks, goods, line))
        data["portfolio_lines"].append(f"[検討] {line}")
        data["signals"][name] = trade_signal(d["position"], d["div_yield"], None, risks, goods) \
            if d and "error" not in d else "データなし"
        for t, _ in news:
            data["news_lines"].append(f"{name}: {t}")
        time.sleep(0.3)

    for ticker, name in MONITOR:
        d = get_stock_data(ticker)
        news, risks, goods = get_ticker_news(name, max_items=1)
        line = watch_line(ticker, name, d, risks, goods)
        data["monitor"].append((ticker, name, d, news, risks, goods, line))
        data["portfolio_lines"].append(f"[監視] {line}")
        time.sleep(0.3)

    return data

def dividend_blocks(holdings, usdjpy, total_div):
    """配当キャッシュフローのブロックを生成（ETF円換算済み）"""
    blocks = [h2("💰 配当キャッシュフロー（個別株・ETF）"),
              callout(f"年間配当収入（概算）: ¥{total_div:,.0f}　※投資信託除く／米ETFはドル円{usdjpy:.1f}で円換算", "💴")]
    rows = []
    for ticker, name, shares, cost, d, *_ in holdings:
        if not d or "error" in d or d["div_yield"] <= 0:
            continue
        px = d["price"] * usdjpy if is_usd_ticker(ticker) else d["price"]
        rows.append((name, d["div_yield"], px * shares * d["div_yield"] / 100,
                     (d["price"]-cost)/cost*100))
    over3  = [r for r in rows if r[1] >= 3.0]
    under3 = [r for r in rows if 0 < r[1] < 3.0]
    if over3:
        blocks.append(para("✅ 目標達成（3%以上）"))
        for n, dy, yen, g in sorted(over3, key=lambda x:-x[1]):
            blocks.append(bul(f"{'🏆' if dy>=4.0 else '✅'} {n}  配当{dy:.1f}%  年間{yen:,.0f}円"))
    if under3:
        blocks.append(para("❌ 目標未達（3%未満）"))
        for n, dy, yen, g in sorted(under3, key=lambda x:-x[1]):
            blocks.append(bul(f"❌ {n}  配当{dy:.1f}%  年間{yen:,.0f}円{' ⚠️含み損' if g<0 else ''}"))
    return blocks

def deep_dive_questions(data):
    """末尾の深掘り想定質問×3（リスクが出た銘柄を優先）"""
    qs = []
    for h in data["holdings"]:
        name, risks = h[1], h[6]
        if risks:
            qs.append(f"{name}の{('・'.join(risks))}は減配につながる？継続保有でいい？")
        if len(qs) >= 2:
            break
    qs.append("今のドル円水準で米国ETF(VOO/SPYD)を買い増すのは妥当？")
    qs.append("NISA成長投資枠が余っていれば、どの高配当株を優先すべき？")
    return qs[:3]

# ── 朝レポート（07:00）────────────────────────────
def create_morning_page(date_str, now_str, title, icon):
    print("\n==== 🌅 朝レポート作成中 ====")
    usdjpy = get_usdjpy_rate() or 1
    snap   = get_macro_snapshot()
    macro_news = get_macro_news("morning")
    macro_txt  = macro_snapshot_text(snap)

    blocks = [callout(f"生成: {now_str}（朝＝今日これからの戦略）", "🌅")]

    # ① 今朝のマクロ環境
    blocks.append(h2("① 今朝のマクロ環境"))
    for name, price, chg, asof in snap["us"]:
        blocks.append(bul(f"{name}: {price:,.2f}  ({'+' if chg>=0 else ''}{chg:.2f}%){f'  {asof}時点' if asof else ''}"))
    if snap.get("usdjpy"):
        cur, chg = snap["usdjpy"]
        blocks.append(bul(f"ドル円: {cur:.2f}  ({'+' if chg>=0 else ''}{chg:.2f}%)"))
    if snap.get("nikkei_3d"):
        blocks.append(bul("日経 直近推移: " + " → ".join(f"{day} {v:,.0f}" for day, v in snap["nikkei_3d"])))
    blocks.append(para("出典: Yahoo Finance（数値）／Google News（理由）"))

    # ② 今日の相場観（AI、マクロ＋ニュース連動）
    print("  マクロ解説生成中...")
    macro_ai = generate_macro_analysis(macro_txt, macro_news, "morning", date_str)
    blocks.append(h2("② 今日の相場観"))
    blocks.extend(long_text_blocks(macro_ai, "🧭"))
    for t, link in macro_news[:5]:
        blocks.append(bul(f"📄 {t}", link or None))
    blocks.append(divider())

    # データ収集
    print("  銘柄データ収集中...")
    data = collect_portfolio(usdjpy)

    # 前回からの変化（差分）
    prev = load_prev_snapshot()
    dl   = diff_lines(prev.get("signals", {}), data["signals"])
    blocks.append(h2("📊 前回からの変化"))
    if dl:
        for d in dl[:8]:
            blocks.append(bul(d))
    else:
        blocks.append(para("シグナルの大きな変化はなし（前回と同水準）"))
    blocks.append(divider())

    # ③ 買い場判定（保有＋候補）
    blocks.append(h2("③ 買い場判定（保有＋候補）"))
    blocks.append(callout("52W: 0%=安値〜100%=高値　🟢割安 🟡適正 🔴高値　⛔=重大ニュースで保留", "📌"))
    blocks.append(h3("保有株"))
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        blocks.append(bul(line))
        for t, link in news:
            blocks.append(bul(f"  📄 {t}", link or None))
    blocks.append(h3("新規購入検討"))
    for ticker, name, d, news, risks, goods, line in data["watch"]:
        blocks.append(bul(line))
        for t, link in news:
            blocks.append(bul(f"  📄 {t}", link or None))
    blocks.append(divider())

    # ④⑤⑥ 買い増し助言・新規注目・ポートフォリオ（AI）
    print("  買い増し助言生成中...")
    advice = generate_advice("\n".join(data["portfolio_lines"][:40]),
                             "\n".join(data["news_lines"][:20]) or "ニュースなし")
    blocks.append(h2("④ 今日の買い増し助言・新規注目"))
    blocks.append(callout("予算目安: 1回10〜30万円・一度に使い切らず分割。NISA成長枠が残れば高配当はNISA優先", "💴"))
    blocks.extend(long_text_blocks(advice, "💡"))
    blocks.append(divider())

    # 監視銘柄
    blocks.append(h2("👀 監視銘柄"))
    for ticker, name, d, news, risks, goods, line in data["monitor"]:
        blocks.append(bul(line))
    blocks.append(divider())

    # 配当キャッシュフロー
    blocks.extend(dividend_blocks(data["holdings"], usdjpy, data["total_div"]))
    blocks.append(divider())

    # 深掘り想定質問
    blocks.append(h2("🔎 深掘り用の想定質問"))
    for q in deep_dive_questions(data):
        blocks.append(bul(q))

    # 差分用スナップショット保存
    save_snapshot({"signals": data["signals"], "asof": now_str})

    url = create_page(title, blocks, icon)
    print(f"朝レポート完了: {url}")
    return url

# ── 夜レポート（19:00）────────────────────────────
def create_evening_page(date_str, now_str, title, icon):
    print("\n==== 🌙 夜レポート作成中 ====")
    usdjpy = get_usdjpy_rate() or 1
    snap   = get_macro_snapshot()
    macro_news = get_macro_news("evening")
    macro_txt  = macro_snapshot_text(snap)

    blocks = [callout(f"生成: {now_str}（夜＝今日の結果の振り返り）", "🌙")]

    # ① 今日の市場サマリー（実績）
    blocks.append(h2("① 今日の市場サマリー（実績）"))
    for tk, name in [("^N225","日経平均"), ("^GSPC","S&P500（参考）")]:
        d = get_stock_data(tk)
        if d and "error" not in d:
            blocks.append(bul(f"{name}: {format_price(tk, d['price'])}  前日比{'+' if d['chg_pct']>=0 else ''}{d['chg_pct']:.2f}%  {_wk_dh_str(d)}"))
    if snap.get("usdjpy"):
        cur, chg = snap["usdjpy"]
        blocks.append(bul(f"ドル円: {cur:.2f}  ({'+' if chg>=0 else ''}{chg:.2f}%)"))
    blocks.append(divider())

    # ② 今日動いた要因の分析（夜のメイン）
    print("  要因分析生成中...")
    macro_ai = generate_macro_analysis(macro_txt, macro_news, "evening", date_str)
    blocks.append(h2("② 今日動いた要因の分析"))
    blocks.extend(long_text_blocks(macro_ai, "🔍"))
    for t, link in macro_news[:5]:
        blocks.append(bul(f"📄 {t}", link or None))
    blocks.append(divider())

    # データ収集
    print("  銘柄データ収集中...")
    data = collect_portfolio(usdjpy)

    # 前回からの変化
    prev = load_prev_snapshot()
    dl   = diff_lines(prev.get("signals", {}), data["signals"])
    if dl:
        blocks.append(h2("📊 前回からの変化"))
        for d in dl[:8]:
            blocks.append(bul(d))
        blocks.append(divider())

    # ③ 今日大きく動いた保有/候補銘柄だけ（±2%以上 or 重要ニュースあり）
    blocks.append(h2("③ 今日大きく動いた銘柄"))
    blocks.append(callout("掲載基準: 前日比±2%以上、または重要ニュースあり（その場合※ニュース注目）。値動きの大きい順", "📏"))
    movers, movers_lines = [], []
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if d and "error" not in d and (abs(d["chg_pct"]) >= 2.0 or risks or goods):
            movers.append((abs(d["chg_pct"]), d["chg_pct"], name, news, risks, goods, line))
    for ticker, name, d, news, risks, goods, line in data["watch"]:
        if d and "error" not in d and (abs(d["chg_pct"]) >= 2.0 or risks or goods):
            movers.append((abs(d["chg_pct"]), d["chg_pct"], name, news, risks, goods, line))
    # 値動きの大きい順
    movers.sort(key=lambda x: -x[0])
    if movers:
        for absc, chg, name, news, risks, goods, line in movers:
            note = "　※ニュース注目（値動きは小さい）" if absc < 2.0 else ""
            blocks.append(bul(line + note))
            movers_lines.append(line)
            for t, link in news:
                blocks.append(bul(f"  📄 {t}", link or None))
    else:
        blocks.append(para("本日、±2%以上動いた保有/候補銘柄なし（小動き・重要ニュースもなし）"))
    blocks.append(divider())

    # ④ 保有銘柄に効く重要ニュース（リスク/好材料フラグ付きのみ）
    blocks.append(h2("④ 保有・候補に効く重要ニュース"))
    flagged = False
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if risks or goods:
            flagged = True
            tag = "・".join(risks + goods)
            blocks.append(bul(f"⚠️ {name}: {tag}"))
            for t, link in news:
                if classify_news(t)[0] or classify_news(t)[1]:
                    blocks.append(bul(f"  📄 {t}", link or None))
    if not flagged:
        blocks.append(para("業績・配当・M&A・不祥事に関わる重要ニュースは検出なし"))
    blocks.append(divider())

    # ⑤⑥ 明日の注目＋ひとことメモ
    print("  夜メモ生成中...")
    memo = generate_evening_memo(macro_txt, "\n".join(movers_lines[:8]), date_str)
    blocks.append(h2("⑤ 明日の注目ポイント・ひとことメモ"))
    blocks.extend(long_text_blocks(memo, "🔭"))
    blocks.append(divider())

    # 深掘り想定質問
    blocks.append(h2("🔎 深掘り用の想定質問"))
    for q in deep_dive_questions(data):
        blocks.append(bul(q))

    save_snapshot({"signals": data["signals"], "asof": now_str})

    url = create_page(title, blocks, icon)
    print(f"夜レポート完了: {url}")
    return url

# ── ニュースページ ────────────────────────────────
def create_news_page(date_str, time_str=""):
    print("\n==== ニュースダイジェスト作成中 ====")
    blocks       = []
    all_news_for_digest = []

    # 日経新聞スクレイピング
    print("  📰 日経新聞取得中...")
    nikkei_items = get_nikkei_news(max_items=15)
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
        items = get_rss_news(urls, max_items=10)
        category_news[category] = items
        for title, _ in items:
            all_news_for_digest.append(f"{category}: {title}")

    # AIダイジェストを冒頭に
    if all_news_for_digest:
        print("  AIダイジェスト生成中...")
        digest = generate_news_digest("\n".join(all_news_for_digest))
        blocks.append(h2("🗞️ 今日のニュース ダイジェスト（AI要約）"))
        blocks.extend(long_text_blocks(digest, "🗞️"))
        blocks.append(divider())

    # 日経新聞ピックアップ（ログイン成功時のみ表示）
    if nikkei_items:
        print("  日経AIコメント生成中...")
        nikkei_analysis = generate_nikkei_analysis(nikkei_items)
        blocks.append(h2("📰 日経新聞ピックアップ"))
        blocks.extend(long_text_blocks(nikkei_analysis, "📰"))
        blocks.append(h3("📋 今日の日経ヘッドライン"))
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

    title = f"{date_str} {time_str} ニュースダイジェスト".replace("  ", " ").strip()
    url = create_page(title, blocks, "📰")
    print(f"ニュースページ完了: {url}")
    return url

# ── メイン ────────────────────────────────────────
def resolve_mode(hour):
    """引数 or 現在時刻からモードを決定（morning / evening / manual）"""
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg in ("morning", "朝", "am"):
        return "morning"
    if arg in ("evening", "夜", "pm", "night"):
        return "evening"
    if arg in ("manual", "手動"):
        return "manual"
    # 引数なし → 時刻で自動判定（15時より前=朝、以降=夜）
    return "morning" if hour < 15 else "evening"

def main():
    JST = timezone(timedelta(hours=9))
    now      = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    now_str  = now.strftime("%Y-%m-%d %H:%M")
    mode     = resolve_mode(now.hour)
    print(f"\n{now_str} 日報作成開始（モード: {mode}）\n")

    # タイトルは「日付＋時刻」を必ず入れる。絵文字はページアイコンのみ（タイトル文字列には入れない）
    if mode == "manual":
        # 手動リロード。時間帯に応じた中身を出すが、タイトル/アイコンで手動と区別
        icon  = "🔄"
        title = f"{date_str} {time_str} 株レポート（手動）"
        if now.hour < 15:
            stock_url = create_morning_page(date_str, now_str, title, icon)
        else:
            stock_url = create_evening_page(date_str, now_str, title, icon)
            news_url  = create_news_page(date_str, time_str)
            print(f"\n=== 手動(夜)完了 ===\n株式 : {stock_url}\nニュース : {news_url}")
            return
        print(f"\n=== 手動(朝)完了 ===\n株式 : {stock_url}")
    elif mode == "morning":
        title = f"{date_str} {time_str} 朝の株レポート"
        stock_url = create_morning_page(date_str, now_str, title, "🌅")
        print(f"\n=== 朝の完了 ===\n株式 : {stock_url}")
    else:
        title = f"{date_str} {time_str} 夜の株レポート"
        stock_url = create_evening_page(date_str, now_str, title, "🌙")
        # 夜は振り返り回。ニュースダイジェストも夜に付ける
        news_url = create_news_page(date_str, time_str)
        print(f"\n=== 夜の完了 ===\n株式 : {stock_url}\nニュース : {news_url}")

if __name__ == "__main__":
    main()
