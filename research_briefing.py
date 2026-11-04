"""Report builders: public news and private investment never share inputs."""
from datetime import timedelta
import re
from research_core import (block, choose_tip, complete_blocks, timestamp, legacy_constant, JST)
from research_sources import RELEVANCE, market_data, ticker_news
from investment_analysis import fresh, number


def prop(page, name, kind):
    p = page.get("properties", {}).get(name, {}).get(kind)
    if kind in {"rich_text", "title"}:
        return "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in p or [])
    if kind == "select":
        return (p or {}).get("name")
    if kind == "date":
        return (p or {}).get("start")
    return p


def portfolio_rows(pages):
    rows, seen = [], set()
    for p in pages:
        ticker = prop(p, "コード", "rich_text").strip()
        if not ticker:
            continue
        if not re.fullmatch(r"[A-Za-z0-9^.=\-]{1,20}", ticker) or ticker in seen:
            raise ValueError("Invalid or duplicate portfolio ticker")
        seen.add(ticker)
        rows.append({"ticker": ticker, "name": prop(p, "銘柄名", "title"),
                     "kind": prop(p, "区分", "select"),
                     "holdings_asof": prop(p, "保有確認日", "date")})
    return rows


def title_key(stream, slot):
    key = f"研究v2:{stream}:{slot.date()}"
    label = "一般ニュース" if stream == "news" else "My Investment｜Daily Briefing"
    return f"{slot.date()} {label} [{key}]", key


def header(slot, now):
    return block(f"対象日: {slot.date()} / 予定: {slot.isoformat()} / 実際の取得: {now.isoformat()}\n"
                 "予定時刻から遅れて掲載される場合があります。公開日・価格基準日は各情報に記載。")


def news_report(slot, now, articles, errors, summaries):
    title, key = title_key("news", slot)
    body = [header(slot, now)]
    if errors:
        body.append(block("一部取得失敗: " + " / ".join(sorted(set(errors)))))
    if not articles:
        body.append(block("新着の特記事項なし。公開日確認済み・未掲載の記事はありません。取得失敗の有無は上記を確認。"))
    for a in articles:
        body.append(block(a["category"] + "｜" + a["title"], "heading_2"))
        body.append(block(f"公開: {a['published_at']} / 取得: {a['retrieved_at']}\n"
                          f"情報源: {a['source']} / {a['url_kind']}\n{a['url']}\n"
                          "確認できた内容（配信元の見出し・抜粋）: " + (a["excerpt"][:300] or "本文抜粋なし・原文確認が必要")))
        summary, background = summaries.get(a["id"], ("要約未生成。上記の見出し・抜粋を参照。", "資料不足"))
        body.append(block("AI要約（未検証）: " + summary + "\n背景（AI・未検証）: " + background +
                          "\n仕事・生活への確認ポイント: " + RELEVANCE.get(a["category"], "必要に応じて原文を確認。")))
    manifest = {"key": key, "stream": "news", "retrieved_at": now.isoformat(),
                "article_ids": [a["id"] for a in articles],
                "categories": sorted({a["category"] for a in articles}), "degraded": bool(errors)}
    return title, key, complete_blocks(body, manifest)


def quote_text(data, now):
    if data.get("error"):
        return data["error"]
    # Missing exact timestamp is retained as a session date, never stamped as now.
    asof = data.get("price_asof")
    session = data.get("session_date")
    if asof:
        valid = fresh(asof, now, 7)
    else:
        try:
            from datetime import date
            age = (now.astimezone(JST).date() - date.fromisoformat(session)).days
            valid = 0 <= age <= 7
        except (ValueError, TypeError):
            valid = False
    if not valid or number(data.get("price")) is None:
        return "価格の基準日未確認または古いデータ。現在値として掲載しません。"
    price = data["price"]
    change = data.get("change_pct")
    change_str = f"{change:+.2f}%" if isinstance(change, (float, int)) else "未取得"
    return (f"参考価格 {price:g} {data.get('currency') or '通貨未確認'} / 前営業日比 {change_str}\n"
            f"価格基準: {asof or session + '（取引日・時刻不明）'} / 取得: {data['retrieved_at']}\n"
            f"{data['provider_note']} / {data['source_url']}")


