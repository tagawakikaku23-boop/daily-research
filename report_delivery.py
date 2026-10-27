"""Notion delivery safeguards; no schema changes or deletion of existing pages."""
import re


def report_slot(title, icon):
    """Daily automatic reports deduplicate by JST date and edition, not minute."""
    if icon in ("🌅", "🌙", "🛒"):
        return title[:10], icon
    if icon == "📰":
        match = re.search(r"\b(\d{2}):\d{2}\b", title)
        # Legacy news without a time was delivered with the evening report.
        edition = "morning" if match and int(match[1]) < 15 else "evening"
        return title[:10], icon, edition
    return None  # Preserve explicit manual reloads.


def find_existing(requests, headers, db_id, parent_id, title, icon, kinds):
    slot = report_slot(title, icon)
    if slot is None:
        return None
    cursor = None
    while True:
        if db_id:
            body = {"page_size": 100, "filter": {"and": [
                {"property": "日付", "date": {"equals": slot[0]}},
                {"property": "種類", "select": {"equals": kinds[icon]}},
            ]}}
            if cursor:
                body["start_cursor"] = cursor
            resp = requests.post(f"https://api.notion.com/v1/databases/{db_id}/query",
                                 headers=headers, json=body, timeout=30)
        else:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = requests.get(f"https://api.notion.com/v1/blocks/{parent_id}/children",
                                headers=headers, params=params, timeout=30)
        resp.raise_for_status()  # Fail closed: a failed lookup must never publish.
        result = resp.json()
        for row in result.get("results", []):
            if db_id:
                name = "".join(x.get("plain_text", x.get("text", {}).get("content", ""))
                               for x in row.get("properties", {}).get("名前", {}).get("title", []))
                row_icon = icon  # The server filter already selected this report kind.
            else:
                if row.get("type") != "child_page":
                    continue
                name = row["child_page"]["title"]
                # Old page children do not expose icons. Recognize legacy titles.
                row_icon = next((emoji for term, emoji in (
                    ("朝の株レポート", "🌅"), ("夜の株レポート", "🌙"),
                    ("ニュースダイジェスト", "📰"), ("今週の買い場チェック", "🛒"))
                    if term in name), "")
            if report_slot(name, row_icon) == slot:
                if (row.get("icon") or {}).get("emoji") == "⏳":
                    raise RuntimeError("同じ回の投稿が未完了です。Notionの作成中ページを確認してください。")
                return row.get("url") or "https://www.notion.so/" + row["id"].replace("-", "")
        if not result.get("has_more"):
            return None
        cursor = result["next_cursor"]


def publish(requests, headers, data, blocks):
    # A timeout/5xx on POST can mean the page was created. Never retry blindly.
    pending = dict(data, icon={"type": "emoji", "emoji": "⏳"})
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers,
                         json=pending, timeout=60)
    resp.raise_for_status()
    page = resp.json()
    for offset in range(100, len(blocks), 100):
        appended = requests.patch(f"https://api.notion.com/v1/blocks/{page['id']}/children",
                                  headers=headers, json={"children": blocks[offset:offset+100]},
                                  timeout=60)
        appended.raise_for_status()
    completed = requests.patch(f"https://api.notion.com/v1/pages/{page['id']}",
                               headers=headers, json={"icon": data["icon"]}, timeout=30)
    completed.raise_for_status()
    return page.get("url", "")

