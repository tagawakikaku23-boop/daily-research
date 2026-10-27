"""Read-only verification using GitHub secrets. Never print keys, URLs or API bodies."""
import json
import os
import sys
import urllib.request
import urllib.error


def read_json(url, key, notion=False, body=None):
    headers = {"Authorization": "Bearer " + key}
    if notion:
        headers["Notion-Version"] = "2022-06-28"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    for name in ("NOTION_API_KEY", "NOTION_PAGE_ID", "GROQ_API_KEY"):
        if not os.environ.get(name):
            print(f"Missing required secret: {name}")
            return 1
    stage = "report database"
    try:
        key = os.environ["NOTION_API_KEY"]
        db = os.environ.get("NOTION_DB_ID") or "37afc83b-0e47-81d4-901d-d20a958122e0"
        watch = os.environ.get("NOTION_WATCHLIST_DB_ID") or "3555cab9-4828-4185-86eb-5a0614b8c0ea"
        report = read_json("https://api.notion.com/v1/databases/" + db, key, True)
        expected = {"名前": "title", "日付": "date", "種類": "select"}
        if any(report.get("properties", {}).get(k, {}).get("type") != v
               for k, v in expected.items()):
            print("Report database schema mismatch; no changes made.")
            return 1
        # Exercise the same query permission used by deduplication.
        print("PASS: report database schema")
        stage = "report database query"
        read_json("https://api.notion.com/v1/databases/" + db + "/query",
                  key, True, {"page_size": 1})
        print("PASS: report query")
        stage = "watchlist database"
        read_json("https://api.notion.com/v1/databases/" + watch, key, True)
        print("PASS: watchlist database")
        stage = "parent page"
        read_json("https://api.notion.com/v1/pages/" + os.environ["NOTION_PAGE_ID"], key, True)
        print("PASS: parent page")
        stage = "private report memory"
        from state_sync import load_remote
        load_remote()
        print("PASS: private report memory")
        stage = "Groq model list"
        # Use the same official client as report generation.
        from groq import Groq
        models = Groq(api_key=os.environ["GROQ_API_KEY"], timeout=30, max_retries=0).models.list()
        ids = {m.id for m in models.data}
        if not ids.intersection({"openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"}):
            print("No configured Groq model available.")
            return 1
    except urllib.error.HTTPError as exc:
        print(f"Read-only connection check failed at {stage} (HTTP {exc.code}); no response body logged.")
        return 1
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        suffix = f" (HTTP {status})" if isinstance(status, int) else ""
        print(f"Read-only connection check failed at {stage}{suffix}; no exception data logged.")
        return 1
    print("PASS: Notion report schema/query, watchlist, parent page and Groq model availability.")
    print("No Notion pages were created or changed. Newspaper logins and generation are not tested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

