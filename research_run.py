"""Independent runners. Production is opt-in and only permitted on main."""
import argparse
import json
import os
from datetime import datetime, timedelta
from research_core import JST, scheduled_slot, read_manifest
from research_store import Store
from research_briefing import title_key, news_report, investment_report
from research_sources import collect_news, summaries

SOURCES = {
    "watch": "ef95daef-20d6-4021-ac66-f4d8e18a753c",
    "funds": "39bfc83b-0e47-8168-918c-000be8fd5cfe",
    "assets": "39bfc83b-0e47-811e-b18f-000b1b19be74",
    "forecast": "37476491-ffe9-4e5b-948c-81364516ac3b",
}
DESTINATIONS = {"news": "30df8385-7101-4e54-a000-a281e6546b1d",
                "investment": "37afc83b-0e47-81b2-903a-000b13874832"}


def run(stream, now, report_date=None, write=False):
    if write and (os.environ.get("RESEARCH_PUBLISH_ENABLED") != "true" or
                  os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("Production publication is not enabled")
    slot = scheduled_slot(stream, now, report_date)
    title, key = title_key(stream, slot)
    store = Store(os.environ["NOTION_API_KEY"], DESTINATIONS[stream],
                  SOURCES.values() if stream == "investment" else ())
    # Read-only dry runs must not reconcile a pending icon.
    if not write:
        existing = store.find(title)
        if existing:
            manifest = read_manifest(store.children(existing["id"]))
            if manifest["key"] != key:
                raise RuntimeError("Report identity mismatch")
            return "already_exists"
    elif store.existing(title, key):
        return "already_exists"
    history = store.history((now - timedelta(days=35)).date().isoformat(), stream)
    if stream == "news":
        articles, errors = collect_news(now, history)
        ai = summaries(articles, os.environ.get("GROQ_API_KEY"), os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))
        title, key, blocks = news_report(slot, now, articles, errors, ai)
    else:
        data = {k: store.query(v) for k, v in SOURCES.items()}
        title, key, blocks = investment_report(slot, now, data["watch"], data["funds"],
                                              data["assets"], data["forecast"], history)
    manifest = read_manifest(blocks)
    status = store.save(title, key, slot.date().isoformat(), blocks, stream) if write else "validated_read_only"
    # No financial content, symbols, page URLs or raw exceptions in public logs.
    print(json.dumps({"stream": stream, "status": status, "degraded": manifest["degraded"]}))
    if manifest["degraded"]:
        raise RuntimeError("Partial provider failure; report retained")
    return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stream", choices=["news", "investment"])
    parser.add_argument("--date", help="Explicit JST report date for a bounded retry")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        run(args.stream, datetime.now(JST), args.date, args.publish)
    except Exception:
        raise SystemExit("Research failed or degraded. Inspect private report; raw details suppressed.")

