"""Stable news edition identity, independent of stock routing and queue delays."""
from datetime import datetime, timedelta
import json
import os
from urllib.request import Request, urlopen
from report_schedule import JST, load_config, effective_enabled

def news_slot(edition, created_at, now, requested_date=""):
    if edition not in {"morning", "evening"} or created_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("Invalid news delivery context")
    anchor = created_at.astimezone(JST)
    hour = 7 if edition == "morning" else 19
    day = datetime.strptime(requested_date, "%Y-%m-%d").date() if requested_date else anchor.date()
    slot = datetime.combine(day, datetime.min.time(), JST).replace(hour=hour)
    if not requested_date and slot > anchor:
        slot -= timedelta(days=1)
    if slot > now or now - slot > timedelta(hours=36):
        raise ValueError("News edition too old or in the future; review required")
    return slot

def route(config, event, schedule, mode, action, override, ref, created_at, now, requested_date=""):
    if ref != "refs/heads/main":
        return None
    if event == "schedule":
        edition = {"0 22 * * *": "morning", "0 10 * * *": "evening"}.get(schedule)
        if not edition or not effective_enabled(config, override):
            return None
    elif event == "workflow_dispatch":
        if mode not in {"morning", "evening"} or action != "post" or override.lower() == "false":
            return None
        edition = mode
    else:
        # Activation pushes remain on the legacy route until explicitly reviewed.
        return None
    return edition, news_slot(edition, created_at, now, requested_date)

def workflow_created_at():
    # The run creation timestamp is immutable across retries. Never use a retry's
    # current clock to relabel an old evening run as the following day's edition.
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    req = Request(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}",
                  headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                           "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=30) as response:
        value = json.load(response)["created_at"]
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def main():
    now = datetime.now(JST)
    result = route(load_config(), os.environ["EVENT"], os.environ.get("SCHEDULE", ""),
                   os.environ.get("INPUT_MODE", ""), os.environ.get("INPUT_ACTION", ""),
                   os.environ.get("CLOUD_REPORTS_ENABLED", ""), os.environ["GITHUB_REF"],
                   workflow_created_at(), now, os.environ.get("REPORT_DATE", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("post=" + ("true" if result else "false") + "\n")
        if result:
            edition, slot = result
            stream.write(f"edition={edition}\ndate={slot:%Y-%m-%d}\ntime={slot:%H:%M}\n")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("News routing failed; no publication attempted.")

