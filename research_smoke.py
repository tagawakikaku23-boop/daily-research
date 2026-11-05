"""No secrets or private data; exercise live public feeds and one index."""
import json
from datetime import datetime
from research_core import JST
from research_sources import collect_news, market_data
now = datetime.now(JST)
try:
    articles, failures = collect_news(now)
    print(json.dumps({"feeds_accepted_articles": len(articles), "failed_feeds": len(failures)}))
    quote = market_data("^N225", now)
    print(json.dumps({"index_price_available": quote.get("price") is not None,
                      "index_asof_available": bool(quote.get("price_asof") or quote.get("session_date"))}))
    if not articles or quote.get("error"):
        raise SystemExit(1)
except Exception:
    raise SystemExit("Public source smoke failed; no private data involved")

