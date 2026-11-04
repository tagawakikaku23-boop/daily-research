"""Live Store contract test using only synthetic data in a fixed sandbox."""
import json
import os
from urllib.error import HTTPError
from research_core import block, complete_blocks, read_manifest, signature
from research_store import Store

DESTINATION = "a640a25d-b831-4034-82d0-01f6cc9d6330"
PARENT = "40efc83b-0e47-81a3-955c-ed432a99b823"


def main():
    # No production source reads, arbitrary destinations, assets or holdings.
    store = Store(os.environ["NOTION_API_KEY"], DESTINATION, sandbox_parent=PARENT)
    original_transport = store.http
    def diagnostic_transport(method, path, body):
        try:
            return original_transport(method, path, body)
        except HTTPError as exc:
            print(json.dumps({"sandbox_http_status": exc.code}), flush=True)
            raise
    store.transport = diagnostic_transport
    print("sandbox: verify destination", flush=True)
    store.check_destination()
    title = "Synthetic Store contract v1 [sandbox-only]"
    key = "sandbox:store-contract:v1"
    blocks = complete_blocks(
        [block("架空データ専用。個人資産・保有情報は含みません。"),
         block("日本語・長文の本文読戻し確認。" * 160)],
        {"key": key, "stream": "news", "categories": ["新潟"],
         "degraded": False, "article_ids": []})
    print("sandbox: save and readback", flush=True)
    first = store.save(title, key, "2026-11-05", blocks, "news")
    if first not in {"created", "already_exists"}:
        raise RuntimeError("Unexpected initial result")
    print("sandbox: idempotency", flush=True)
    if store.save(title, key, "2026-11-05", blocks, "news") != "already_exists":
        raise RuntimeError("Idempotency failed")
    rows = store.query(DESTINATION, {"property": "名前", "title": {"equals": title}})
    if len(rows) != 1:
        raise RuntimeError("Expected exactly one synthetic page")
    page = store.request("GET", "pages/" + rows[0]["id"])
    actual = store.children(rows[0]["id"])
    if signature(actual) != signature(blocks) or read_manifest(actual)["key"] != key:
        raise RuntimeError("Body verification failed")
    props = page["properties"]
    if {x["name"] for x in props["分野"]["multi_select"]} != {"新潟"}:
        raise RuntimeError("Category readback failed")
    if props["日付"]["date"]["start"] != "2026-11-05":
        raise RuntimeError("Date readback failed")
    if page.get("icon", {}).get("emoji") != "✅":
        raise RuntimeError("Completion verification failed")
    print(json.dumps({"sandbox_store_integration": "passed",
                      "full_body_readback": True, "duplicate_count": 0}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Sandbox integration failed; raw API details suppressed")
