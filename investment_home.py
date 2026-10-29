"""Sandbox-only weekly preview. Live portfolio ingestion is deliberately gated."""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from investment_analysis import JST, dashboard


def demo_rows(now):
    """Synthetic examples only; no real tickers or portfolio amounts."""
    stamp = now.isoformat()
    base = dict(asset_type="common_stock", price=1000, annual_dps=40,
                price_asof=stamp, financial_asof=stamp, dividend_asof=stamp,
                source_url="https://example.com/synthetic", payout_ratio_pct=40,
                net_income=100, operating_cf=150, free_cf=80, dividend_cut_5y=False,
                max_price=1100, target_yield_pct=3.5, sector_room=True,
                currency="JPY", dividend_currency="JPY", dividend_basis="forward_annual")
    return [dict(base, ticker="DEMO-A", name="架空A・通信", kind="holding", shares=10, sector="通信"),
            dict(base, ticker="DEMO-B", name="架空B・食品", kind="watch", sector="食品"),
            dict(base, ticker="DEMO-C", name="架空C・住宅", kind="watch", sector="住宅"),
            dict(base, ticker="DEMO-D", name="架空D・資料不足", kind="watch", sector="素材", free_cf=None)]


def preview(now):
    result = dashboard(demo_rows(now), 1000, now)
    result["synthetic"] = True
    result["generated_at"] = now.isoformat()
    return result


def render(result):
    total = result["annual_dividend_jpy"]
    progress = result["goal_progress_pct"]
    return ("検証用・架空データ / " + result["key"] + "\n"
            + (f"年間配当（税引前）: {total:,.0f}円" if total is not None else "年間配当: 算定保留")
            + " / 確認済み " + result["coverage"] + "\n"
            + (f"目標達成率: {progress:.1f}%" if progress is not None else "目標達成率: 未設定または算定保留")
            + "\n買い増し検討候補（最終確認が必要）: "
            + ("、".join(r["name"] for r in result["top3"]) or "該当なし")
            + "\n重要アラート: " + (" / ".join(result["alerts"]) or "なし")
            + "\n学習: 架空Aの配当性向40%と営業CFを比較し、減配リスクを考える。")


class NotionSandbox:
    def __init__(self, token, parent, transport=None):
        # Production targets are never accepted by this validation-only runner.
        if parent.replace("-", "") != "408fc83b0e4781dab814e8e4f351dbc9":
            raise ValueError("検証HOME以外への書込みは禁止されています")
        self.parent = parent
        self.token = token
        self.transport = transport or self._http

    def _http(self, method, path, body=None):
        req = Request("https://api.notion.com/v1/" + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": "Bearer " + self.token,
                               "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
                      method=method)
        # No automatic mutation retries. Timeout outcomes must be reconciled.
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    def save(self, result):
        if result.get("synthetic") is not True:
            raise ValueError("架空データ以外は受け付けません")
        title = "検証週次 " + result["key"]
        cursor = None
        while True:
            from urllib.parse import urlencode
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.transport("GET", f"blocks/{self.parent}/children?" + urlencode(params))
            for row in data["results"]:
                if row.get("child_page", {}).get("title") == title:
                    return "already_exists"
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        blocks = [{"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": render(result)}}]}}]
        # One request contains all content: a timeout is resolved by title on rerun.
        created = self.transport("POST", "pages", {
            "parent": {"page_id": self.parent},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": blocks})
        # Read back before reporting success.
        self.transport("GET", "pages/" + created["id"])
        return "created"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-sandbox", action="store_true")
    parser.add_argument("--write-logs", action="store_true")
    parser.add_argument("--read-notion", action="store_true")
    args = parser.parse_args()
    now = datetime.now(JST)
    if args.read_notion:
        if args.write_sandbox or args.write_logs:
            raise ValueError("実データの書込みはまだ有効化していません")
        from investment_notion import NotionClient
        client = NotionClient(os.environ['NOTION_API_KEY'])
        raw_goal = os.environ.get('INVESTMENT_ANNUAL_GOAL_JPY')
        goal = None
        if raw_goal:
            goal = float(raw_goal)
        else:
            try:
                goal = client.read_goal()
            except (RuntimeError, ValueError):
                # The existing integration may not have access to the new private page.
                # Keep the goal unknown; never widen access or invent a default amount.
                pass
        # No portfolio, rendered amounts, or source response goes to public stdout.
        dashboard(client.read_portfolio(), goal, now)
        print('Read-only Notion analysis completed. Financial details suppressed; no writes.')
        return
    result = preview(now)
    if args.write_logs:
        if os.environ.get("INVESTMENT_SANDBOX_WRITES") != "true":
            raise ValueError("検証書込みフラグが無効です")
        from investment_notion import NotionClient, save_weekly_logs
        statuses = save_weekly_logs(NotionClient(os.environ['NOTION_API_KEY']), demo_rows(now), result)
        print('Synthetic weekly logs completed; ' + str(len(statuses)) + ' records reconciled.')
    if args.write_sandbox:
        if os.environ.get("INVESTMENT_SANDBOX_WRITES") != "true":
            raise ValueError("検証書込みフラグが無効です")
        status = NotionSandbox(os.environ["NOTION_API_KEY"], os.environ["INVESTMENT_SANDBOX_PAGE_ID"]).save(result)
        print("Sandbox synthetic preview: " + status)
    elif not args.write_logs:
        print(render(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Investment preview failed; details suppressed. Reconcile any pending page before retrying.")