def metrics_text(data):
    if data.get("error"):
        return "指標は取得失敗"
    fields = [("per", "PER"), ("pbr", "PBR"), ("roe", "ROE（比率）"),
              ("payout", "配当性向（比率）"), ("annual_dps", "1株年間配当参考値"),
              ("revenue_growth", "売上成長率（比率）"), ("earnings_growth", "利益成長率（比率）"),
              ("free_cf", "FCF"), ("debt_to_equity", "負債/自己資本（提供者単位）")]
    values = " / ".join(label + ": " + (str(data.get(k)) if data.get(k) is not None else "未取得") for k, label in fields)
    return values + "\n財務対象期参考: " + str(data.get("financial_period") or "未確認") + " / 個別指標の基準・配当期間は一次資料未照合。"


def research_priority(data, now):
    # A research queue, not a buy score. Never use stale market data.
    if data.get("asset_type") != "EQUITY" or not fresh(data.get("price_asof"), now, 7):
        return None
    if data.get("sector") == "Financial Services":
        return None  # Separate bank/insurer criteria required.
    price, dps = number(data.get("price")), number(data.get("annual_dps"))
    payout, cf = number(data.get("payout")), number(data.get("free_cf"))
    growth = number(data.get("earnings_growth"))
    if not price or dps is None or payout is None or cf is None or growth is None:
        return None
    dy = dps / price * 100
    if not 2.5 <= dy <= 10 or payout > .7 or cf <= 0:
        return None
    return dy


