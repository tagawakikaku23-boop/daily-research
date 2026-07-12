#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
月次資産レビュー（毎月1日の朝に実行）
- 株・ETF: 監視銘柄リスト（保有）から実測
- 投信・年金: 投信・積立マスタの口数×指数推定（毎月の積立を自動加算・二重加算ガード付き）
- 現金・ポイント: マスタの金額（ユーザー編集可）
- 出力: 資産推移DBに1行＋「📊 月次資産レビュー」ページ（AI助言付き）
"""
import daily_report as m   # ヘルパー・APIキー・stdout UTF-8化を再利用

import os
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta

MASTER_DB  = os.environ.get("NOTION_ASSET_MASTER_DB_ID", "39bfc83b-0e47-818e-9749-d57197c0deb9")
HISTORY_DB = os.environ.get("NOTION_ASSET_HISTORY_DB_ID", "39bfc83b-0e47-8170-800d-c8a67c14515c")
H = {"Authorization": f"Bearer {m.NOTION_API_KEY}",
     "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
JST = timezone(timedelta(hours=9))

PROXY = {"SP500": "^GSPC", "先進国": "TOK", "新興国": "EEM",
         "日経225": "^N225", "全世界": "ACWI"}


def ptext(prop):
    arr = (prop or {}).get("title") or (prop or {}).get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in arr).strip()

def pnum(prop):
    return (prop or {}).get("number")

def psel(prop):
    return ((prop or {}).get("select") or {}).get("name", "")

def query_all(dbid, sorts=None, page_size=100):
    rows, cursor = [], None
    while True:
        body = {"page_size": page_size}
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"https://api.notion.com/v1/databases/{dbid}/query",
                          headers=H, json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
        rows += j["results"]
        if not j.get("has_more"):
            break
        cursor = j["next_cursor"]
    return rows


def latest_close(ticker):
    h = yf.Ticker(ticker).history(period="7d")["Close"].dropna()
    return float(h.iloc[-1]) if len(h) else None


def stocks_value(usdjpy):
    """保有株・ETFの時価合計（実測）"""
    total, fail = 0.0, []
    for ticker, name, shares, cost in m.HOLDINGS:
        d = m.get_stock_data(ticker)
        if not d or "error" in d:
            fail.append(name)
            continue
        px = d["price"] * usdjpy if m.is_usd_ticker(ticker) else d["price"]
        total += px * shares
    return total, fail


def generate_review_comment(facts, today_str):
    try:
        from groq import Groq
        client = Groq(api_key=m.GROQ_API_KEY)
        prompt = f"""あなたは長期・分散・低コストを重んじる投資アドバイザーです（山崎元・両学長の思想に近い立場）。本日は {today_str}。
ユーザーの方針: ①インデックス積立（S&P500中心・月4万円）②日本の高配当・連続増配株、の2本柱。短期売買はしない。

【今月の資産スナップショット（推定含む・信頼できる値）】
{facts}

月次レビューとして以下を日本語で6〜8行:
1. 前月比の解釈（相場要因か入金要因か、一喜一憂しない視点で）
2. 資産配分の評価（リスク資産比率・現金比率が方針に照らして問題ないか）
3. 来月に向けた一言（淡々と積立継続の観点。売買を煽らない）

