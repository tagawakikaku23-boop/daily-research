"""Scoped Notion persistence. Existing schemas and historical records stay intact."""
import json
import time
from uuid import UUID
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from research_core import block_text, read_manifest, signature

def uid(value):
    return str(UUID(value.removeprefix("collection://")))


def text_prop(value, title=False):
    return {"title" if title else "rich_text": [{"text": {"content": value}}]}


class Store:
    def __init__(self, token, destination, sources=(), sandbox_parent=None, transport=None, sleep=time.sleep):
        self.destination = uid(destination)
        self.sources = {uid(s) for s in sources} | {self.destination}
        self.sandbox_parent = uid(sandbox_parent) if sandbox_parent else None
        self.token, self.transport, self.sleep = token, transport or self.http, sleep
        self.owned = set()

    def http(self, method, path, body):
        req = Request("https://api.notion.com/v1/" + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": "Bearer " + self.token, "Notion-Version": "2025-09-03",
                               "Content-Type": "application/json"}, method=method)
        with urlopen(req, timeout=30) as r:
            return json.load(r)

    def request(self, method, path, body=None):
        # Only explicit read sources, new report creation, and completion of our
        # own pending reports. No source edits, schema changes or deletion.
        safe = method == "GET"
        if method == "POST" and path.startswith("data_sources/") and path.endswith("/query"):
            if uid(path.split("/")[1]) not in self.sources:
                raise ValueError("Source not allowed")
            safe = True
        elif method == "POST":
            if path != "pages" or body.get("parent", {}).get("data_source_id") != self.destination:
                raise ValueError("Destination not allowed")
        elif method == "PATCH":
            if path not in {"pages/" + p for p in self.owned} or set(body) != {"icon"}:
                raise ValueError("Existing records are read-only")
        elif method != "GET":
            raise ValueError("Operation not allowed")
        if "://" in path or ".." in path:
            raise ValueError("Invalid path")
        for attempt in range(3):
            self.sleep(.4)
            try:
                return self.transport(method, path, body)
            except HTTPError as exc:
                if not safe or exc.code not in {429, 500, 502, 503, 504, 529} or attempt == 2:
                    raise RuntimeError("Notion HTTP failure; response suppressed") from None
                payload = exc.read().decode(errors="replace") if exc.fp else ""
                if "public_api_request_blocked" in payload:
                    raise RuntimeError("Notion access blocked") from None
                delay = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = int(delay) if delay is not None else 2 ** attempt
                except (TypeError, ValueError):
                    raise RuntimeError("Invalid retry window") from None
                if not 0 <= delay <= 60:
                    raise RuntimeError("Retry deferred to next run") from None
                self.sleep(delay)
            except (URLError, TimeoutError):
                if not safe or attempt == 2:
                    raise RuntimeError("Notion transport failed; reconcile before retry") from None
                self.sleep(2 ** attempt)

    def query(self, ds, filter=None):
        ds = uid(ds)
        result, cursor, seen = [], None, set()
        for _ in range(100):
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            if filter:
                body["filter"] = filter
            data = self.request("POST", f"data_sources/{ds}/query", body)
            result.extend(p for p in data["results"] if not p.get("archived") and not p.get("in_trash"))
            if not data.get("has_more"):
                return result
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen:
                raise RuntimeError("Incomplete pagination")
            seen.add(cursor)
        raise RuntimeError("Pagination limit reached")

    def children(self, page):
        result, cursor, seen = [], None, set()
        for _ in range(100):
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.request("GET", f"blocks/{uid(page)}/children?" + urlencode(params))
            result.extend(data["results"])
            if not data.get("has_more"):
                return result
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen:
                raise RuntimeError("Incomplete blocks")
            seen.add(cursor)
        raise RuntimeError("Block limit reached")

    def check_destination(self):
        meta = self.request("GET", "data_sources/" + self.destination)
        if self.sandbox_parent:
            db = self.request("GET", "databases/" + uid(meta["parent"]["database_id"]))
            if uid(db.get("parent", {}).get("page_id", "")) != self.sandbox_parent:
                raise ValueError("Sandbox destination moved")
        props = meta["properties"]
        for name, kind in {"名前": "title", "日付": "date"}.items():
            if props.get(name, {}).get("type") != kind:
                raise ValueError("Destination schema mismatch")
        return props

    def find(self, title):
        rows = self.query(self.destination, {"property": "名前", "title": {"equals": title}})
        if len(rows) > 1:
            raise RuntimeError("Duplicate report keys; manual review required")
        return rows[0] if rows else None

    def existing(self, title, key):
        row = self.find(title)
        if not row:
            return None
        blocks = self.children(row["id"])
        manifest = read_manifest(blocks)
        if manifest["key"] != key:
            raise RuntimeError("Existing report identity mismatch")
        if (row.get("icon") or {}).get("emoji") == "⏳":
            self.owned.add(uid(row["id"]))
            self.request("PATCH", "pages/" + uid(row["id"]), {"icon": {"type": "emoji", "emoji": "✅"}})
            saved = self.request("GET", "pages/" + uid(row["id"]))
            if (saved.get("icon") or {}).get("emoji") != "✅":
                raise RuntimeError("Completion readback failed")
        return row

    def history(self, since, stream):
        rows = self.query(self.destination, {"and": [
            {"property": "日付", "date": {"on_or_after": since}},
            {"property": "名前", "title": {"contains": "研究v2:" + stream + ":"}},
        ]})
        result = []
        for row in rows:
            # Do not skip a pending report: reconcile its manifest before using it.
            manifest = read_manifest(self.children(row["id"]))
            if manifest.get("stream") != stream:
                raise RuntimeError("History stream mismatch")
            result.append(manifest)
        return result

    def save(self, title, key, date, blocks, stream):
        existing = self.existing(title, key)
        if existing:
            return "already_exists"
        props_schema = self.check_destination()
        if read_manifest(blocks)["key"] != key or len(blocks) > 100:
            raise ValueError("Invalid report")
        props = {"名前": text_prop(title, True), "日付": {"date": {"start": date}}}
        if stream == "news" and "便" in props_schema:
            props["便"] = {"select": {"name": "🌅 朝"}}
        if stream == "news" and "分野" in props_schema:
            field = props_schema["分野"]
            categories = read_manifest(blocks).get("categories", [])
            allowed = {o["name"] for o in field.get("multi_select", {}).get("options", [])}
            if field.get("type") != "multi_select" or set(categories) - allowed:
                raise ValueError("News category schema mismatch")
            props["分野"] = {"multi_select": [{"name": c} for c in categories]}
        if stream == "investment" and "種類" in props_schema:
            props["種類"] = {"select": {"name": "🌅 朝"}}
        # Entire report in a single create; ambiguous timeouts are reconciled by
        # title+manifest on the next run, never blindly POSTed again.
        created = self.request("POST", "pages", {
            "parent": {"data_source_id": self.destination}, "properties": props,
            "icon": {"type": "emoji", "emoji": "⏳"}, "children": blocks})
        page_id = uid(created["id"])
        self.owned.add(page_id)
        saved = self.request("GET", "pages/" + page_id)
        saved_title = "".join(x.get("plain_text", x.get("text", {}).get("content", ""))
                              for x in saved.get("properties", {}).get("名前", {}).get("title", []))
        if saved_title != title or saved.get("properties", {}).get("日付", {}).get("date", {}).get("start") != date:
            raise RuntimeError("Page property readback failed")
        stored = self.children(page_id)
        if signature(stored) != signature(blocks):
            raise RuntimeError("Full body readback failed")
        read_manifest(stored)
        self.request("PATCH", "pages/" + page_id, {"icon": {"type": "emoji", "emoji": "✅"}})
        final = self.request("GET", "pages/" + page_id)
        if (final.get("icon") or {}).get("emoji") != "✅":
            raise RuntimeError("Completion readback failed")
        return "created"