def investment_report(slot, now, pages, funds, assets, forecast, history, quote=market_data, news=ticker_news):
    rows = portfolio_rows(pages)
    if len(rows) > 45:
        raise ValueError("Portfolio exceeds bounded run; configure paging before publication")
    title, key = title_key("investment", slot)
    errors = []
    body = [header(slot, now), block("1. 今日の市場概況", "heading_2")]
    try:
        import jpholiday
        holiday = now.weekday() >= 5 or jpholiday.is_holiday(now.date()) or (now.month == 1 and now.day <= 3) or (now.month == 12 and now.day == 31)
        body.append(block("日本市場: 休場日。直近営業日の値を参照。" if holiday else "日本市場: 営業日判定。朝の時点では直近営業日の値を参照。"))
    except ImportError:
        body.append(block("日本市場の休場判定ライブラリ未導入。取引日を確認してください。"))
    for ticker, name in legacy_constant("INDICES")[:2] + [("JPY=X", "ドル円")]:
        data = quote(ticker, now)
        if data.get("error"):
            errors.append("market_overview")
        body.append(block(name + "\n" + quote_text(data, now)))
    body += [block("2. 保有銘柄の重要な変化", "heading_2"),
             block("以下はNotionの登録上の区分です。保有確認日がない銘柄は、現在の保有数量・取得単価を未確認として扱います。")]
    results, news_ids, events = [], [], []
    seen_news = {item for report in history for item in report.get("article_ids", [])}
    for row in rows:
        data = quote(row["ticker"], now)
        results.append((row, data))
        if data.get("error"):
            errors.append("market")
        if row["kind"] != "保有":
            continue
        name = row["name"].split("★")[0].strip()
        try:
            fetched = news(name, now)
            items = []
            for item in fetched:
                if item["id"] not in seen_news:
                    seen_news.add(item["id"])
                    items.append(item)
        except Exception:
            items = []
            errors.append("news")
            change = "関連ニュース取得失敗。重要変化の有無は判断できません。"
        else:
            change = "\n".join(a["title"] + " / " + a["published_at"] + " / " + a["url"] +
                               "（検索経由・原文要確認）" for a in items) or "取得範囲で特記事項なし。適時開示の網羅性は未保証。"
            news_ids.extend(a["id"] for a in items)
        body.append(block(name + "（登録上: 保有 / 保有確認日: " + str(row["holdings_asof"] or "未確認") +
                          "）\n" + quote_text(data, now) + "\n" + metrics_text(data) + "\n変化: " + change +
                          "\n確認ポイント: 業績・配当方針への継続的影響を会社IRで照合。"))
    body.append(block("3. 買い増し候補のチェック", "heading_2"))
    ranked = [(research_priority(d, now), r, d) for r, d in results]
    ranked = sorted([x for x in ranked if x[0] is not None], key=lambda x: (-x[0], x[1]["ticker"]))[:3]
    if not ranked:
        body.append(block("条件を満たす調査候補なし、または根拠不足。未取得と非該当を区別し、買い時とは判定しません。"))
    for _, row, data in ranked:
        body.append(block(row["name"] + "｜調査優先候補（購入推奨ではありません）\n" + metrics_text(data) +
                          "\n" + data["source_url"] + " / 取得: " + data["retrieved_at"] +
                          "\n投資仮説: 利益成長と配当の持続性が両立するか確認。\n"
                          "懸念: 一時利益・特別配当・分割・指標の時点差。一次資料・同業比較・保有配分の確認まで買い増し判断は保留。"))
    body.append(block("4. 新規発掘銘柄", "heading_2"))
    excluded = {r["ticker"] for r in rows} | {prop(p, "証券コード", "rich_text") for p in forecast}
    excluded |= {t for h in history for t in h.get("discovery_tickers", [])}
    pool = [p for p in legacy_constant("DISCOVERY_POOL") if p[0] not in excluded]
    if pool:
        shift = now.date().toordinal() % len(pool)
        pool = pool[shift:] + pool[:shift]
    discoveries = []
    for ticker, name, sector in pool[:2]:
        data = quote(ticker, now)
        if data.get("error"):
            errors.append("discovery")
            continue
        discoveries.append(ticker)
        body.append(block(name + " / " + sector + "｜未評価の調査対象\n" +
                          quote_text(data, now) + "\n" + metrics_text(data) +
                          "\n企業概要（提供者参考）: " + data.get("overview", "")[:240] +
                          "\n仮説: 長期の利益・配当成長を確認。懸念: 景気循環、財務、還元方針。"
                          "有望と確定せず、一次資料の照合後に再評価。保有・監視DBには自動登録しません。"))
    if not discoveries:
        body.append(block("新規発掘の特記事項なし。重複を除外、または取得できる根拠が不足。"))
    body.append(block("5. ポートフォリオへの影響", "heading_2"))
    fund_dates = sorted({prop(p, "基準日", "date") or "未入力" for p in funds})
    asset_dates = sorted({prop(p, "日付", "date") for p in assets if prop(p, "日付", "date")})
    body.append(block("集中度・現在資産額は算定保留。保有確認日、通貨、投信評価の出典を揃えてから算定します。\n"
                      "投信マスタ基準日: " + ", ".join(fund_dates) + "\n資産推移の最新記録日: " +
                      (asset_dates[-1] if asset_dates else "未入力") +
                      "（実残高の照合済みとは限りません）\n"
                      "S&P500・全世界・先進国の中身の重複、円以外の通貨の影響、日本株の業種偏りを確認。"))
    tip = choose_tip(history, now)
    body += [block("6. 今日の投資Tips", "heading_2"), block(tip["title"] + "\n" + tip["text"])]
    body.append(block("7. 今後注目するイベント・決算", "heading_2"))
    for row, data in results:
        for day in data.get("earnings_dates", []):
            try:
                from datetime import date
                d = date.fromisoformat(day[:10])
                if now.date() <= d <= (now + timedelta(days=14)).date():
                    events.append(f"{row['name']}: {day}（提供者の予定・会社IR未照合） / {data['source_url']} / 取得: {data['retrieved_at']}")
            except ValueError:
                pass
    body.append(block("\n".join(events) if events else "今後14日間の決算日を確認できませんでした。予定がないという意味ではありません。"))
    if errors:
        body.append(block("一部の情報取得に失敗しました。該当欄に明示し、過去値は流用していません。"))
    manifest = {"key": key, "stream": "investment", "retrieved_at": now.isoformat(),
                "tip_id": tip["id"], "discovery_tickers": discoveries, "article_ids": news_ids,
                "degraded": bool(errors)}
    return title, key, complete_blocks(body, manifest)


