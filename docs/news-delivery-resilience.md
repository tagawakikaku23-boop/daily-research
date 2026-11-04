# News delivery resilience — review before production
## Verified 2026-11-05 JST
- main: f15562595fa07db116f75e26bbb9dd8f298ff0d3.
- PR #5 remains draft at 437201325d43ac45085d9183673d55434a470c39.
- No README or AGENTS.md exists in the main repository tree.
- Schedules remain 07:00/19:00 JST, weekly Monday 07:30.
- Run 41881184027: evening schedule started 2026-11-05 02:51 JST and failed
  before collection with the safe delivery window error.
- Run 41756769724 succeeded; provider login/content success is not inferred.
- Watchlist and operations health runs succeeded. Masked non-empty Notion,
  Groq and parent-page secrets appear in job metadata. Secret-list access is
  unavailable through the connector; newspaper credentials are not verified.
- Separate branch codex/separate-news-investment already exists with validation.
  It is not merged, changed, or silently enabled by this patch.
## Proposed behavior
The existing scheduled workflow gains an independent news job (no new schedule).
Stock runs retain the old safe window. News does not load holdings or state.
A run's immutable created_at anchors its last scheduled 07:00/19:00 occurrence;
a retry uses the same date/edition even after midnight. Maximum age is 36 hours.
GitHub does not expose the exact intended occurrence for a schedule delayed more
than a full cycle; such missed occurrences cannot be reliably reconstructed.
Explicit bounded report_date is available for manual news-only retry.
Delivery titles retain legacy date/time matching for existing report lookup.
Actual retrieval start is shown separately. Existing pending pages block reruns;
ambiguous creation is not retried. Existing concurrency prevents parallel runs
of this workflow. Do not run another publisher against these slots concurrently.
Repeated links within a report are normalized and deduplicated. The same public
story may appear in both morning and evening reports; cross-edition story
suppression is not implemented. RSS older than 3 days, future dated or undated
items are excluded. Newspaper publication timestamps remain unverified.
All-source empty collection fails without posting. Individual empty categories
retain the existing failure display; no cached report is reused as fresh news.
## Validation and limits
Run the main-only tests plus tests/test_news_resilience.py. Tests include
cross-midnight/retry identity, stock-free import, news entrypoint, mock
collection-to-save pipeline, empty collection and tracking-link duplicates.
No live provider login, Groq generation or Notion report write has been run.
Notion API save behavior is tested with mocks; this is not production verification.
Monthly, watchlist and investment-HOME code is unchanged.
## Production approval and rollout
1. Review this PR, the passing CI and the remaining live-validation gap.
2. Before any production write, disclose: one news page in the existing reports
   DB for one unposted date/edition; no holdings/state/schema changes.
3. With user approval, merge and run a bounded manual date/edition once. An
   existing report is skipped; a pending report requires manual inspection.
4. Verify full Notion content, actual retrieval time, login/category status and
   next scheduled morning/evening delivery. Do not enable research_news.yml or
   any second scheduler in parallel.
5. This patch intentionally leaves push activation on the legacy stock route;
   activation pushes do not trigger independent news. Use scheduled/manual news.
## Rollback
First pause using CLOUD_REPORTS_ENABLED=false; let active workflow finish.
Revert this PR commit through a new revert PR, then restore the previous setting
after review. Do not delete Notion reports or state, or reactivate PC schedules.
Reverting restores the pre-existing delay failure, so keep the limitation visible.

