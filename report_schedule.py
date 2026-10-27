"""Shared cloud reporting switch and safe activation routing."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path

JST = timezone(timedelta(hours=9))
MODES = {"check", "morning", "evening", "manual", "weekly"}
CRONS = {"0 22 * * *": "morning", "0 10 * * *": "evening", "30 22 * * 0": "weekly"}


def load_config():
    config = json.loads(Path("cloud-reporting.json").read_text(encoding="utf-8-sig"))
    if type(config.get("enabled")) is not bool:
        raise ValueError("enabled must be a boolean")
    return config


def effective_enabled(config, override=""):
    # Keep the existing repository variable as an emergency pause switch.
    return config["enabled"] and override.lower() != "false"


def choose(config, event, now, schedule="", mode="check", action="check", override="", ref="refs/heads/main"):
    if mode not in MODES or action not in {"check", "post"}:
        raise ValueError("Invalid dispatch inputs")
    if event == "workflow_dispatch":
        if mode != "check" and action == "post" and ref != "refs/heads/main":
            raise ValueError("Publication is restricted to main")
        return mode, "check" if mode == "check" else action
    if event not in {"push", "schedule"}:
        raise ValueError("Unsupported event")
    if ref != "refs/heads/main":
        raise ValueError("Automatic publication is restricted to main")
    if not effective_enabled(config, override):
        return "check", "check"
    if event == "push":
        activation = config.get("activation", {})
        if activation.get("date") != now.strftime("%Y-%m-%d"):
            return "check", "check"
        mode = activation.get("mode")
        if mode not in {"morning", "evening"}:
            raise ValueError("Invalid activation mode")
    else:
        if schedule not in CRONS:
            raise ValueError("Unknown schedule")
        mode = CRONS[schedule]
    # Reject extreme delays rather than labeling the wrong edition/day.
    if (mode == "morning" and now.hour >= 15) or (mode == "evening" and now.hour < 15):
        raise ValueError("The report edition is outside its safe delivery window")
    return mode, "post"


if __name__ == "__main__":
    mode, action = choose(load_config(), os.environ["EVENT"], datetime.now(JST),
                          os.environ.get("SCHEDULE", ""), os.environ.get("INPUT_MODE") or "check",
                          os.environ.get("INPUT_ACTION") or "check",
                          os.environ.get("CLOUD_REPORTS_ENABLED", ""),
                          ref=os.environ["GITHUB_REF"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"mode={mode}\naction={action}\n")
    print(f"Selected mode={mode}, action={action}")

