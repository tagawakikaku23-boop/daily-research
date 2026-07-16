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
# レポート格納先データベース（第3-4弾④：氾濫対策）。未設定なら従来どおりページ直下に作成
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "37afc83b-0e47-81d4-901d-d20a958122e0")
# 監視銘柄DB（Notionでワンタッチ追加）。未設定/失敗時は watchlist.csv にフォールバック
NOTION_WATCHLIST_DB_ID = os.environ.get("NOTION_WATCHLIST_DB_ID", "3555cab9-4828-4185-86eb-5a0614b8c0ea")

# ── AI出力の文字化けガード ─────────────────────────
# llama生成文にまれに混入する非日本語圏の文字（アラビア・ヘブライ・ハングル等）を除去する。
# ニュース見出しなど取得データには適用しない（AI生成文のみ）。
import re as _re
_NON_JA_RE = _re.compile(
    "["
    "֐-׿"   # ヘブライ
    "؀-ۿ"   # アラビア
    "܀-ݏ"   # シリア
    "ऀ-෿"   # インド系（デーヴァナーガリー等）
    "฀-๿"   # タイ
    "ᄀ-ᇿ"   # ハングル字母
    "가-힯"   # ハングル
    "]+")

def clean_ai_text(text):
    """AI生成文から日本語圏で使わない文字を除去する"""
    if not text:
        return text
    return _NON_JA_RE.sub("", text)

# インデックス
INDICES = [
    ("^N225",  "日経平均"),
    ("^GSPC",  "S&P 500"),
    ("^DJI",   "NYダウ"),
    ("^IXIC",  "NASDAQ"),
]

