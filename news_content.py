"""Public news identity and freshness checks; no private data access."""
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

def article_key(title, link):
    try:
        parts = urlsplit(link)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return title.strip()
        query = [(k, v) for k, v in parse_qsl(parts.query)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                           urlencode(sorted(query)), ""))
    except ValueError:
        return title.strip()

def fresh_entry(entry, now=None):
    # RSS without a publication timestamp is not asserted to be today's news.
    value = entry.get("published") or entry.get("updated")
    if not value:
        return False
    try:
        try:
            stamp = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        now = now or datetime.now(timezone.utc)
        return stamp.tzinfo is not None and timedelta(0) <= now - stamp <= timedelta(days=3)
    except (ValueError, TypeError, OverflowError):
        return False
