"""Cloud health checks. No portfolio values, task text, or API bodies are logged."""
from datetime import datetime, timezone, timedelta
import json
import os
import re
import sys
import urllib.request

JST = timezone(timedelta(hours=9))
WORKFLOWS = ("daily_report.yml", "refresh_watchlist.yml", "monthly_review.yml")
OPS_PAGE = "406fc83b-0e47-81b7-8e6b-cd45c6fec9a6"
REPORT_DB = "37afc83b-0e47-81d4-901d-d20a958122e0"


def api(url, token, body=None, notion=False):
    headers = {"Authorization": "Bearer " + token, "User-Agent": "daily-research-health/1.0"}
    if notion:
        headers["Notion-Version"] = "2022-06-28"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    # Never retry POST: a timeout can follow a successful creation.
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def required_editions(now):
    # Leave three hours for scheduling delay and report generation.
    needed = set()
    if now.hour >= 10:
        needed.update(("morning", "news_morning"))
    if now.hour >= 22:
        needed.update(("evening", "news_evening"))
    return needed


def completed_editions(rows):
    found = set()
    for row in rows:
        if (row.get("icon") or {}).get("emoji") == "⏳":
            continue
        props = row.get("properties", {})
        kind = (props.get("種類", {}).get("select") or {}).get("name", "")
        if kind == "🌅 朝":
            found.add("morning")
        elif kind == "🌙 夜":
            found.add("evening")
        elif kind == "📰 ニュース":
            title = "".join(x.get("plain_text", x.get("text", {}).get("content", ""))
                            for x in props.get("名前", {}).get("title", []))
            match = re.search(r"\b(\d{2}):\d{2}\b", title)
            found.add("news_morning" if match and int(match[1]) < 15 else "news_evening")
    return found


def summarize(runs, enabled, now, editions):
    lines, problems = [], []
    if not enabled:
        lines.append("日報：停止中。旧PCの予約停止確認後に切替。点検成功は投稿成功を意味しません。")
    else:
        missing = required_editions(now) - editions
        if missing:
            problems.append("本日の期限を過ぎた未配信の回あり：" + ", ".join(sorted(missing)))
        lines.append("日報：有効。Notionの当日完了レポートで配信を照合。")
    for name in WORKFLOWS:
        recent = runs.get(name, [])
        if not recent:
            problems.append(name + "：実行履歴を確認できません。")
            continue
        latest = recent[0]
        outcome = latest.get("conclusion") or latest.get("status", "unknown")
        lines.append(name + "：" + outcome + " / " + latest["created_at"])
        if latest.get("conclusion") in ("failure", "timed_out", "cancelled"):
            problems.append(name + "：最新実行に失敗または中断。再実行前に履歴を確認。")
        if name == "refresh_watchlist.yml" and now.weekday() < 5 and now.hour >= 18:
            successes = [x for x in recent if x.get("conclusion") == "success"]
            if not successes or (now - datetime.fromisoformat(
                    successes[0]["created_at"].replace("Z", "+00:00"))).total_seconds() > 30 * 3600:
                problems.append(name + "：平日の最終成功が30時間より前。")
    return lines, problems


def report_rows(token, date):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100, "filter": {"property": "日付", "date": {"equals": date}}}
        if cursor:
            body["start_cursor"] = cursor
        result = api("https://api.notion.com/v1/databases/" + REPORT_DB + "/query",
                     token, body, True)
        rows.extend(result.get("results", []))
        if not result.get("has_more"):
            return rows
        cursor = result["next_cursor"]


def save_snapshot(token, run_id, now, lines, problems):
    title = now.strftime("%Y-%m-%d") + " 稼働点検（実行" + run_id + "）"
    cursor = None
    while True:
        url = "https://api.notion.com/v1/blocks/" + OPS_PAGE + "/children?page_size=100"
        if cursor:
            url += "&start_cursor=" + cursor
        result = api(url, token, notion=True)
        if any(b.get("child_page", {}).get("title") == title for b in result.get("results", [])):
            return False  # Same Actions run, including retries, must not create two snapshots.
        if not result.get("has_more"):
            break
        cursor = result["next_cursor"]
    messages = ["点検時刻：" + now.isoformat(), *lines, *problems,
                "この点検は既存レポート・案件・健康記録を更新していません。",
                "日報の実投稿や新聞ログインの成功を保証するものではありません。"]
    children = [{"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"type": "text", "text": {"content": msg}}]}}
                for msg in messages]
    api("https://api.notion.com/v1/pages", token, {
        "parent": {"page_id": OPS_PAGE}, "icon": {"type": "emoji", "emoji": "⚠️" if problems else "🛠️"},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": children}, True)
    return True


def main():
    now = datetime.now(JST)
    token = os.environ["GH_TOKEN"]
    notion = os.environ["NOTION_API_KEY"]
    repo = os.environ.get("GITHUB_REPOSITORY", "tagawakikaku23-boop/daily-research")
    runs = {name: api("https://api.github.com/repos/" + repo + "/actions/workflows/"
                     + name + "/runs?branch=main&per_page=5", token)["workflow_runs"]
            for name in WORKFLOWS}
    enabled = os.environ.get("CLOUD_REPORTS_ENABLED") == "true"
    # Query even while posting is disabled, to test read access without creating a report.
    editions = completed_editions(report_rows(notion, now.strftime("%Y-%m-%d")))
    lines, problems = summarize(runs, enabled, now, editions)
    summary = "\n".join(["# 自動処理の稼働点検", *lines, *problems])
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
    if "--save" in sys.argv:
        save_snapshot(notion, os.environ["GITHUB_RUN_ID"], now, lines, problems)
    if problems:
        print("::warning::点検で要確認事項あり。日報の自動再開や再投稿はしていません。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("::error::点検自体が失敗しました。秘密値と応答本文は表示しません。")
        sys.exit(1)

