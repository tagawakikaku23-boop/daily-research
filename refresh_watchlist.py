#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
監視銘柄リスト（Notion DB）の指標を更新するスクリプト。
- 場中30分ごとに実行する想定（GitHub Actions）
- 毎回更新: 現在値/前日比%/年初来%/配当利回り%/PER/PBR/52W位置%/判定/更新時刻
- 空のときだけ一度設定: 事業内容(自動翻訳) / Claudeコメント(AI) / 株主優待(→要確認)
"""
import daily_report as m   # 既存のロジック・APIキーを再利用（stdoutのUTF-8化もこちらで実施）

import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta

HEADERS = {"Authorization": f"Bearer {m.NOTION_API_KEY}",
           "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
JST = timezone(timedelta(hours=9))


def ptext(prop):
    if not prop:
        return ""
    arr = prop.get("title") or prop.get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in arr).strip()


def query_rows():
    rows, cursor, guard = [], None, 0
    while guard < 20:
        guard += 1
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{m.NOTION_WATCHLIST_DB_ID}/query",
            headers=HEADERS, json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
        rows += j.get("results", [])
        if not j.get("has_more"):
            break
        cursor = j.get("next_cursor")
    return rows


def ytd_pct(ticker, price):
    try:
        h = yf.Ticker(ticker).history(period="ytd")["Close"].dropna()
        if len(h) >= 1 and h.iloc[0]:
            return (price - float(h.iloc[0])) / float(h.iloc[0]) * 100
    except Exception:
        pass
    return None


def translate_summary(ticker, name):
    """yfinanceの英語事業概要を1行の日本語に要約（一度だけ）"""
    try:
        info = yf.Ticker(ticker).info
        summ = info.get("longBusinessSummary") or ""
        if not summ:
            return ""
        from groq import Groq
        c = Groq(api_key=m.GROQ_API_KEY)
        r = c.chat.completions.create(
            model=m.groq_model(), **m.groq_extra(),
            messages=[{"role": "user", "content":
                       f"次の会社（{name}）の事業内容を、日本語で1文・40字以内に要約。"
                       f"誇張せず事実のみ。\n\n{summ[:1500]}"}],
            max_tokens=300)
        return m.clean_ai_text(r.choices[0].message.content).strip().replace("\n", " ")
    except Exception as e:
        print(f"    事業内容生成失敗 {ticker}: {e}")
        return ""


def claude_comment(name, d):
    """長期インカム視点の一言所感（一度だけ。AIで生成）"""
    try:
        from groq import Groq
        c = Groq(api_key=m.GROQ_API_KEY)
        facts = (f"配当{(d.get('div_yield') or 0):.1f}% "
                 f"PER{d.get('per') or '―'} PBR{d.get('pbr') or '―'} "
                 f"52W位置{int(d['position']*100)}%")
        r = c.chat.completions.create(
            model=m.groq_model(), **m.groq_extra(),
            messages=[{"role": "user", "content":
                       f"長期インカム（高配当・連続増配）投資家向けに、{name}（{facts}）への"
                       f"一言所感を日本語50字以内で。誇張・断定を避け、提示数値の範囲で。"}],
            max_tokens=300)
        return m.clean_ai_text(r.choices[0].message.content).strip().replace("\n", " ")
    except Exception as e:
        print(f"    コメント生成失敗 {name}: {e}")
        return ""


def num(v, digits=2):
    import math
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, digits)
    except (TypeError, ValueError):
        return None


def main():
    now = datetime.now(JST)
    stamp = now.strftime("%m/%d %H:%M")
    print(f"\n=== 監視銘柄 指標更新 {now.strftime('%Y-%m-%d %H:%M')} ===")
    rows = query_rows()
    print(f"対象 {len(rows)} 銘柄")
    usdjpy = m.get_usdjpy_rate() or 1
    ok = 0
    for row in rows:
        pid = row["id"]
        p   = row.get("properties", {})
        code = ptext(p.get("コード"))
        name = ptext(p.get("銘柄名"))
        if not code:
            continue
        d = m.get_stock_data(code)
        if not d or "error" in d:
            print(f"  {code} {name}: 取得失敗")
            continue

        # 直近の朝夜レポートが書き込んだニュースフラグを判定に反映（レポートと同一基準）
        # フラグ語は daily_report の分類辞書（NEWS_RISK/NEWS_POSITIVE）の定義値と照合する
        flags_txt = ptext(p.get("ニュースフラグ"))
        words = [w for w in flags_txt.split("・") if w] if flags_txt else []
        risks = [w for w in words if w in set(m.NEWS_RISK.values())]
        goods = [w for w in words if w in set(m.NEWS_POSITIVE.values())]

        props = {
            "現在値":      {"number": num(d["price"])},
            "前日比%":     {"number": num(d["chg_pct"])},
            "年初来%":     {"number": num(ytd_pct(code, d["price"]))},
            "配当利回り%": {"number": num(d.get("div_yield"))},
            "PER":        {"number": num(d.get("per"))},
            "PBR":        {"number": num(d.get("pbr"))},
            "52W位置%":    {"number": num(d["position"] * 100, 0)},
            "判定":        {"rich_text": [{"text": {"content": m.trade_signal(d["position"], d.get("div_yield") or 0, None, risks, goods)}}]},
            "更新時刻":    {"rich_text": [{"text": {"content": stamp}}]},
        }

        # 一度だけ設定（空欄のときのみ）
        if not ptext(p.get("事業内容")):
            s = translate_summary(code, name)
            if s:
                props["事業内容"] = {"rich_text": [{"text": {"content": s[:300]}}]}
        if not ptext(p.get("Claudeコメント")):
            cc = claude_comment(name, d)
            if cc:
                props["Claudeコメント"] = {"rich_text": [{"text": {"content": cc[:300]}}]}
        if not (p.get("株主優待", {}).get("select")):
            props["株主優待"] = {"select": {"name": "要確認"}}

        try:
            r = requests.patch(f"https://api.notion.com/v1/pages/{pid}",
                               headers=HEADERS, json={"properties": props}, timeout=30)
            r.raise_for_status()
            ok += 1
            print(f"  ✅ {code} {name}: {d['price']:.1f} ({d['chg_pct']:+.1f}%)")
        except Exception as e:
            print(f"  ⚠️ {code} {name}: 更新失敗 {e}")

    print(f"=== 完了: {ok}/{len(rows)} 更新 ===")


if __name__ == "__main__":
    main()