# 保有個別株 (ticker, 銘柄名, 保有株数, 平均取得単価)
# ↓ watchlist.csv が存在すればそちらを正とし、無ければこの既定値を使う
_DEFAULT_HOLDINGS = [
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
_DEFAULT_WATCHLIST = [
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
_DEFAULT_MONITOR = [
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

# ── 監視リスト（watchlist.csv）読み込み（第4弾①）─────────
WATCHLIST_FILE = Path(__file__).parent / "watchlist.csv"

def _wl_text(prop):
    """Notionプロパティ（title/rich_text）からプレーン文字列を取り出す"""
    if not prop:
        return ""
    arr = prop.get("title") or prop.get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in arr).strip()

# コード→NotionページID（ニュースフラグ書き戻し用。DB読込時に埋まる）
WATCHLIST_PAGE_IDS = {}

def load_watchlist_from_notion():
    """Notionの監視銘柄DBを読む。失敗時は None を返す（CSVへフォールバック）"""
    if not NOTION_WATCHLIST_DB_ID:
        return None
    try:
        headers = {"Authorization": f"Bearer {NOTION_API_KEY}",
                   "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
        holdings, watch, monitor = [], [], []
        cursor, guard = None, 0
        while guard < 20:
            guard += 1
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = requests.post(
                f"https://api.notion.com/v1/databases/{NOTION_WATCHLIST_DB_ID}/query",
                headers=headers, json=body, timeout=30)
            r.raise_for_status()
            j = r.json()
            for row in j.get("results", []):
                p = row.get("properties", {})
                code = _wl_text(p.get("コード"))
                name = _wl_text(p.get("銘柄名"))
                kind = (p.get("区分", {}).get("select") or {}).get("name", "")
                if not code or not name:
                    continue
                WATCHLIST_PAGE_IDS[code] = row.get("id", "")
                if kind == "保有":
                    shares = p.get("株数", {}).get("number") or 0
                    cost   = p.get("取得単価", {}).get("number") or 0
                    holdings.append((code, name, int(shares), float(cost)))
                elif kind == "候補":
                    watch.append((code, name))
                else:
                    monitor.append((code, name))
            if not j.get("has_more"):
                break
            cursor = j.get("next_cursor")
        if holdings or watch or monitor:
            print(f"  ✅ 監視銘柄をNotion DBから読込: 保有{len(holdings)}/候補{len(watch)}/監視{len(monitor)}")
            return holdings, watch, monitor
        return None
    except Exception as e:
        print(f"  ⚠️ Notion監視DB読込失敗、CSV/既定にフォールバック: {e}")
        return None

def load_watchlist():
    """監視銘柄を Notion DB → watchlist.csv → 既定リスト の順で読み込む。
    (holdings, watchlist, monitor) を返す。"""
    via_notion = load_watchlist_from_notion()
    if via_notion:
        return via_notion
    if not WATCHLIST_FILE.exists():
        return _DEFAULT_HOLDINGS, _DEFAULT_WATCHLIST, _DEFAULT_MONITOR
    holdings, watch, monitor = [], [], []
    try:
        for raw in WATCHLIST_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < 3:
                continue
            code, name, kind = cols[0], cols[1], cols[2]
            if kind == "保有":
                try:
                    shares = int(float(cols[3])) if len(cols) > 3 and cols[3] else 0
                    cost   = float(cols[4]) if len(cols) > 4 and cols[4] else 0
                except ValueError:
                    shares, cost = 0, 0
                holdings.append((code, name, shares, cost))
            elif kind == "候補":
                watch.append((code, name))
            else:  # 監視 など
                monitor.append((code, name))
        if not (holdings or watch or monitor):
            return _DEFAULT_HOLDINGS, _DEFAULT_WATCHLIST, _DEFAULT_MONITOR
        return holdings, watch, monitor
    except Exception as e:
        print(f"  ⚠️ watchlist.csv 読み込み失敗、既定リストを使用: {e}")
        return _DEFAULT_HOLDINGS, _DEFAULT_WATCHLIST, _DEFAULT_MONITOR

HOLDINGS, WATCHLIST, MONITOR = load_watchlist()

# ── 発掘銘柄プール（監視外の高配当・連続増配・割安候補）第4弾② ──
# (ticker, 銘柄名, セクター) — watchlist.csv に載っている銘柄は自動除外される
DISCOVERY_POOL = [
    ("1928.T", "積水ハウス ★連続増配", "建設・住宅"),
    ("4732.T", "ユー・エス・エス ★連続増配", "サービス(中古車)"),
    ("8424.T", "芙蓉総合リース ★連続増配", "金融(リース)"),
    ("8566.T", "リコーリース ★連続増配", "金融(リース)"),
    ("2502.T", "アサヒグループHD", "食品・飲料"),
    ("2503.T", "キリンHD", "食品・飲料"),
    ("4503.T", "アステラス製薬", "医薬品"),
    ("4901.T", "富士フイルムHD", "化学・医療"),
    ("5108.T", "ブリヂストン", "ゴム(タイヤ)"),
    ("5401.T", "日本製鉄", "鉄鋼"),
    ("5411.T", "JFEHD", "鉄鋼"),
    ("7267.T", "ホンダ", "自動車"),
    ("7270.T", "SUBARU", "自動車"),
    ("9101.T", "日本郵船", "海運"),
    ("9104.T", "商船三井", "海運"),
    ("9513.T", "電源開発(Jパワー)", "電力"),
    ("5020.T", "ENEOS HD", "石油・エネルギー"),
    ("6178.T", "日本郵政", "サービス・金融"),
    ("7956.T", "ピジョン", "日用品(育児)"),
    ("4544.T", "H.U.グループ", "医薬・検査"),
    ("1893.T", "五洋建設", "建設(海洋土木)"),
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
        pbr     = info.get("priceToBook")
        sector  = info.get("sector") or info.get("industry") or ""

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
            "per": per, "pbr": pbr, "sector": sector, "signal": signal,
            "week_chg": week_chg, "drop_high": drop_high, "asof": asof,
        }
    except Exception as e:
        return {"error": str(e)}

# ── 東証の休場判定（土日・祝日）─────────────────────
# 東証の休業日（祝日＋振替＋年末年始）。2026-2027分。年1回追記でメンテ。
TSE_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-12", "2026-02-11",
    "2026-02-23", "2026-03-20", "2026-04-29", "2026-05-03", "2026-05-04",
    "2026-05-05", "2026-05-06", "2026-07-20", "2026-08-11", "2026-09-21",
    "2026-09-22", "2026-09-23", "2026-10-12", "2026-11-03", "2026-11-23",
    "2026-12-31",
    # 2027
    "2027-01-01", "2027-01-02", "2027-01-03", "2027-01-11", "2027-02-11",
    "2027-02-23", "2027-03-22", "2027-04-29", "2027-05-03", "2027-05-04",
    "2027-05-05", "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23",
    "2027-10-11", "2027-11-03", "2027-11-23", "2027-12-31",
}

# 祝日判定はライブラリ（jpholiday）を優先し、無い環境では手打ちリストにフォールバック
try:
    import jpholiday
    _HAS_JPHOLIDAY = True
except ImportError:
    _HAS_JPHOLIDAY = False

def is_tse_trading_day(dt):
    """東証が開く日か（土日・祝日・年末年始でないか）"""
    if dt.weekday() >= 5:          # 5=土, 6=日
        return False
    # 年末年始（12/31〜1/3）は東証休業
    if (dt.month == 12 and dt.day == 31) or (dt.month == 1 and dt.day <= 3):
        return False
    if _HAS_JPHOLIDAY:
        return not jpholiday.is_holiday(dt.date())
    return dt.strftime("%Y-%m-%d") not in TSE_HOLIDAYS

def market_status(now):
    """休場判定とラベルを返す。
    戻り値: dict(open=bool, reason=str, next_label=str)"""
    if is_tse_trading_day(now):
        return {"open": True, "reason": "", "next_label": ""}
    wd = "土曜" if now.weekday() == 5 else ("日曜" if now.weekday() == 6 else "祝日")
    return {"open": False,
            "reason": f"本日は{wd}で東証は休場",
            "next_label": "週明け（次の取引日）"}

# ── マクロ環境スナップショット（yfinanceで機械取得） ──
MACRO_TICKERS = [
    ("^DJI",  "NYダウ"),
    ("^GSPC", "S&P500"),
    ("^IXIC", "NASDAQ"),
    ("^SOX",  "SOX(半導体)"),
]

# ── 指数の最新値取得（日足が遅延するため分足を日別集約して鮮度を確保） ──
def index_quote(ticker):
    """分足5mを「日別の最終値」に集約して最新値・前日比・取得時刻を返す。
    ^N225 等は日足フィードが1〜2営業日遅れることがあるため分足を優先する。
    戻り値: dict(price, chg_pct, asof_dt, days=[(date,close)...]) / 取れなければ None"""
    t = yf.Ticker(ticker)
    days = []
    # ① 分足（鮮度優先）
    try:
        intr = t.history(period="5d", interval="5m")["Close"].dropna()
        by_day = {}
        for ts, v in intr.items():
            by_day[ts.date()] = (float(v), ts)
        for d in sorted(by_day):
            days.append((d, by_day[d][0], by_day[d][1]))
    except Exception:
        pass
    # ② 分足が不足なら日足でフォールバック
    if len(days) < 2:
        try:
            dl = t.history(period="1mo")["Close"].dropna()
            days = [(dl.index[i].date(), float(dl.iloc[i]), dl.index[i]) for i in range(len(dl))]
        except Exception:
            return None
    if len(days) < 1:
        return None
    price, asof_dt = days[-1][1], days[-1][2]
    prev = days[-2][1] if len(days) >= 2 else price
    return {
        "price": price,
        "chg_pct": (price - prev) / prev * 100 if prev else 0,
        "asof_dt": asof_dt,
        "days": [(d.strftime("%m/%d"), c) for d, c, _ in days],
    }

def get_macro_snapshot():
    """米国市場・SOX・ドル円・日経推移を機械取得（出典=Yahoo Finance）。
    指数は分足ベースで最新値を取り、古い値を載せないようにする。"""
    snap = {"us": [], "usdjpy": None, "nikkei_3d": [], "asof": "", "stale": []}
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    snap["asof"] = now.strftime("%Y-%m-%d %H:%M")
    # 米国主要指数＋SOX（分足ベース）
    for tk, name in MACRO_TICKERS:
        q = index_quote(tk)
        if q:
            asof = q["asof_dt"].astimezone(JST).strftime("%m/%d %H:%M")
            snap["us"].append((name, q["price"], q["chg_pct"], asof))
    # ドル円
    try:
        fx = yf.Ticker("JPY=X").history(period="5d", interval="5m")["Close"].dropna()
        if len(fx) < 2:
            fx = yf.Ticker("JPY=X").history(period="5d")["Close"].dropna()
        if len(fx) >= 1:
            cur = float(fx.iloc[-1])
            prv = float(fx.iloc[-2]) if len(fx) >= 2 else cur
            snap["usdjpy"] = (cur, (cur - prv) / prv * 100)
    except Exception:
        pass
    # 日経（分足ベースで最新＋前日比、推移）
    nk = index_quote("^N225")
    if nk:
        snap["nikkei_3d"] = nk["days"][-3:]
        # 鮮度チェック：最新足が2日以上前なら警告フラグ
        age_days = (now.date() - nk["asof_dt"].astimezone(JST).date()).days
        prev = nk["days"][-2][1] if len(nk["days"]) >= 2 else nk["price"]
        snap["nikkei"] = {
            "close": nk["price"], "chg_yen": nk["price"] - prev,
            "chg_pct": nk["chg_pct"],
            "asof": nk["asof_dt"].astimezone(JST).strftime("%m/%d %H:%M"),
            "stale": age_days >= 2,
        }
        if age_days >= 2:
            snap["stale"].append(f"日経データが{age_days}日前（{snap['nikkei']['asof']}）")
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
    if mode == "weekend":
        queries = [
            "週明け 日経平均 見通し", "来週 株式市場 注目", "米国株 金曜 終値",
            "来週 経済指標 日米 スケジュール",
        ]
    elif mode == "morning":
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

# ── 💎 発掘銘柄（第4弾②③）────────────────────────
DISC_FILE = Path(__file__).parent / "last_discovery.json"

def load_recent_discovery():
    """直近数日に提案した銘柄コード（顔ぶれ重複回避用）"""
    try:
        import json
        data = json.loads(DISC_FILE.read_text(encoding="utf-8"))
        return set(data.get("recent", []))
    except Exception:
        return set()

def save_recent_discovery(tickers, keep=12):
    try:
        import json
        prev = []
        try:
            prev = json.loads(DISC_FILE.read_text(encoding="utf-8")).get("recent", [])
        except Exception:
            pass
        merged = list(tickers) + [t for t in prev if t not in tickers]
        DISC_FILE.write_text(json.dumps({"recent": merged[:keep]}, ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        pass

def pick_discovery(date_str, n=3):
    """監視外の高配当・連続増配・割安銘柄を日替わりで2〜3つ選ぶ。
    戻り値: [(ticker, name, sector, d, reasons[]), ...]"""
    import random
    held = ({t for t, *_ in HOLDINGS} | {t for t, _ in WATCHLIST}
            | {t for t, _ in MONITOR})
    recent = load_recent_discovery()
    pool = [c for c in DISCOVERY_POOL if c[0] not in held]
    random.seed(date_str)          # 日付シードで毎日順番が変わる（日替わり）
    random.shuffle(pool)
    # 直近に出した銘柄は後回し
    pool.sort(key=lambda c: c[0] in recent)

    picks = []
    for ticker, name, sector in pool:
        if len(picks) >= n:
            break
        d = get_stock_data(ticker)
        if not d or "error" in d:
            continue
        dy  = d.get("div_yield") or 0
        per = d.get("per")
        pbr = d.get("pbr")
        reasons = []
        if dy >= 3.5:
            reasons.append(f"高配当{dy:.1f}%")
        if "連続増配" in name:
            reasons.append("連続増配")
        if per and per < 12:
            reasons.append(f"低PER{per:.1f}")
        if pbr and pbr <= 1.2:
            reasons.append(f"低PBR{pbr:.2f}")
        if not reasons:
            continue
        # 配当を脅かす重大ニュースがある銘柄は出さない
        _, risks, _ = get_ticker_news(name, max_items=2)
        if risks:
            continue
        picks.append((ticker, name, sector, d, reasons))

    save_recent_discovery([p[0] for p in picks])
    return picks

def generate_discovery_comment(picks):
    """発掘銘柄に『なぜ注目か＋会社概要一言』をAIで付ける（出典は数値ベース）"""
    if not picks:
        return {}
    try:
        client = Groq(api_key=GROQ_API_KEY)
        lines = []
        for ticker, name, sector, d, reasons in picks:
            lines.append(f"{ticker} {name}／{sector}／"
                         f"配当{(d.get('div_yield') or 0):.1f}% "
                         f"PER{d.get('per') or '―'} PBR{d.get('pbr') or '―'}／"
                         f"注目点:{'・'.join(reasons)}")
        data_text = "\n".join(lines)
        prompt = f"""次の各銘柄について、長期インカム投資家向けに
「①その会社が何をしているか（一言）」「②なぜ今注目できるか（1〜2行、提示した数値に基づく）」
を簡潔に書いてください。誇張や憶測は避け、提示数値の範囲で。

各銘柄を必ず次の形式で：
{{コード}} | {{何の会社か一言}} | {{注目理由1〜2行}}

【対象】
{data_text}"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        out = {}
        for ln in clean_ai_text(r.choices[0].message.content).splitlines():
            if "|" in ln:
                parts = [p.strip() for p in ln.split("|")]
                code = parts[0].split()[0] if parts[0] else ""
                if code:
                    out[code] = parts[1:]
        return out
    except Exception:
        return {}

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
def get_rss_news(urls, max_items=5, retries=3, wait=3):
    """RSS取得。一時的な失敗（サイト混雑・通信瞬断）に備えて数回リトライする。"""
    items = []
    for url in urls:
        for attempt in range(retries):
            try:
                feed = feedparser.parse(url)
                entries = feed.entries or []
                if not entries:
                    raise ValueError("entries empty")  # 一時不調とみなして再試行
                for entry in entries:
                    title = entry.get("title", "").strip()
                    link  = entry.get("link", "")
                    if title and len(title) > 5:
                        items.append((title, link))
                    if len(items) >= max_items:
                        break
                break  # このURLは成功
            except Exception:
                if attempt < retries - 1:
                    print(f"    RSS再試行({attempt+1}/{retries}): {url[:50]}")
                    time.sleep(wait)
                    continue
        if items:
            break
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

- 出力は日本語のみ。他言語の文字（アラビア文字等）を混ぜない。

事実とニュースに基づき憶測を避け、4〜6行で簡潔に。同じ言い回しの繰り返しを避けること。"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        return clean_ai_text(r.choices[0].message.content)
    except Exception as e:
        return f"マクロ解説生成失敗: {e}"

# ── 週末・休場日の「週明け気配」AI生成 ─────────────────
def generate_weekend_outlook(us_text, news_text, today_str):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""あなたは日本株の市況に詳しいアナリストです。本日は {today_str}（東証は休場）。
東証は動いていないので、直近の金曜の米国市場の流れと週末のニュースから、
「週明け（次の取引日）の日本株の気配」を述べてください。

【金曜の米国市場（週明けの手がかり・Yahoo Finance）】
{us_text}

【週末の関連ニュース見出し（Google News）】
{news_text}

厳守ルール：
- これは「断定」ではなく「気配・見通し」。「上がる/下がる」と言い切らず「〜の流れなら上がりやすい/下がりやすい」と条件付きで。
- 株価・指数の数字は上の【金曜の米国市場】の値のみ使う。自分で数字を創作しない。
- 経済イベントは「○月○日」と日付を添え、本日({today_str})より後の予定だけ。過去のものは書かない。
- 半導体が強ければ半導体関連、円安なら輸出関連…のように、どのセクター/銘柄傾向に効きそうかを一言。

出力は日本語のみ。4〜6行で簡潔に。最後に「※あくまで気配。寄り付きで確認を」と添える。"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        return clean_ai_text(r.choices[0].message.content)
    except Exception as e:
        return f"週明け気配の生成失敗: {e}"

# ── Groq APIでアドバイス生成 ─────────────────────
def generate_advice(portfolio_summary, news_summary):
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""あなたは日本株・米国株に詳しい投資アドバイザーです。
以下のポートフォリオデータと最新ニュースをもとに、今日のアドバイスをください。
出力は日本語のみ（他言語の文字を混ぜない）。

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
        return clean_ai_text(response.choices[0].message.content)
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
        return clean_ai_text(response.choices[0].message.content)
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
        return clean_ai_text(response.choices[0].message.content)
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

# ── 見やすい版フォーマット用ヘルパー（表・段組み・トグル・色） ──
def rt(text, bold=False, color=None, link=None):
    """リッチテキスト1要素を作る。color は 'green' / 'red_background' 等"""
    o = {"type": "text", "text": {"content": str(text)[:2000]}}
    ann = {}
    if bold:  ann["bold"] = True
    if color: ann["color"] = color
    if ann:   o["annotations"] = ann
    if link:  o["text"]["link"] = {"url": link}
    return o

def callout_rt(rich, emoji="💡", color=None):
    """リッチテキスト配列＋色付きのコールアウト"""
    body = {"rich_text": rich, "icon": {"type": "emoji", "emoji": emoji}}
    if color: body["color"] = color
    return {"object": "block", "type": "callout", "callout": body}

def card(title, lines, emoji="📊", color="gray_background"):
    """段組みの中に入れる小さなカード（太字タイトル＋数値行）"""
    rich = [rt(title, bold=True)]
    for ln in lines:
        rich.append(rt("\n" + ln))
    return callout_rt(rich, emoji, color)

def columns(col_blocklists):
    """col_blocklists: 各カラムのブロック配列のリスト（2個以上）"""
    cols = []
    for blocks in col_blocklists:
        cols.append({"object": "block", "type": "column",
                     "column": {"children": blocks}})
    return {"object": "block", "type": "column_list",
            "column_list": {"children": cols}}

def cell(text, color=None, bold=False):
    """テーブルセル（リッチテキスト配列）"""
    return [rt(text, bold=bold, color=color)]

def table(headers, rows, has_row_header=False):
    """headers: 文字列リスト, rows: 各行=セル配列（cell()で作る）のリスト"""
    width = len(headers)
    children = [{"object": "block", "type": "table_row",
                 "table_row": {"cells": [cell(h, bold=True) for h in headers]}}]
    for r in rows:
        # 足りない列は空セルで埋める
        cells = list(r) + [cell("")] * (width - len(r))
        children.append({"object": "block", "type": "table_row",
                         "table_row": {"cells": cells[:width]}})
    return {"object": "block", "type": "table",
            "table": {"table_width": width, "has_column_header": True,
                      "has_row_header": has_row_header, "children": children}}

def toggle(summary, children, color=None):
    """折りたたみ（出典リンクなどを隠す）"""
    body = {"rich_text": [rt(summary)], "children": children}
    if color: body["color"] = color
    return {"object": "block", "type": "toggle", "toggle": body}

def chg_color(pct, strong=2.0):
    """前日比%に応じた文字色（ヒートマップ用）。大きい動きは背景色で強調"""
    if pct >= strong:   return "green_background"
    if pct > 0:         return "green"
    if pct <= -strong:  return "red_background"
    if pct < 0:         return "red"
    return None

def signal_color(signal):
    """判定文字列→色"""
    if "⛔" in signal or "🔴" in signal: return "red"
    if "🟢" in signal:                   return "green"
    if "🟡" in signal:                   return "yellow"
    return None

# アイコン→「種類」セレクト値の対応（DB分類用）
_ICON_KIND = {"🌅": "🌅 朝", "🌙": "🌙 夜", "🔄": "🔄 手動", "📰": "📰 ニュース", "📊": "📊 月次", "🛒": "🛒 週次"}

def create_page(title, blocks, icon="📋"):
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type":  "application/json",
        "Notion-Version": "2022-06-28",
    }
    # DBが設定されていれば、日付・種類プロパティ付きでデータベースに登録（一覧で並べ替え/絞り込み可能）
    if NOTION_DB_ID:
        # タイトル先頭の YYYY-MM-DD を日付プロパティに使う
        date_val = title[:10] if len(title) >= 10 and title[4] == "-" else None
        props = {"名前": {"title": [{"text": {"content": title}}]}}
        if date_val:
            props["日付"] = {"date": {"start": date_val}}
        if icon in _ICON_KIND:
            props["種類"] = {"select": {"name": _ICON_KIND[icon]}}
        data = {
            "parent": {"database_id": NOTION_DB_ID},
            "icon":   {"type": "emoji", "emoji": icon},
            "properties": props,
            "children": blocks[:100],
        }
    else:
        data = {
            "parent": {"page_id": PARENT_PAGE_ID},
            "icon":   {"type":"emoji","emoji":icon},
            "properties": {"title":{"title":[{"text":{"content":title}}]}},
            "children": blocks[:100],
        }
    # Notion側の一時エラー（5xx/429/瞬断）に備えてリトライ
    resp = None
    for attempt in range(3):
        try:
            resp = requests.post("https://api.notion.com/v1/pages",
                                 headers=headers, json=data, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"status {resp.status_code}")
            resp.raise_for_status()
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Notionページ作成リトライ({attempt+1}/3): {e}")
                time.sleep(10 * (attempt + 1))
                continue
            raise
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
        return clean_ai_text(r.choices[0].message.content)
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

def write_news_flags(data):
    """朝夜レポートで検出したニュースフラグを監視銘柄DBへ書き戻す（best-effort）。
    30分毎の指標更新(refresh_watchlist.py)がこの列を読んで判定に反映する。"""
    if not (NOTION_WATCHLIST_DB_ID and WATCHLIST_PAGE_IDS):
        return
    headers = {"Authorization": f"Bearer {NOTION_API_KEY}",
               "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    rows = [(t, risks, goods) for t, n, s, c, d, nw, risks, goods, l in data["holdings"]]
    rows += [(t, risks, goods) for t, n, d, nw, risks, goods, l in data["watch"]]
    rows += [(t, risks, goods) for t, n, d, nw, risks, goods, l in data["monitor"]]
    ok = 0
    for code, risks, goods in rows:
        pid = WATCHLIST_PAGE_IDS.get(code)
        if not pid:
            continue
        flags = "・".join((risks or []) + (goods or []))
        body = {"properties": {"ニュースフラグ": {
            "rich_text": ([{"text": {"content": flags[:200]}}] if flags else [])}}}
        try:
            r = requests.patch(f"https://api.notion.com/v1/pages/{pid}",
                               headers=headers, json=body, timeout=30)
            r.raise_for_status()
            ok += 1
        except Exception:
            continue
    print(f"  ニュースフラグ書き戻し: {ok}/{len(rows)}件")

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

# ── 見やすい版：集計とセクション組み立て ─────────────
def portfolio_metrics(data, usdjpy):
    """評価額・含み損益・年間配当・平均利回りを算出"""
    val = pl = div = 0.0
    for ticker, name, shares, cost, d, *_ in data["holdings"]:
        if not d or "error" in d:
            continue
        px = d["price"] * usdjpy if is_usd_ticker(ticker) else d["price"]
        val += px * shares
        pl  += (px - cost) * shares
        if (d.get("div_yield") or 0) > 0:
            div += px * shares * d["div_yield"] / 100
    yld = div / val * 100 if val else 0
    return val, pl, div, yld

def _signed(pct, digits=1):
    return f"{'+' if pct >= 0 else ''}{pct:.{digits}f}%"

def build_summary_cards(data, usdjpy):
    """ポートフォリオ・サマリーの4カード（段組み）"""
    val, pl, div, yld = portfolio_metrics(data, usdjpy)
    pl_pct = pl / (val - pl) * 100 if (val - pl) else 0
    pl_color = "green_background" if pl >= 0 else "red_background"
    return columns([
        [card("💰 評価額", [f"¥{val:,.0f}"], "💰", "gray_background")],
        [card("📈 含み損益", [f"{'+' if pl>=0 else ''}¥{pl:,.0f}", _signed(pl_pct)], "📈", pl_color)],
        [card("💴 年間配当(概算)", [f"¥{div:,.0f}"], "💴", "blue_background")],
        [card("📊 平均利回り", [f"{yld:.1f}%"], "📊", "yellow_background")],
    ])

def collect_buys_and_cautions(data):
    """買い場候補・要注意（⛔）銘柄を仕分け"""
    buys, cautions = [], []
    rows = [(t, n, d, risks) for t, n, s, c, d, nw, risks, g, l in data["holdings"]]
    rows += [(t, n, d, risks) for t, n, d, nw, risks, g, l in data["watch"]]
    for ticker, name, d, risks in rows:
        if not d or "error" in d:
            continue
        sig = data["signals"].get(name, "")
        if "⛔" in sig:
            cautions.append((name, risks))
        elif "🟢" in sig or "買い" in sig:
            buys.append((ticker, name, d))
    return buys, cautions

def build_tldr(mode, data, snap, is_open=True):
    """今日の3行まとめ（データから決定的に作成＝古い情報を載せない）"""
    buys, cautions = collect_buys_and_cautions(data)
    lines = []
    # 1) 相場の方向（当日のスナップショットのみ）
    nk = snap.get("nikkei")
    if nk and not is_open:
        lines.append(rt(f"1. 🏖️ 本日は休場。最終取引日の日経終値 {nk['close']:,.0f}円。週明けは米国市場の流れに注目。"))
    elif nk:
        ud = "上昇" if nk["chg_yen"] >= 0 else "下落"
        lines.append(rt(f"1. 日経 {nk['close']:,.0f}円（前日比{_signed(nk['chg_pct'],2)}）の{ud}。"))
    # 2) 買い場
    if buys:
        names = "・".join(n for _, n, _ in buys[:3])
        lines.append(rt(f"\n2. 今日の買い場候補：{names}", bold=True, color="green"))
    else:
        lines.append(rt("\n2. 今日の買い場候補：なし（無理に買わない）"))
    # 3) 注意
    if cautions:
        names = "・".join(n for n, _ in cautions[:3])
        lines.append(rt(f"\n3. ⛔ 要注意：{names}（重要ニュースあり・判断保留）", color="red"))
    else:
        lines.append(rt("\n3. 重大な悪材料ニュースは検出なし"))
    head = [rt("今日の3行まとめ（TL;DR）\n", bold=True)]
    return callout_rt(head + lines, "📌", "purple_background")

def build_buy_alert(data):
    """買い場アラート（緑）＋要注意（赤）"""
    buys, cautions = collect_buys_and_cautions(data)
    blocks = []
    if buys:
        rich = [rt("◎ 買い場・買い検討（割安＋配当）\n", bold=True)]
        for ticker, name, d in buys[:5]:
            per = f"PER{d['per']:.1f}" if d.get("per") else "PER―"
            rich.append(rt(f"・{name}  {format_price(ticker, d['price'])}  配当{(d.get('div_yield') or 0):.1f}%  {per}\n"))
        blocks.append(callout_rt(rich, "🟢", "green_background"))
    if cautions:
        rich = [rt("⛔ 今日は見送り・要注意\n", bold=True)]
        for name, risks in cautions[:5]:
            tag = "・".join(risks) if risks else "重要ニュースあり"
            rich.append(rt(f"・{name}（{tag}）\n"))
        blocks.append(callout_rt(rich, "⛔", "red_background"))
    return blocks

def build_heatmap_table(data, usdjpy):
    """保有株ヒートマップ（前日比を色で）"""
    rows = []
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if not d or "error" in d:
            continue
        px = d["price"] * usdjpy if is_usd_ticker(ticker) else d["price"]
        chg = d["chg_pct"]; wk = d.get("week_chg") or 0
        rows.append([
            cell(name),
            cell(f"¥{px:,.0f}"),
            cell(_signed(chg, 2), chg_color(chg), bold=abs(chg) >= 2),
            cell(_signed(wk), "green" if wk >= 0 else "red"),
            cell(f"{(d.get('div_yield') or 0):.1f}%"),
        ])
    return table(["銘柄", "現在値", "前日比", "週間", "配当"], rows)

def build_judgment_table(data, usdjpy, title_label="保有株"):
    """銘柄一覧（判定を色付きで）"""
    rows = []
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if not d or "error" in d:
            rows.append([cell(name), cell("取得失敗"), cell(""), cell(""), cell("")])
            continue
        px = d["price"] * usdjpy if is_usd_ticker(ticker) else d["price"]
        sig = data["signals"].get(name, "")
        rows.append([
            cell(name),
            cell(f"¥{px:,.0f}"),
            cell(f"{(d.get('div_yield') or 0):.1f}%"),
            cell(f"{int(d['position']*100)}%"),
            cell(sig, signal_color(sig)),
        ])
    return table(["銘柄", "現在値", "配当", "52W", "判定"], rows)

def build_watch_table(data):
    """検討銘柄の判定テーブル"""
    rows = []
    for ticker, name, d, news, risks, goods, line in data["watch"]:
        if not d or "error" in d:
            continue
        sig = data["signals"].get(name, "")
        per = f"{d['per']:.1f}" if d.get("per") else "―"
        rows.append([
            cell(name),
            cell(format_price(ticker, d["price"])),
            cell(f"{(d.get('div_yield') or 0):.1f}%"),
            cell(per),
            cell(sig, signal_color(sig)),
        ])
    return table(["銘柄", "現在値", "配当", "PER", "判定"], rows)

def news_toggle(label, items):
    """ニュースの引用リンクをトグルに折りたたむ"""
    children = [bul(t, link or None) for t, link in items]
    return toggle(f"📎 {label}（クリックで開く）", children)

def freshness_note():
    return callout_rt(
        [rt("※ データは本日取得分。古い記事は載せず、過去の事例は「過去の参照」と明示時のみ引用。")],
        "🕒", "gray_background")

def build_weekend_section(status, snap, date_str):
    """休場日（土日祝）用：休場バナー＋金曜の米国市場＋週明けの気配"""
    blocks = [callout_rt(
        [rt(status["reason"] + "。", bold=True),
         rt(" 表の株価・前日比は最終取引日（直近の金曜など）の終値です。")],
        "🏖️", "orange_background")]
    us_map = {n: (p, c) for n, p, c, _ in snap.get("us", [])}
    cards = []
    for key, emoji in [("S&P500", "📈"), ("NASDAQ", "💻"), ("SOX(半導体)", "🔥")]:
        if key in us_map:
            p, c = us_map[key]
            cards.append([card(f"{emoji} {key}", [f"{p:,.0f}", _signed(c, 2)], emoji,
                               "green_background" if c >= 0 else "red_background")])
    if snap.get("usdjpy"):
        cur, c = snap["usdjpy"]
        cards.append([card("💴 ドル円", [f"{cur:.2f}", _signed(c, 2)], "💴", "gray_background")])
    blocks.append(h3("🌎 金曜の米国市場（週明けの手がかり）"))
    if len(cards) >= 2:
        blocks.append(columns(cards))
    # 週明けの気配（AI）
    print("  週明け気配生成中...")
    news = get_macro_news("weekend")
    ai = generate_weekend_outlook(
        macro_snapshot_text(snap),
        "\n".join(f"・{t}" for t, _ in news) or "（ニュース取得なし）", date_str)
    blocks.append(h3("🔮 週明けの気配"))
    blocks.append(callout_rt([rt(ai[:1900])], "🔮", "purple_background"))
    if news:
        blocks.append(news_toggle("週明け関連ニュース", news[:5]))
    blocks.append(divider())
    return blocks

# ── 朝レポート（07:00）────────────────────────────
def create_morning_page(date_str, now_str, title, icon):
    print("\n==== 🌅 朝レポート作成中 ====")
    usdjpy = get_usdjpy_rate() or 1
    snap   = get_macro_snapshot()
    macro_news = get_macro_news("morning")
    macro_txt  = macro_snapshot_text(snap)

    # データ収集（TL;DR等で使うので先に）
    print("  銘柄データ収集中...")
    data = collect_portfolio(usdjpy)
    write_news_flags(data)   # 監視銘柄DBの判定をニュース連動させる

    status = market_status(datetime.now(timezone(timedelta(hours=9))))
    blocks = [callout_rt([rt(f"生成: {now_str}", bold=True), rt("　｜　朝＝今日これからの戦略")],
                         "🌅", "yellow_background")]

    # 今日の3行まとめ（TL;DR）
    blocks.append(build_tldr("morning", data, snap, status["open"]))

    # 休場日（土日祝）は「週明けの気配」を差し込む
    if not status["open"]:
        blocks.extend(build_weekend_section(status, snap, date_str))

    # ポートフォリオ・サマリー（4カード）
    blocks.append(h2("💼 ポートフォリオ・サマリー"))
    blocks.append(build_summary_cards(data, usdjpy))

    # 買い場アラート / 要注意
    blocks.append(h2("🔥 今日の買い場アラート"))
    alert = build_buy_alert(data)
    blocks.extend(alert if alert else [para("本日は明確な買い場・要注意銘柄なし")])

    # ① 今朝のマクロ環境（主要3指標をカード＋他はトグル）
    blocks.append(h2("① 今朝のマクロ環境"))
    if snap.get("stale"):
        blocks.append(callout_rt([rt("データ鮮度の注意：" + "／".join(snap["stale"]), bold=True)],
                                 "⚠️", "orange_background"))
    us_map = {name: (price, chg) for name, price, chg, _ in snap["us"]}
    macro_cards = []
    if snap.get("nikkei"):
        nk = snap["nikkei"]
        macro_cards.append([card("📉 日経平均", [f"{nk['close']:,.0f}円", _signed(nk['chg_pct'],2), f"({nk['asof']})"],
                                 "📉", "red_background" if nk['chg_yen']<0 else "green_background")])
    if snap.get("usdjpy"):
        cur, chg = snap["usdjpy"]
        macro_cards.append([card("💴 ドル円", [f"{cur:.2f}", _signed(chg,2)], "💴", "gray_background")])
    if "SOX(半導体)" in us_map:
        p, c = us_map["SOX(半導体)"]
        macro_cards.append([card("🔥 SOX 半導体", [f"{p:,.0f}", _signed(c,2)], "🔥",
                                 "green_background" if c>=0 else "red_background")])
    if len(macro_cards) >= 2:
        blocks.append(columns(macro_cards))
    other_us = [bul(f"{n}: {p:,.2f}  ({_signed(c,2)})") for n, (p, c) in us_map.items() if "SOX" not in n]
    if snap.get("nikkei_3d"):
        other_us.append(bul("日経 直近推移: " + " → ".join(f"{day} {v:,.0f}" for day, v in snap["nikkei_3d"])))
    if other_us:
        blocks.append(toggle("📊 その他の米国指数・日経の推移", other_us))

    # ② 今日の相場観（AI）＋出典はトグル
    print("  マクロ解説生成中...")
    macro_ai = generate_macro_analysis(macro_txt, macro_news, "morning", date_str)
    blocks.append(h2("② 今日の相場観"))
    blocks.append(callout_rt([rt(macro_ai[:1900])], "🧭", "blue_background"))
    if macro_news:
        blocks.append(news_toggle("相場の理由ニュース", macro_news[:5]))
    blocks.append(divider())

    # 前回からの変化（差分）
    prev = load_prev_snapshot()
    dl   = diff_lines(prev.get("signals", {}), data["signals"])
    if dl:
        blocks.append(callout_rt([rt("前回からの変化\n", bold=True)] + [rt(f"・{x}\n") for x in dl[:8]],
                                 "📊", "gray_background"))

    # ③ 保有株ヒートマップ＋判定テーブル
    blocks.append(h2("③ 保有株ヒートマップ"))
    blocks.append(callout_rt([rt("色が濃いほど値動き大　｜　🟩緑=上昇　🟥赤=下落")], "💡", "gray_background"))
    blocks.append(build_heatmap_table(data, usdjpy))

    blocks.append(h2("④ 買い場判定（保有株）"))
    blocks.append(callout_rt([rt("52W: 0%=安値〜100%=高値　🟢割安 🟡適正 🔴高値 ⛔重大ニュースで保留")], "📌", "gray_background"))
    blocks.append(build_judgment_table(data, usdjpy))

    blocks.append(h2("⑤ 新規購入検討"))
    blocks.append(build_watch_table(data))

    # 各銘柄ニュースはトグルに集約（ゴチャつき防止）
    news_children = []
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        for t, link in news:
            news_children.append(bul(f"{name}: {t}", link or None))
    for ticker, name, d, news, risks, goods, line in data["watch"]:
        for t, link in news:
            news_children.append(bul(f"{name}: {t}", link or None))
    if news_children:
        blocks.append(toggle("📎 保有・候補の関連ニュース（クリックで開く）", news_children[:90]))
    blocks.append(divider())

    # ⑥ 買い増し助言（AI）
    print("  買い増し助言生成中...")
    advice = generate_advice("\n".join(data["portfolio_lines"][:40]),
                             "\n".join(data["news_lines"][:20]) or "ニュースなし")
    blocks.append(h2("⑥ 今日の買い増し助言・新規注目"))
    blocks.append(callout_rt([rt("予算目安: 1回10〜30万円・分割で。NISA成長枠が残れば高配当はNISA優先")], "💴", "blue_background"))
    blocks.append(callout_rt([rt(advice[:1900])], "💡", "yellow_background"))
    blocks.append(divider())

    # 💎 今日の発掘銘柄（監視外からAIが提案／朝のみ）
    print("  発掘銘柄選定中...")
    picks = pick_discovery(date_str, n=3)
    blocks.append(h2("💎 今日の発掘銘柄"))
    blocks.append(callout("保有・監視リスト外から、高配当/連続増配/割安の候補を日替わりで提案。気に入ったら1行追記で正式採用", "💎"))
    if picks:
        comments = generate_discovery_comment(picks)
        for ticker, name, sector, d, reasons in picks:
            dy  = d.get("div_yield") or 0
            per = f"PER{d['per']:.1f}" if d.get("per") else "PER―"
            pbr = f"PBR{d['pbr']:.2f}" if d.get("pbr") else "PBR―"
            blocks.append(h3(f"{name}（{ticker}）"))
            blocks.append(bul(f"{format_price(ticker, d['price'])}  配当{dy:.1f}%  {per}  {pbr}  ｜ {sector}"))
            blocks.append(bul(f"注目点: {'・'.join(reasons)}"))
            c = comments.get(ticker)
            if c:
                if len(c) >= 1 and c[0]:
                    blocks.append(bul(f"📖 {c[0]}"))
                if len(c) >= 2 and c[1]:
                    blocks.append(bul(f"🔎 {c[1]}"))
            # 採用方法（コピペ用の1行）
            blocks.append(bul(f"📌 採用するには：📋 監視銘柄リストで ＋New → コード「{ticker}」銘柄名「{name}」区分「監視」を入力 → 次回から自動で追跡"))
    else:
        blocks.append(para("本日は条件を満たす発掘銘柄なし（無理に提案しません）"))
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
    blocks.append(freshness_note())

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

    # データ収集（TL;DR等で先に使う）
    print("  銘柄データ収集中...")
    data = collect_portfolio(usdjpy)
    write_news_flags(data)   # 監視銘柄DBの判定をニュース連動させる

    status = market_status(datetime.now(timezone(timedelta(hours=9))))
    blocks = [callout_rt([rt(f"生成: {now_str}", bold=True), rt("　｜　夜＝今日の結果の振り返り")],
                         "🌙", "blue_background")]

    # 今日の3行まとめ（TL;DR）
    blocks.append(build_tldr("evening", data, snap, status["open"]))

    # 休場日（土日祝）は「週明けの気配」を差し込む
    if not status["open"]:
        blocks.extend(build_weekend_section(status, snap, date_str))

    # ① 今日の市場サマリー（カード）
    blocks.append(h2("① 今日の市場サマリー（実績）"))
    if snap.get("stale"):
        blocks.append(callout_rt([rt("データ鮮度の注意：" + "／".join(snap["stale"]), bold=True)],
                                 "⚠️", "orange_background"))
    sum_cards = []
    if snap.get("nikkei"):
        nk = snap["nikkei"]
        sum_cards.append([card("📊 日経平均", [f"{nk['close']:,.0f}円", _signed(nk['chg_pct'],2), f"({nk['asof']})"],
                              "📊", "red_background" if nk['chg_yen']<0 else "green_background")])
    if snap.get("usdjpy"):
        cur, chg = snap["usdjpy"]
        sum_cards.append([card("💴 ドル円", [f"{cur:.2f}", _signed(chg,2)], "💴", "gray_background")])
    us_map = {name: (price, chg) for name, price, chg, _ in snap["us"]}
    if "SOX(半導体)" in us_map:
        p, c = us_map["SOX(半導体)"]
        sum_cards.append([card("🔥 SOX 半導体", [f"{p:,.0f}", _signed(c,2)], "🔥",
                              "green_background" if c>=0 else "red_background")])
    if len(sum_cards) >= 2:
        blocks.append(columns(sum_cards))

    # ポートフォリオ・サマリー
    blocks.append(h2("💼 ポートフォリオ・サマリー"))
    blocks.append(build_summary_cards(data, usdjpy))

    # ② 今日動いた要因（AI）＋出典トグル
    print("  要因分析生成中...")
    macro_ai = generate_macro_analysis(macro_txt, macro_news, "evening", date_str)
    blocks.append(h2("② 今日動いた要因の分析"))
    blocks.append(callout_rt([rt(macro_ai[:1900])], "🔍", "yellow_background"))
    if macro_news:
        blocks.append(news_toggle("値動きの理由ニュース", macro_news[:5]))
    blocks.append(divider())

    # 前回からの変化
    prev = load_prev_snapshot()
    dl   = diff_lines(prev.get("signals", {}), data["signals"])
    if dl:
        blocks.append(callout_rt([rt("前回からの変化\n", bold=True)] + [rt(f"・{x}\n") for x in dl[:8]],
                                 "📊", "gray_background"))

    # ③ 今日大きく動いた銘柄（±2%以上 or 重要ニュース）→ 表
    blocks.append(h2("③ 今日大きく動いた銘柄"))
    blocks.append(callout_rt([rt("掲載基準: 前日比±2%以上、または重要ニュースあり。値動きの大きい順")], "📏", "gray_background"))
    movers, movers_lines, mrows = [], [], []
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if d and "error" not in d and (abs(d["chg_pct"]) >= 2.0 or risks or goods):
            movers.append((abs(d["chg_pct"]), d, name, ticker, risks, goods))
    for ticker, name, d, news, risks, goods, line in data["watch"]:
        if d and "error" not in d and (abs(d["chg_pct"]) >= 2.0 or risks or goods):
            movers.append((abs(d["chg_pct"]), d, name, ticker, risks, goods))
    movers.sort(key=lambda x: -x[0])
    if movers:
        for absc, d, name, ticker, risks, goods in movers:
            chg = d["chg_pct"]
            tag = "・".join(risks + goods) if (risks or goods) else ("※ニュース注目" if absc < 2.0 else "")
            mrows.append([
                cell(name),
                cell(format_price(ticker, d["price"])),
                cell(_signed(chg, 2), chg_color(chg), bold=absc >= 2),
                cell(tag, "red" if risks else None),
            ])
            movers_lines.append(f"{name} {_signed(chg,2)} {tag}")
        blocks.append(table(["銘柄", "株価", "前日比", "メモ"], mrows))
    else:
        blocks.append(para("本日、±2%以上動いた保有/候補銘柄なし（小動き・重要ニュースもなし）"))
    blocks.append(divider())

    # ④ 重要ニュース（リスク/好材料のみ）→ コールアウト＋トグル
    blocks.append(h2("④ 保有・候補に効く重要ニュース"))
    flagged = False
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if risks or goods:
            flagged = True
            color = "red_background" if risks else "green_background"
            blocks.append(callout_rt([rt(f"{name}: ", bold=True), rt("・".join(risks + goods))],
                                     "⚠️" if risks else "✨", color))
            rel = [(t, link) for t, link in news if classify_news(t)[0] or classify_news(t)[1]]
            if rel:
                blocks.append(news_toggle(f"{name} 関連ニュース", rel))
    if not flagged:
        blocks.append(para("業績・配当・M&A・不祥事に関わる重要ニュースは検出なし"))
    blocks.append(divider())

    # ⑤⑥ 明日の注目＋ひとことメモ
    print("  夜メモ生成中...")
    memo = generate_evening_memo(macro_txt, "\n".join(movers_lines[:8]), date_str)
    blocks.append(h2("⑤ 明日の注目ポイント・ひとことメモ"))
    blocks.append(callout_rt([rt(memo[:1900])], "🔭", "blue_background"))
    blocks.append(divider())

    # 深掘り想定質問
    blocks.append(h2("🔎 深掘り用の想定質問"))
    for q in deep_dive_questions(data):
        blocks.append(bul(q))
    blocks.append(freshness_note())

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

    # ヘッダー
    total_cnt = len(all_news_for_digest)
    blocks.append(callout_rt(
        [rt(f"生成: {date_str} {time_str}", bold=True),
         rt(f"　｜　今日のニュース（全{total_cnt}件を収集・AIが要約）")],
        "📰", "blue_background"))

    # 🗞️ AIダイジェスト（最重要 → 冒頭に大きく）
    if all_news_for_digest:
        print("  AIダイジェスト生成中...")
        digest = generate_news_digest("\n".join(all_news_for_digest))
        blocks.append(h2("🗞️ 今日のダイジェスト（AI要約）"))
        blocks.append(callout_rt([rt(digest[:1900])], "🗞️", "purple_background"))
        blocks.append(divider())

    # 📰 日経新聞ピックアップ（AI所感＋ヘッドラインはトグル）
    if nikkei_items:
        print("  日経AIコメント生成中...")
        nikkei_analysis = generate_nikkei_analysis(nikkei_items)
        blocks.append(h2("📰 日経新聞ピックアップ"))
        blocks.append(callout_rt([rt(nikkei_analysis[:1900])], "📰", "yellow_background"))
        blocks.append(toggle(
            f"📋 日経ヘッドライン（{len(nikkei_items)}件・クリックで開く）",
            [bul(t, l or None) for t, l in nikkei_items]))
        blocks.append(divider())
    elif os.environ.get("NIKKEI_EMAIL"):
        blocks.append(h2("📰 日経新聞ピックアップ"))
        blocks.append(callout_rt([rt("ログインに失敗しました。メール/パスワードを確認してください。")],
                                 "⚠️", "orange_background"))
        blocks.append(divider())

    # 📋 カテゴリ別ニュース（各カテゴリをトグルに折りたたみ）
    blocks.append(h2("📋 カテゴリ別ニュース（クリックで展開）"))
    for category, items in category_news.items():
        children, cnt = [], len(items)
        if category == "🌾 新潟・地域経済" and niigata_nippo_items:
            children.append(para("📰 新潟日報"))
            children += [bul(t, l or None) for t, l in niigata_nippo_items]
            cnt += len(niigata_nippo_items)
            if items:
                children.append(para("📡 その他（Google News）"))
        elif category == "🌾 新潟・地域経済" and os.environ.get("NIIGATA_NIPPO_EMAIL"):
            children.append(para("⚠️ 新潟日報ログイン失敗。メール/パスワードを確認してください。"))
        if items:
            children += [bul(t, l or None) for t, l in items]
        if not children:
            children = [para("ニュース取得できませんでした")]
        blocks.append(toggle(f"{category}（{cnt}件）", children))
    blocks.append(freshness_note())

    title = f"{date_str} {time_str} ニュースダイジェスト".replace("  ", " ").strip()
    url = create_page(title, blocks, "📰")
    print(f"ニュースページ完了: {url}")
    return url

# ── 週次「今週の買い場チェック」（月曜 07:30）─────────
def get_next_earnings(ticker):
    """次回決算発表日を取得（yfinance calendar。取れなければ None）"""
    try:
        cal = yf.Ticker(ticker).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        d = dates[0]
        # datetime / date / Timestamp のいずれでも date に揃える
        if hasattr(d, "date") and callable(getattr(d, "date")):
            d = d.date()
        return d
    except Exception:
        return None

def generate_weekly_comment(picks_text, cautions_text, today_str):
    """週次のひとことコメント（Groq）。失敗しても本文は成立するのでbest-effort"""
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""あなたは長期インカム（高配当・連続増配）投資家に寄り添うアドバイザーです。本日は {today_str}（月曜の朝）です。

【今週、水準として魅力的と機械判定された銘柄】
{picks_text or "特になし"}

【保有銘柄の注意点】
{cautions_text or "特になし"}

以下を日本語で簡潔に（合計5行以内）：
1. 🛒 今週の買い方のスタンス（分割買い前提で、焦らないための一言。特定銘柄を「買え」とは書かない）
2. 💬 ひとことメモ（長期インカム投資家の心構え。毎回同じ言い回しは避ける）
※「買うべき」等の断定は禁止。「水準として魅力的」「様子見が無難」のような表現に留める。"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        return clean_ai_text(r.choices[0].message.content)
    except Exception as e:
        return ""

def create_weekly_page(date_str, now_str, title, icon="🛒"):
    print("\n==== 🛒 今週の買い場チェック作成中 ====")
    JST    = timezone(timedelta(hours=9))
    now    = datetime.now(JST)
    status = market_status(now)
    usdjpy = get_usdjpy_rate() or 1
    data   = collect_portfolio(usdjpy)

    blocks = []
    # 冒頭: 位置づけとデータ鮮度
    intro = [rt("毎週月曜の「今週の買い場チェック」。", bold=True),
             rt(" 株価・指標は本日取得分（カッコ内は株価の最終取引日）。")]
    if not status["open"]:
        intro.append(rt(f" {status['reason']}のため、株価は直近取引日の終値です。", color="orange"))
    blocks.append(callout_rt(intro, "🛒", "blue_background"))

    # ── 1. 保有銘柄: 注意ピックアップ ──
    blocks.append(h2("⚠️ 保有銘柄の注意ピックアップ"))
    cautions = []
    caution_lines = []
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if not d or "error" in d:
            cautions.append(bul(f"{name}: データ取得失敗（要確認）"))
            continue
        px   = d["price"] * usdjpy if is_usd_ticker(ticker) else d["price"]
        gain = (px - cost) / cost * 100
        sig  = data["signals"].get(name, "")
        reasons = []
        if risks:
            reasons.append("・".join(risks[:3]) + "のニュースあり")
        if gain < 0:
            reasons.append(f"取得比{gain:.1f}%の含み損")
        if (d.get("div_yield") or 0) == 0:
            reasons.append("無配（インカム方針とズレ）")
        if not reasons and ("⛔" in sig or "🔴" in sig):
            reasons.append(sig)
        if reasons:
            txt = f"{name}: {'、'.join(reasons)}"
            cautions.append(bul(txt + (f" → {sig}" if sig and sig not in txt else "")))
            caution_lines.append(txt)
            for t, l in (news or [])[:2]:
                cautions.append(toggle(f"　📎 {name}の関連ニュース", [bul(t, l or None)]))
                break
    if cautions:
        blocks.extend(cautions)
    else:
        blocks.append(para("今週は特に注意が必要な保有銘柄はありません。"))
    # 保有全体の水準（折りたたみ）
    rows = []
    for ticker, name, shares, cost, d, news, risks, goods, line in data["holdings"]:
        if not d or "error" in d:
            rows.append([cell(name), cell("取得失敗", "red")])
            continue
        px   = d["price"] * usdjpy if is_usd_ticker(ticker) else d["price"]
        gain = (px - cost) / cost * 100
        sig  = data["signals"].get(name, "")
        asof = f"({d['asof']})" if d.get("asof") else ""
        rows.append([
            cell(name),
            cell(format_price(ticker, d["price"]) + asof),
            cell(_signed(gain), "red" if gain < 0 else "green"),
            cell(f"{(d.get('div_yield') or 0):.1f}%"),
            cell(sig, signal_color(sig)),
        ])
    blocks.append(toggle("📋 保有全銘柄の水準（クリックで開く）",
                         [table(["銘柄", "現在値", "取得比", "配当", "判定"], rows)]))
    blocks.append(divider())

    # ── 2. 候補・監視: 買い場に近い順 ──
    blocks.append(h2("🎯 候補・監視銘柄の買い場チェック"))
    cands = []
    for kind, lst in (("候補", data["watch"]), ("監視", data["monitor"])):
        for ticker, name, d, news, risks, goods, line in lst:
            if not d or "error" in d:
                continue
            sig = trade_signal(d["position"], d["div_yield"], None, risks, goods)
            cands.append((kind, ticker, name, d, risks, goods, sig))
    # 52週位置が低く配当が高いほど上位（買い場に近い順）
    cands.sort(key=lambda c: c[3]["position"] - (c[3].get("div_yield") or 0) / 100.0)
    near_buy   = [c for c in cands if "🟢" in c[6]]
    pick_lines = []
    if near_buy:
        blocks.append(h3("🟢 水準として魅力的（買い場に近い）"))
        for kind, ticker, name, d, risks, goods, sig in near_buy:
            why = [f"52週レンジ下から{int(d['position']*100)}%",
                   f"配当{(d.get('div_yield') or 0):.1f}%"]
            try:
                if d.get("pbr"): why.append(f"PBR{float(d['pbr']):.2f}")
            except (ValueError, TypeError):
                pass
            if goods:
                why.append("＋" + "・".join(goods[:2]))
            txt = f"[{kind}] {name}  {format_price(ticker, d['price'])}  {'／'.join(why)}"
            blocks.append(bul(txt))
            pick_lines.append(txt)
    else:
        blocks.append(para("今週は「買い場に近い」と機械判定できる候補・監視銘柄はありません。"))
    # 候補・監視全体（折りたたみ）
    rows = []
    for kind, ticker, name, d, risks, goods, sig in cands:
        try:
            per = f"{float(d['per']):.1f}" if d.get("per") else "―"
        except (ValueError, TypeError):
            per = "―"
        try:
            pbr = f"{float(d['pbr']):.2f}" if d.get("pbr") else "―"
        except (ValueError, TypeError):
            pbr = "―"
        asof = f"({d['asof']})" if d.get("asof") else ""
        rows.append([
            cell(name), cell(kind),
            cell(format_price(ticker, d["price"]) + asof),
            cell(f"{(d.get('div_yield') or 0):.1f}%"),
            cell(per), cell(pbr),
            cell(f"{int(d['position']*100)}%"),
            cell(sig, signal_color(sig)),
        ])
    blocks.append(toggle("📋 候補・監視の全銘柄（クリックで開く）",
                         [table(["銘柄", "区分", "現在値", "配当", "PER", "PBR", "52W位置", "判定"], rows)]))
    blocks.append(divider())

    # ── 3. 今週の注目（決算予定・休場日） ──
    blocks.append(h2("📅 今週の注目"))
    events = []
    week_end = (now + timedelta(days=7)).date()
    all_stocks = [(t, n) for t, n, _, _ in HOLDINGS] + list(WATCHLIST) + list(MONITOR)
    print("  決算予定日を取得中...")
    for ticker, name in all_stocks:
        ed = get_next_earnings(ticker)
        if ed and now.date() <= ed <= week_end:
            clean = name.replace("★", "").split("連続増配")[0].strip()
            events.append((ed, f"{ed.strftime('%m/%d')} {clean} 決算発表（予定）"))
        time.sleep(0.2)
    for i in range(1, 8):
        day = now + timedelta(days=i)
        if day.weekday() < 5 and not is_tse_trading_day(day):
            events.append((day.date(), f"{day.strftime('%m/%d')} 東証休場（祝日）"))
    if events:
        for _, label in sorted(events, key=lambda e: str(e[0])):
            blocks.append(bul(label))
        blocks.append(para("※ 決算日はyfinanceの自動取得のため、正式な会社発表と数日ずれる場合があります。"))
    else:
        blocks.append(para("今週は決算発表・休場の予定を自動取得できませんでした（各社IRをご確認ください）。"))
    blocks.append(divider())

    # ── 4. AIひとこと＋締め ──
    print("  週次コメント生成中...")
    ai = generate_weekly_comment("\n".join(pick_lines), "\n".join(caution_lines), date_str)
    if ai:
        blocks.append(callout_rt([rt(ai[:1900])], "💬", "purple_background"))
    blocks.append(freshness_note())
    blocks.append(callout_rt(
        [rt("これは判断のたたき台です。特定銘柄の売買を推奨するものではなく、最終的な投資判断はご自身でお願いします。")],
        "🙏", "gray_background"))

    url = create_page(title, blocks, icon)
    print(f"週次買い場チェック完了: {url}")
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
    if arg in ("weekly", "週次", "week"):
        return "weekly"
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
            try:
                news_url = create_news_page(date_str, time_str)
            except Exception as e:
                news_url = f"生成失敗: {e}"
                print(f"  ⚠️ ニュースダイジェスト生成失敗（株レポートは完了済み）: {e}")
            print(f"\n=== 手動(夜)完了 ===\n株式 : {stock_url}\nニュース : {news_url}")
            return
        print(f"\n=== 手動(朝)完了 ===\n株式 : {stock_url}")
    elif mode == "weekly":
        title = f"{date_str} 今週の買い場チェック"
        stock_url = create_weekly_page(date_str, now_str, title, "🛒")
        print(f"\n=== 週次完了 ===\n買い場チェック : {stock_url}")
    elif mode == "morning":
        title = f"{date_str} {time_str} 朝の株レポート"
        stock_url = create_morning_page(date_str, now_str, title, "🌅")
        print(f"\n=== 朝の完了 ===\n株式 : {stock_url}")
    else:
        title = f"{date_str} {time_str} 夜の株レポート"
        stock_url = create_evening_page(date_str, now_str, title, "🌙")
        # 夜は振り返り回。ニュースダイジェストも夜に付ける
        # （ニュース側が失敗しても株レポートは完了扱いにする＝全滅防止）
        try:
            news_url = create_news_page(date_str, time_str)
        except Exception as e:
            news_url = f"生成失敗: {e}"
            print(f"  ⚠️ ニュースダイジェスト生成失敗（株レポートは完了済み）: {e}")
        print(f"\n=== 夜の完了 ===\n株式 : {stock_url}\nニュース : {news_url}")

if __name__ == "__main__":
    main()