厳守: 出力は日本語のみ。数字は提供値だけを使い創作しない。断定や煽りを避ける。
「暴落が来る」等の予言はしない。同じ言い回しの繰り返しを避ける。"""
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        return m.clean_ai_text(r.choices[0].message.content)
    except Exception as e:
        return f"（AIコメント生成失敗: {e}）"


def main():
    now = datetime.now(JST)
    ym = now.strftime("%Y-%m")
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    print(f"\n==== 📊 月次資産レビュー {ym} ====")

    # 現在値（指数・為替）
    idx_now = {}
    for key, tk in PROXY.items():
        v = latest_close(tk)
        if v:
            idx_now[key] = v
    fx_now = m.get_usdjpy_rate()
    print("  指数:", {k: round(v, 1) for k, v in idx_now.items()}, "為替:", round(fx_now or 0, 2))

    # マスタを読む
    rows = query_all(MASTER_DB)
    cash = points = funds = pension = 0.0
    skipped = []
    for row in rows:
        p = row["properties"]
        name = ptext(p.get("名称"))
        kind = psel(p.get("区分"))
        if kind == "現金":
            cash += pnum(p.get("金額")) or 0
            continue
        if kind == "ポイント":
            points += pnum(p.get("金額")) or 0
            continue
        # 投信・年金: 口数×指数推定
        units    = pnum(p.get("口数")) or 0
        nav_base = pnum(p.get("基準価額")) or 0
        idx_key  = psel(p.get("連動指数"))
        idx_base = pnum(p.get("指数基準値"))
        fx_base  = pnum(p.get("為替基準値"))
        if not (units and nav_base and idx_key in idx_now and idx_base):
            skipped.append(name)
            continue
        nav_est = nav_base * (idx_now[idx_key] / idx_base)
        if fx_base and fx_now:
            nav_est *= (fx_now / fx_base)

        # 毎月の積立を自動加算（同月2回目以降はスキップ）
        tsumitate = pnum(p.get("月額積立")) or 0
        applied   = ptext(p.get("最終積立反映月"))
        if tsumitate > 0 and applied != ym and nav_est > 0:
            add_units = tsumitate / nav_est * 10000
            units += add_units
            try:
                requests.patch(f"https://api.notion.com/v1/pages/{row['id']}", headers=H,
                    json={"properties": {
                        "口数": {"number": round(units, 0)},
                        "最終積立反映月": {"rich_text": [{"text": {"content": ym}}]}}},
                    timeout=30).raise_for_status()
                print(f"  積立反映: {name} +{tsumitate:,}円 (+{add_units:,.0f}口)")
            except Exception as e:
                print(f"  ⚠️ 積立書き戻し失敗 {name}: {e}")

        value = units * nav_est / 10000
        if kind == "年金":
            pension += value
        else:
            funds += value

    # 株・ETF（実測）
    stocks, fail = stocks_value(fx_now or 1)
    total = cash + points + funds + pension + stocks
    risk_ratio = (total - cash - points) / total * 100 if total else 0

    # 前月比（資産推移DBの最新行と比較）
    prev_rows = query_all(HISTORY_DB, sorts=[{"property": "日付", "direction": "descending"}], page_size=5)
    prev_total = None
    for r0 in prev_rows:
        t0 = pnum(r0["properties"].get("合計"))
        if t0 and ptext(r0["properties"].get("年月")) != ym:
            prev_total = t0
            break
    mom = (total - prev_total) / prev_total * 100 if prev_total else None

    print(f"  現金¥{cash:,.0f} 株¥{stocks:,.0f} 投信¥{funds:,.0f} 年金¥{pension:,.0f} pt¥{points:,.0f}")
    print(f"  合計¥{total:,.0f}  リスク資産{risk_ratio:.1f}%  前月比{f'{mom:+.2f}%' if mom is not None else '―(初回)'}")

    # 資産推移DBへ1行追加
    props = {
        "年月": {"title": [{"text": {"content": ym}}]},
        "日付": {"date": {"start": date_str}},
        "現金": {"number": round(cash)},
        "株式ETF": {"number": round(stocks)},
        "投資信託": {"number": round(funds)},
        "年金": {"number": round(pension)},
        "ポイント": {"number": round(points)},
        "合計": {"number": round(total)},
        "リスク資産比率%": {"number": round(risk_ratio, 1)},
    }
    if mom is not None:
        props["前月比%"] = {"number": round(mom, 2)}
    try:
        requests.post("https://api.notion.com/v1/pages", headers=H,
                      json={"parent": {"database_id": HISTORY_DB}, "properties": props},
                      timeout=30).raise_for_status()
        print("  資産推移DBに行を追加")
    except Exception as e:
        print(f"  ⚠️ 資産推移の行追加失敗: {e}")

    # AI助言
    facts = (f"総資産: ¥{total:,.0f}（前月比 {f'{mom:+.2f}%' if mom is not None else '初回のため無し'}）\n"
             f"内訳: 現金¥{cash:,.0f}({cash/total*100:.1f}%) 株式ETF¥{stocks:,.0f}({stocks/total*100:.1f}%) "
             f"投資信託¥{funds:,.0f}({funds/total*100:.1f}%) 年金¥{pension:,.0f}({pension/total*100:.1f}%) "
             f"ポイント¥{points:,.0f}\nリスク資産比率: {risk_ratio:.1f}%\n"
             f"毎月の積立: 4万円（S&P500中心・自動反映済み）。iDeCoは免除期間中で停止。")
    print("  AI助言生成中...")
    comment = generate_review_comment(facts, date_str)

    # レビューページ生成
    def yen(v):
        return f"¥{v:,.0f}"
    mom_str = f"{mom:+.2f}%" if mom is not None else "―（初回）"
    mom_color = "green_background" if (mom or 0) >= 0 else "red_background"
    blocks = [
        m.callout_rt([m.rt(f"生成: {date_str} {time_str}", bold=True),
                      m.rt("　｜　月1回の資産レビュー（投信・年金は指数推定）")], "📊", "blue_background"),
        m.columns([
            [m.card("💰 総資産", [yen(total)], "💰", "gray_background")],
            [m.card("📈 前月比", [mom_str], "📈", mom_color)],
            [m.card("⚖️ リスク資産比率", [f"{risk_ratio:.1f}%"], "⚖️", "yellow_background")],
        ]),
        m.h2("📋 資産の内訳"),
        m.table(["資産クラス", "金額", "構成比"], [
            [m.cell("投資信託（推定）"), m.cell(yen(funds)), m.cell(f"{funds/total*100:.1f}%")],
            [m.cell("株式・ETF（実測）"), m.cell(yen(stocks)), m.cell(f"{stocks/total*100:.1f}%")],
            [m.cell("現金・預金"), m.cell(yen(cash)), m.cell(f"{cash/total*100:.1f}%")],
            [m.cell("年金（推定）"), m.cell(yen(pension)), m.cell(f"{pension/total*100:.1f}%")],
            [m.cell("ポイント"), m.cell(yen(points)), m.cell(f"{points/total*100:.1f}%")],
        ]),
        m.h2("🧭 今月のレビュー（AI）"),
        m.callout_rt([m.rt(comment[:1900])], "🧭", "purple_background"),
        m.callout_rt([m.rt("※ 投信・年金は指数からの推定値（誤差0.5%程度）。毎月の積立4万円は自動反映。"
                           "マネフォのPDFをClaudeに渡すと実際の口数・現金に補正されます。")],
                     "🕒", "gray_background"),
    ]
    if skipped or fail:
        note = "データ取得できず除外: " + "・".join(skipped + fail)
        blocks.append(m.callout_rt([m.rt(note)], "⚠️", "orange_background"))

    url = m.create_page(f"{date_str} {time_str} 月次資産レビュー", blocks, "📊")
    print(f"月次レビュー完了: {url}")


if __name__ == "__main__":
    main()
