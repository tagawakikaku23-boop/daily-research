# -*- coding: utf-8 -*-
import daily_report as m

print("=== カテゴリRSS ===")
for cat, urls in m.NEWS_FEEDS.items():
    items = m.get_rss_news(urls, max_items=3, retries=1, wait=1)
    print(f"{cat}: {len(items)}件", ("OK: " + items[0][0][:40]) if items else "★取得ゼロ")

print("=== マクロニュース ===")
mn = m.get_macro_news("morning", max_items=4)
print("morning:", len(mn), "件")

print("=== 銘柄ニュース ===")
n, r, g = m.get_ticker_news("トヨタ自動車", max_items=2)
print("トヨタ:", len(n), "件")
