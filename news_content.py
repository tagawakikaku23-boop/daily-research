"""Public news identity and freshness checks; no private data access."""
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote

CATEGORIES = (
    "日本経済", "世界経済", "金融・マーケット", "新潟", "企業・M&A",
    "通信・IT", "AI・テクノロジー", "社会・その他",
)
QUERIES = (
    "日本 GDP OR 景気 OR 賃金", "世界経済 OR 世界貿易", "金利 OR 為替 OR 日銀",
    "新潟", "M&A OR 事業承継 OR 買収", "通信 OR 携帯 OR IT",
    "生成AI OR 半導体 OR ロボット", "社会 OR 防災 OR 教育",
)
LEGACY = {
    "📰 日本経済": "日本経済", "🌐 世界経済": "社会・その他",
    "💻 IT・テクノロジー": "通信・IT", "📱 携帯キャリア・ガジェット": "通信・IT",
    "🌾 新潟・地域経済": "新潟",
}
TOPICS = (
    ("新潟", r"新潟|長岡市|佐渡|上越市"),
    ("企業・M&A", r"ma|事業承継|買収|経営統合|合併"),
    ("AI・テクノロジー", r"生成ai|人工知能|半導体|ロボット|chatgpt"),
    ("通信・IT", r"携帯|通信|スマホ|サイバー|クラウド"),
    ("金融・マーケット", r"金利|為替|日銀|株式市場|国債"),
)

def general_news_feeds(existing):
    feeds = {category: [] for category in CATEGORIES}
    for name, urls in existing.items():
        feeds[LEGACY.get(name, name if name in feeds else "社会・その他")].extend(urls)
    for category, query in zip(CATEGORIES, QUERIES):
        feeds[category].append("https://news.google.com/rss/search?q=" + quote(query)
                               + "&hl=ja&gl=JP&ceid=JP:ja")
    return feeds

def headline_key(headline):
    title = unicodedata.normalize("NFKC", str(headline)).casefold()
    source = getattr(headline, "source", "")
    suffix = " - " + unicodedata.normalize("NFKC", source).casefold()
    if source and title.endswith(suffix):
        title = title[:-len(suffix)]
    return re.sub(r"[\W_]+", "", title)

def classify(headline, fallback):
    title = headline_key(headline)
    tags = [category for category, pattern in TOPICS
            if re.search(pattern, title)]
    primary = tags[0] if tags else fallback
    return primary, tuple(category for category in tags if category != primary)

def publisher_key(headline, link):
    source = getattr(headline, "source", "") or urlsplit(link).hostname or ""
    key = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", source).casefold())
    return "nikkei" if "nikkei" in key or "日経" in key else key


def balance_articles(candidates):
    """Assign each story once and cap exposure to one publisher."""
    grouped = {category: [] for category in CATEGORIES}
    urls, titles, publishers = set(), set(), Counter()
    ordered = sorted(candidates, key=lambda row: (
        getattr(row[1], "url_kind", "") == "配信元記事URL",
        getattr(row[1], "published_at", "")), reverse=True)
    for fallback, headline, link in ordered:
        primary, tags = classify(headline, fallback)
        url, title = canonical_url(link), headline_key(headline)
        publisher = publisher_key(headline, link)
        if not url or url in urls or not title or title in titles:
            continue
        if publishers[publisher] >= 3 or len(grouped[primary]) >= 5:
            continue
        urls.add(url)
        titles.add(title)
        publishers[publisher] += 1
        grouped[primary].append((headline, link, tags))
    return grouped

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
            if not feed.get("version") or (not feed.entries and feed.get("bozo")):
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


# Signals describe event types visible in the headline, not inferred outcomes.
IMPACT = re.compile(
    r"利上げ|利下げ|政策金利|金融政策|GDP|物価指数|雇用統計|日銀短観|"
    r"買収|経営統合|事業承継|法案|制度改正|大規模障害|避難指示|警報", re.I)
SECONDARY_IMPACT = re.compile(r"決算|賃上げ|輸出|関税|補助金|供給停止|脆弱性|地震")
OFFICIAL_DOMAINS = ("go.jp", "boj.or.jp", "pref.niigata.lg.jp")

def select_top_news(groups, now=None):
    """Rank explicit impact, publication age and source provenance across categories."""
    now = now or datetime.now(timezone.utc)
    ranked = []
    for category, articles in groups.items():
        for headline, link, tags in articles:
            title = unicodedata.normalize("NFKC", headline)
            impact = 4 if IMPACT.search(title) else 2 if SECONDARY_IMPACT.search(title) else 0
            stamp = publication_time({"published": getattr(headline, "published_at", None)})
            if not impact or stamp is None:
                continue
            age = now - stamp
            if not timedelta(0) <= age <= timedelta(days=2):
                continue
            freshness = 2 if age <= timedelta(hours=12) else 1 if age <= timedelta(days=1) else 0
            domain = urlsplit(link).hostname or ""
            direct = getattr(headline, "url_kind", "") == "配信元記事URL"
            official = direct and any(domain == d or domain.endswith("." + d) for d in OFFICIAL_DOMAINS)
            provenance = 2 if official else 1 if direct else 0
            score = impact + freshness + provenance
            if score >= 4:
                ranked.append((score, stamp, category, headline, link, tags))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected, categories, publishers = [], Counter(), set()
    for _, _, category, headline, link, tags in ranked:
        publisher = publisher_key(headline, link)
        if categories[category] >= 2 or publisher in publishers:
            continue
        selected.append((category, (headline, link, tags)))
        categories[category] += 1
        publishers.add(publisher)
        if len(selected) == 5:
            break
    return selected
