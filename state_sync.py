"""Keep report memory in Notion, never in a public Git commit or Actions log."""
import json
import os
from pathlib import Path
import sys
import urllib.request

STATE_PAGE = os.environ.get("NOTION_REPORT_STATE_PAGE_ID", "406fc83b-0e47-81db-bb71-fb5ab0e4255e")
FILES = {"last_snapshot": "last_snapshot.json", "last_discovery": "last_discovery.json"}


def request(method, path, body=None):
    headers = {"Authorization": "Bearer " + os.environ["NOTION_API_KEY"],
               "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    req = urllib.request.Request("https://api.notion.com/v1/" + path, method=method,
                                 headers=headers, data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def load_remote():
    cursor = ""
    while True:
        path = f"blocks/{STATE_PAGE}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        result = request("GET", path)
        for block in result.get("results", []):
            if block.get("type") == "code" and block["code"].get("language") == "json":
                value = "".join(t.get("plain_text", t.get("text", {}).get("content", ""))
                                for t in block["code"]["rich_text"])
                return block["id"], json.loads(value)
        if not result.get("has_more"):
            raise RuntimeError("State JSON block is missing")
        cursor = result["next_cursor"]


def sync(mode):
    if mode not in ("pull", "push"):
        raise ValueError("Unsupported state operation")
    block_id, remote = load_remote()
    if mode == "pull":
        for key, filename in FILES.items():
            Path(filename).write_text(json.dumps(remote.get(key, {}), ensure_ascii=False), encoding="utf-8")
    else:
        for key, filename in FILES.items():
            if Path(filename).exists():
                remote[key] = json.loads(Path(filename).read_text(encoding="utf-8"))
        value = json.dumps(remote, ensure_ascii=False)
        parts = [{"type": "text", "text": {"content": value[i:i+1800]}}
                 for i in range(0, len(value), 1800)]
        request("PATCH", "blocks/" + block_id, {"code": {"language": "json", "rich_text": parts}})


if __name__ == "__main__":
    try:
        sync(sys.argv[1])
        print("PASS: private Notion report memory synchronized.")
    except Exception:
        print("FAIL: private report memory sync; details suppressed to protect portfolio data.")
        sys.exit(1)

