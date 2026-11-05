"""Public news identity and freshness checks; no private data access."""
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

EXTRA_NEWS_QUERIES = {
    "金融・金利・為替": "日本銀行 金利 為替",
    "中小企業・M&A・事業承継": "中小企業 M&A 事業承継",
}

# Reused from PR #6 research_core (1ef3d12); this is the news route's
# canonical URL/publication contract. No portfolio configuration is imported.
def canonical_url(value):
    try:
        u = urlsplit(value)
        if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password:
            return None
        if u.port not in {None, 80, 443}:
            return None
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, urlencode(sorted(query)), ""))
    except (ValueError, TypeError):
        return None

def publication_time(entry):
    value = entry.get("published") or entry.get("dc_date")
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return stamp if stamp.tzinfo else None

class NewsHeadline(str):
    """String-compatible headline with source evidence retained for rendering."""
    def __new__(cls, text, entry, feed_url, now):
        item = super().__new__(cls, text)
        item.published_at = publication_time(entry).isoformat()
        item.retrieved_at = now.isoformat()
        item.source = str((entry.get("source") or {}).get("title") or urlsplit(feed_url).hostname)
        item.url_kind = ("検索経由・原文URL未確認" if urlsplit(entry["link"]).hostname == "news.google.com"
                         else "配信元記事URL")
        return item

class RssItems(list):
    def __init__(self, items, completed_feeds, failed_feeds):
        super().__init__(items)
        self.completed_feeds = completed_feeds
        self.failed_feeds = failed_feeds

def collect_rss(urls, fetch, parser, now=None, max_items=5):
    now = now or datetime.now(timezone.utc)
    accepted, seen, completed, failed = [], set(), 0, 0
    for url in urls:
        try:
            response = fetch(url, timeout=(5, 20))
            response.raise_for_status()
            if len(response.content) > 2000000:
                raise ValueError("Feed too large")
            feed = parser.parse(response.content)
            if not feed.entries and feed.get("bozo"):
                raise ValueError("Invalid RSS")
            completed += 1
            for entry in feed.entries:
                link = canonical_url(entry.get("link"))
                title = entry.get("title", "").strip()
                if not link or len(title) <= 5 or not fresh_entry(entry, now) or link in seen:
                    continue
                seen.add(link)
                accepted.append((NewsHeadline(title, entry, url, now), link))
        except Exception:
            failed += 1
    return RssItems(accepted[:max_items], completed, failed)

def article_key(title, link):
    return canonical_url(link) or title.strip()

def fresh_entry(entry, now=None):
    # RSS without a publication timestamp is not asserted to be today's news.
    stamp = publication_time(entry)
    try:
        now = now or datetime.now(timezone.utc)
        return stamp is not None and timedelta(0) <= now - stamp <= timedelta(days=3)
    except (ValueError, TypeError, OverflowError):
        return False

