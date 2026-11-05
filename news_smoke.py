"""Live PUBLIC news only: no daily_report import, Notion, AI or holdings."""
import ast
import json
from pathlib import Path
from news_content import collect_rss, general_news_feeds


def main():
    import requests
    import feedparser
    # Reuse existing public feed config without executing private module setup.
    tree = ast.parse(Path(__file__).with_name("daily_report.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "NEWS_FEEDS" for t in n.targets)]
    if len(nodes) != 1:
        raise ValueError("Expected exactly one public feed configuration")
    feeds = general_news_feeds(ast.literal_eval(nodes[0].value))
    total = 0
    with requests.Session() as session:
        for category, urls in feeds.items():
            items = collect_rss(urls, session.get, feedparser)
            total += len(items)
            print(json.dumps({"category": category, "accepted": len(items),
                              "completed_feeds": items.completed_feeds,
                              "failed_feeds": items.failed_feeds}, ensure_ascii=False))
    if total == 0:
        raise RuntimeError("No eligible public news obtained")
    print(json.dumps({"public_news_smoke": "passed", "accepted_total": total,
                      "notion_write_tested": False, "ai_quality_tested": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Public news smoke failed; raw details suppressed")
